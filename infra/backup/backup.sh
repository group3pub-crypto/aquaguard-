#!/bin/sh
# Encrypted logical backup of the AquaGuard database .
#
#   backup.sh once      take one backup now, apply retention, exit
#   backup.sh daemon    take one backup every day at BACKUP_HOUR_UTC (default 02:xx), forever
#
# Environment
#   PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD   libpq connection (a role with pg_read_all_data)
#   BACKUP_AGE_RECIPIENT   age public key(s) "age1..." (comma separated). REQUIRED. Only PUBLIC keys
#                          live on the backup host; the private identity stays offline and is
#                          needed only by restore.sh.
#   BACKUP_DIR             output directory (/backups)
#   BACKUP_RETENTION_DAYS  delete backups older than this (14); 0 keeps everything
#   BACKUP_HOUR_UTC        hour of day for `daemon` (2)
#
# Output: aquaguard-<UTC stamp>.dump.age (+ .sha256) = pg_dump --format=custom, age-encrypted.
set -eu
umask 077

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION="${BACKUP_RETENTION_DAYS:-14}"

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }

backup_once() {
  : "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT (age public key) is required}"
  mkdir -p "$BACKUP_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  work=$(mktemp -d "${TMPDIR:-/tmp}/aqbackup.XXXXXX")
  # shellcheck disable=SC2064
  trap "rm -rf '$work'" EXIT INT TERM

  set --
  old_ifs=$IFS; IFS=,
  for r in $BACKUP_AGE_RECIPIENT; do set -- "$@" -r "$r"; done
  IFS=$old_ifs

  log "dumping database ${PGDATABASE:-?}"
  pg_dump --format=custom --compress=6 --no-owner --no-privileges \
    --lock-wait-timeout=60000 --file="$work/db.dump"
  # structural sanity check of the archive before it is encrypted (cannot be done afterwards)
  pg_restore --list "$work/db.dump" >/dev/null
  out="$BACKUP_DIR/aquaguard-$stamp.dump.age"
  age "$@" --output "$out.part" "$work/db.dump"
  mv "$out.part" "$out"
  ( cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256" )
  log "wrote $out ($(wc -c < "$out") bytes)"

  date -u +%FT%TZ > "$BACKUP_DIR/.last_success"
  if [ "$RETENTION" -gt 0 ]; then
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'aquaguard-*' -mtime +"$RETENTION" \
      -print -delete | sed 's/^/backup: pruned /'
  fi
  rm -rf "$work"; trap - EXIT INT TERM
}

seconds_until_next_run() {
  hour="${BACKUP_HOUR_UTC:-2}"
  now=$(date -u +%s)
  today=$(date -u -d "$(date -u +%F) $hour:17:00" +%s 2>/dev/null || date -u +%s)
  [ "$today" -gt "$now" ] && echo $((today - now)) || echo $((today + 86400 - now))
}

case "${1:-once}" in
  once) backup_once ;;
  daemon)
    log "daemon started: daily at ${BACKUP_HOUR_UTC:-2}:17 UTC, retention ${RETENTION}d"
    while :; do
      sleep "$(seconds_until_next_run)" &
      wait $! || exit 0
      backup_once || log "FAILED (will retry next cycle)"
    done ;;
  *) echo "usage: backup.sh once|daemon" >&2; exit 2 ;;
esac

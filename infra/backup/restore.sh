#!/bin/sh
# Restore an encrypted backup produced by backup.sh into a database.
#
#   restore.sh --file aquaguard-<stamp>.dump.age --identity age-key.txt --target-db NAME \
#              [--create] [--clean] [--allow-live]
#
#   --identity   age private key file (kept offline; never stored with the backups)
#   --target-db  database to restore into. Restoring into the live database requires --allow-live
#                (stop the backend first). A scratch name is the safe way to verify a backup.
#   --create     CREATE DATABASE first (owned by $RESTORE_OWNER, default aquaguard_owner)
#   --clean      drop existing objects before recreating them (pg_restore --clean --if-exists)
#
# Connection: libpq variables (PGHOST, PGPORT, PGUSER, PGPASSWORD) of the SUPERUSER (needed for
# --create) or of the owner role. Objects are restored with `SET ROLE $RESTORE_OWNER`
# (default aquaguard_owner), so they are owned by the owner role and the database's default
# privileges re-grant DML to the application role. The dump carries no owners/ACLs on purpose.
# The .sha256 next to the file is verified first when present.
set -eu
umask 077

file='' identity='' target='' create=0 clean=0 live=0
while [ $# -gt 0 ]; do
  case "$1" in
    --file) file=$2; shift 2 ;;
    --identity) identity=$2; shift 2 ;;
    --target-db) target=$2; shift 2 ;;
    --create) create=1; shift ;;
    --clean) clean=1; shift ;;
    --allow-live) live=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -r "$file" ] || { echo "restore: --file missing/unreadable" >&2; exit 2; }
[ -r "$identity" ] || { echo "restore: --identity missing/unreadable" >&2; exit 2; }
[ -n "$target" ] || { echo "restore: --target-db is required" >&2; exit 2; }
if [ "$target" = "${LIVE_DB:-aquaguard}" ] && [ "$live" != 1 ]; then
  echo "restore: refusing to touch the live database '$target' without --allow-live" >&2
  exit 2
fi

if [ -f "$file.sha256" ]; then
  ( cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null ) \
    || { echo "restore: checksum mismatch - backup is corrupt or tampered" >&2; exit 1; }
  echo "restore: checksum ok"
else
  echo "restore: WARNING no .sha256 next to the file; relying on age authentication only" >&2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/aqrestore.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM
age --decrypt --identity "$identity" --output "$work/db.dump" "$file" \
  || { echo "restore: decryption failed (wrong key or damaged file)" >&2; exit 1; }
pg_restore --list "$work/db.dump" >/dev/null

if [ "$create" = 1 ]; then
  # (psql only interpolates variables from stdin/-f, not from -c)
  echo 'CREATE DATABASE :"t" OWNER :"o"' \
    | psql -v ON_ERROR_STOP=1 -d postgres -v t="$target" -v o="${RESTORE_OWNER:-aquaguard_owner}"
fi
set -- --no-owner --no-privileges --role "${RESTORE_OWNER:-aquaguard_owner}" \
  --exit-on-error --single-transaction --dbname "$target"
[ "$clean" = 1 ] && set -- "$@" --clean --if-exists
pg_restore "$@" "$work/db.dump"

# The dump carries no ACLs, and default privileges are per database (a freshly created one has
# none): re-apply the role model of infra/postgres/init.sql. Idempotent.
psql -v ON_ERROR_STOP=1 -q -d "$target" \
  -v db="$target" -v owner="${RESTORE_OWNER:-aquaguard_owner}" \
  -v app="${RESTORE_APP_USER:-aquaguard_app}" -v bkp="${RESTORE_BACKUP_USER:-aquaguard_backup}" <<'SQL'
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"app", :"bkp";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app", :"bkp";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app";
SQL
echo "restore: restored $(basename "$file") into '$target'"

#!/bin/sh
# docker-entrypoint-initdb.d hook: validate the role passwords, then apply init.sql.
# Runs only when the data directory is empty (first start). To change a password later use
# ALTER ROLE ... PASSWORD (see docs/deployment.md "Rotating secrets").
set -eu

for v in POSTGRES_PASSWORD DB_OWNER_PASSWORD DB_APP_PASSWORD DB_BACKUP_PASSWORD; do
  val=""; eval "val=\${$v:-}"
  case "$val" in
    CHANGE_ME*|"") echo "init: $v is unset or a CHANGE_ME placeholder" >&2; exit 1 ;;
  esac
  [ "${#val}" -ge 16 ] || { echo "init: $v must be at least 16 characters" >&2; exit 1; }
  # DATABASE_URL embeds the password unescaped: keep it URL-safe (make env generates hex)
  printf '%s' "$val" | grep -Eq '^[A-Za-z0-9_.~-]+$' \
    || { echo "init: $v may only contain [A-Za-z0-9_.~-]" >&2; exit 1; }
done

exec psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -f "${AQUAGUARD_INIT_SQL:-/etc/aquaguard/init.sql}"

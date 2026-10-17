#!/bin/bash
# Backup -> restore drill against a THROW-AWAY native PostgreSQL cluster. It exercises the real
# artefacts: infra/postgres/10-roles.sh + init.sql (role model), the real alembic migrations (as
# the owner role), the application role's grants, infra/backup/backup.sh (pg_dump | age) and
# infra/backup/restore.sh, then proves row counts and content hashes match and that a wrong key
# or a tampered file is refused.
#
#   sh/bash infra/backup/restore-test.sh            (make restore-test)
#
# Needs: PostgreSQL 14+ server binaries (PGBIN, auto-detected under /usr/lib/postgresql/*/bin),
# age + age-keygen, and a Python with the backend dependencies (PYTHON, default python3) to run
# `alembic upgrade head`. PostgreSQL refuses to run as root: when started as root the cluster is
# run as RESTORE_TEST_OS_USER (default: claude, else nobody) via runuser; clients stay as caller.
#   RESTORE_TEST_PORT (55432, next free port if taken)  KEEP=1 keeps the temp dir on exit
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PORT="${RESTORE_TEST_PORT:-55432}"
# first free port from RESTORE_TEST_PORT upwards (another cluster may already own 55432)
while (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; do PORT=$((PORT + 1)); done
PYTHON="${PYTHON:-python3}"
PGBIN="${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
[ -x "$PGBIN/initdb" ] || { echo "PostgreSQL server binaries not found (set PGBIN)" >&2; exit 2; }
command -v age >/dev/null && command -v age-keygen >/dev/null || { echo "age/age-keygen required" >&2; exit 2; }
export PATH="$PGBIN:$PATH"

TMP=$(mktemp -d)
RUN=""
if [ "$(id -u)" = 0 ]; then
  OSUSER="${RESTORE_TEST_OS_USER:-$(id -u claude >/dev/null 2>&1 && echo claude || echo nobody)}"
  RUN="runuser -u $OSUSER --"
  chown "$OSUSER" "$TMP"
fi
PGDATA="$TMP/pgdata"
cleanup() {
  $RUN "$PGBIN/pg_ctl" -D "$PGDATA" -m immediate stop >/dev/null 2>&1 || true
  [ "${KEEP:-0}" = 1 ] || rm -rf "$TMP"
}
trap cleanup EXIT

rnd() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
ADMIN_PW=$(rnd 16); OWNER_PW=$(rnd 16); APP_PW=$(rnd 16); BKP_PW=$(rnd 16)
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; exit 1; }

echo "== temp cluster on 127.0.0.1:$PORT ($("$PGBIN/postgres" --version)) as ${OSUSER:-$(id -un)}"
printf '%s' "$ADMIN_PW" > "$TMP/pwfile"; chmod 644 "$TMP/pwfile"
$RUN "$PGBIN/initdb" -D "$PGDATA" -U aquaguard_admin --pwfile="$TMP/pwfile" \
  --auth-local=scram-sha-256 --auth-host=scram-sha-256 -E UTF8 >/dev/null
$RUN "$PGBIN/pg_ctl" -D "$PGDATA" -w -l "$TMP/pg.log" \
  -o "-p $PORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=$TMP -c password_encryption=scram-sha-256" start >/dev/null

export PGHOST=127.0.0.1 PGPORT="$PORT"
PGPASSWORD="$ADMIN_PW" psql -qAt -U aquaguard_admin -d postgres -c 'CREATE DATABASE aquaguard' >/dev/null

echo "== roles (10-roles.sh + init.sql, as in the postgres container)"
PGPASSWORD="$ADMIN_PW" POSTGRES_USER=aquaguard_admin POSTGRES_DB=aquaguard \
  POSTGRES_PASSWORD="$ADMIN_PW" DB_OWNER_USER=aquaguard_owner DB_OWNER_PASSWORD="$OWNER_PW" \
  DB_APP_USER=aquaguard_app DB_APP_PASSWORD="$APP_PW" DB_BACKUP_USER=aquaguard_backup \
  DB_BACKUP_PASSWORD="$BKP_PW" AQUAGUARD_INIT_SQL="$ROOT/infra/postgres/init.sql" \
  sh "$ROOT/infra/postgres/10-roles.sh" >/dev/null
pass "roles created"
# a CHANGE_ME placeholder must be refused at init
if POSTGRES_USER=x POSTGRES_PASSWORD=CHANGE_ME_xxxxxxxxxxxx DB_OWNER_PASSWORD=a DB_APP_PASSWORD=a \
   DB_BACKUP_PASSWORD=a sh "$ROOT/infra/postgres/10-roles.sh" >/dev/null 2>&1; then
  fail "10-roles.sh accepted a CHANGE_ME password"; fi
pass "10-roles.sh rejects CHANGE_ME / short passwords"

URL() { echo "postgresql+asyncpg://$1:$2@127.0.0.1:$PORT/aquaguard"; }
echo "== migrations as owner, no-op re-run as app role"
( cd "$ROOT/backend" && DATABASE_URL=$(URL aquaguard_owner "$OWNER_PW") "$PYTHON" -m alembic upgrade head ) >"$TMP/alembic1.log" 2>&1 \
  || { cat "$TMP/alembic1.log"; fail "alembic upgrade as owner"; }
pass "alembic upgrade head (owner)"
( cd "$ROOT/backend" && DATABASE_URL=$(URL aquaguard_app "$APP_PW") "$PYTHON" -m alembic upgrade head ) >"$TMP/alembic2.log" 2>&1 \
  && pass "alembic at head is a no-op for the app role (entrypoint re-run is safe)" \
  || { cat "$TMP/alembic2.log"; fail "alembic as app role (backend entrypoint would fail)"; }

APP() { PGPASSWORD="$APP_PW" psql -v ON_ERROR_STOP=1 -qAt -U aquaguard_app -d aquaguard "$@"; }
if APP -c 'CREATE TABLE should_fail(x int)' >/dev/null 2>&1; then fail "app role can CREATE TABLE"; fi
if APP -c 'TRUNCATE flats CASCADE' >/dev/null 2>&1; then fail "app role can TRUNCATE"; fi
pass "app role cannot run DDL / TRUNCATE"

echo "== load data through the app role (proves DML grants)"
APP >/dev/null <<'SQL'
INSERT INTO societies (id, name, slug, total_flats)
  VALUES ('00000000-0000-0000-0000-000000000001', 'Drill Society', 'drill', 20);
INSERT INTO flats (id, society_id, unit_number, sensor_id)
  SELECT gen_random_uuid(), '00000000-0000-0000-0000-000000000001', (100 + g)::text, 'drill.' || (100 + g)
  FROM generate_series(1, 20) g;
INSERT INTO flow_readings (flat_id, "timestamp", flow_rate, volume, message_id)
  SELECT f.id, TIMESTAMPTZ '2025-01-01 00:00+00' + (m || ' minutes')::interval,
         round((random() * 12)::numeric, 3), m, lpad(to_hex(m), 8, '0')
  FROM flats f, generate_series(0, 1499) m;
SQL
SNAPSHOT_SQL="SELECT 'societies', count(*), '' FROM societies UNION ALL
 SELECT 'flats', count(*), md5(string_agg(sensor_id, ',' ORDER BY sensor_id)) FROM flats UNION ALL
 SELECT 'flow_readings', count(*), md5(string_agg(flat_id::text||'|'||message_id||'|'||flow_rate::text, ',' ORDER BY flat_id, message_id)) FROM flow_readings UNION ALL
 SELECT 'alembic_version', count(*), max(version_num) FROM alembic_version ORDER BY 1"
snapshot() { PGPASSWORD="$OWNER_PW" psql -qAt -U aquaguard_owner -d "$1" -c "$SNAPSHOT_SQL"; }
snapshot aquaguard > "$TMP/src.snap"
sed 's/^/      /' "$TMP/src.snap"
ROWS=$(awk -F'|' '$1=="flow_readings"{print $2}' "$TMP/src.snap")
[ "$ROWS" = 30000 ] || fail "expected 30000 flow_readings, got $ROWS"

echo "== backup (backup.sh as the read-only backup role) and restore"
age-keygen -o "$TMP/key.txt" 2>"$TMP/pub.txt"; RECIPIENT=$(sed -n 's/^Public key: //p' "$TMP/pub.txt")
age-keygen -o "$TMP/wrongkey.txt" 2>/dev/null
mkdir "$TMP/backups"
PGUSER=aquaguard_backup PGPASSWORD="$BKP_PW" PGDATABASE=aquaguard BACKUP_AGE_RECIPIENT="$RECIPIENT" \
  BACKUP_DIR="$TMP/backups" TMPDIR="$TMP" sh "$ROOT/infra/backup/backup.sh" once
FILE=$(ls "$TMP"/backups/aquaguard-*.dump.age)
[ -s "$FILE" ] && [ -s "$FILE.sha256" ] || fail "backup artefacts missing"
head -c 3 "$FILE" | grep -q age || fail "backup is not age-encrypted"
if grep -aq PGDMP "$FILE"; then fail "plaintext dump header visible in encrypted file"; fi
pass "encrypted backup written ($(wc -c < "$FILE") bytes), no plaintext header"

export PGUSER=aquaguard_admin PGPASSWORD="$ADMIN_PW"   # superuser: --create needs it
TMPDIR="$TMP" sh "$ROOT/infra/backup/restore.sh" --file "$FILE" --identity "$TMP/key.txt" \
  --target-db aquaguard_restored --create
snapshot aquaguard_restored > "$TMP/dst.snap"
if diff -u "$TMP/src.snap" "$TMP/dst.snap"; then pass "row counts and content hashes identical after restore"
else fail "restored data differs"; fi

OWNERS=$(PGPASSWORD="$OWNER_PW" psql -qAt -U aquaguard_owner -d aquaguard_restored -c "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='public'")
[ "$OWNERS" = aquaguard_owner ] && pass "restored tables are owned by aquaguard_owner" || fail "unexpected owners: $OWNERS"
# default privileges must have re-granted DML to the app role on the restored objects
PGPASSWORD="$APP_PW" psql -qAt -U aquaguard_app -d aquaguard_restored -c 'SELECT count(*) FROM flow_readings' >/dev/null \
  && pass "app role can read the restored tables" || fail "app role cannot read restored tables"

echo "== negative checks"
if TMPDIR="$TMP" sh "$ROOT/infra/backup/restore.sh" --file "$FILE" --identity "$TMP/wrongkey.txt" \
     --target-db aquaguard_neg1 --create >/dev/null 2>&1; then fail "wrong key accepted"; fi
pass "wrong age key refused"
cp "$FILE" "$TMP/tampered.dump.age"; cp "$FILE.sha256" "$TMP/tampered.dump.age.sha256"
sed -i "s/  .*/  tampered.dump.age/" "$TMP/tampered.dump.age.sha256"
printf 'x' >> "$TMP/tampered.dump.age"
if TMPDIR="$TMP" sh "$ROOT/infra/backup/restore.sh" --file "$TMP/tampered.dump.age" --identity "$TMP/key.txt" \
     --target-db aquaguard_neg2 --create >/dev/null 2>&1; then fail "tampered file accepted"; fi
pass "tampered backup refused (checksum)"
if TMPDIR="$TMP" sh "$ROOT/infra/backup/restore.sh" --file "$FILE" --identity "$TMP/key.txt" \
     --target-db aquaguard >/dev/null 2>&1; then fail "restore into live db without --allow-live"; fi
pass "restore into the live database requires --allow-live"

echo "RESTORE TEST PASSED (flow_readings=$ROWS)"

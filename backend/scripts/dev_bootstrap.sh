#!/bin/sh
# Local development bootstrap (NOT for production): migrate, create an admin, a society, a flat
# and device credentials, all through the real CLI / API. Nothing is seeded implicitly.
#
#   ENVIRONMENT=development DATABASE_URL=postgresql+asyncpg://... \
#   DEV_ADMIN_PASSWORD='choose-a-12+char-pass1' sh scripts/dev_bootstrap.sh
#
# The admin password comes from the environment of THIS script only and is piped to the CLI
# via stdin (never argv). Without DEV_ADMIN_PASSWORD you are prompted on the terminal.
set -eu
cd "$(dirname "$0")/.."

: "${DATABASE_URL:?set DATABASE_URL}"
SOCIETY="${DEV_SOCIETY:-dev-society}"
UNIT="${DEV_UNIT:-101}"
ADMIN="${DEV_ADMIN_USER:-admin}"
API="${DEV_API_URL:-http://127.0.0.1:8000/api/v1}"

alembic upgrade head

if [ -n "${DEV_ADMIN_PASSWORD:-}" ]; then
  printf '%s\n' "$DEV_ADMIN_PASSWORD" | python -m app.cli create-admin --username "$ADMIN" --password-stdin || true
else
  python -m app.cli create-admin --username "$ADMIN" || true
fi

cat <<MSG

Next (server running: uvicorn app.main:app_factory --factory):
  TOKEN=\$(curl -s $API/auth/login -H 'content-type: application/json' \\
      -d '{"username":"$ADMIN","password":"<your password>"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
  SID=\$(curl -s $API/societies -H "authorization: Bearer \$TOKEN" -H 'content-type: application/json' \\
      -d '{"name":"Dev Society","slug":"$SOCIETY"}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
  curl -s $API/flats -H "authorization: Bearer \$TOKEN" -H 'content-type: application/json' \\
      -d "{\"society_id\":\"\$SID\",\"unit_number\":\"$UNIT\"}"
  python -m app.cli provision-sensor --society $SOCIETY --unit $UNIT
MSG

#!/bin/sh
# Apply migrations, then hand over to uvicorn (exec so SIGTERM reaches the server and the app
# can drain gracefully).
#
#   PORT                 listen port (default 8000)
#   WEB_CONCURRENCY      uvicorn workers (default 1; >1 needs RATE_LIMIT_STORAGE_URI=redis://...)
#   FORWARDED_ALLOW_IPS  proxies whose X-Forwarded-* headers are trusted (default 127.0.0.1;
#                        a wildcard is refused: any client could then spoof its IP)
set -e
PORT="${PORT:-8000}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-1}"
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
case "$FORWARDED_ALLOW_IPS" in
  *'*'*) echo "FORWARDED_ALLOW_IPS must list proxy IPs, not '*'" >&2; exit 1 ;;
esac
case "$WEB_CONCURRENCY" in
  ''|*[!0-9]*) echo "WEB_CONCURRENCY must be a positive integer" >&2; exit 1 ;;
esac
export WEB_CONCURRENCY FORWARDED_ALLOW_IPS

alembic upgrade head
exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port "$PORT" \
  --workers "$WEB_CONCURRENCY" --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS" \
  --timeout-graceful-shutdown 30

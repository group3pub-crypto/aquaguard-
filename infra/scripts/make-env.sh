#!/bin/sh
# Create .env from .env.example with fresh random secrets. NEVER overwrites an existing file.
#   sh infra/scripts/make-env.sh [ENV_FILE]      (default: .env)
# Every `NAME=CHANGE_ME` line gets a fresh random value from openssl: base64url (64 chars) for
# SECRET_KEY, hex for the rest (URL-safe, which the database URL requires).
set -eu
umask 077
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TARGET="${1:-$ROOT/.env}"
EXAMPLE="$ROOT/.env.example"

if [ -e "$TARGET" ]; then
  echo "$TARGET exists, leaving it untouched"
  exit 0
fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

tmp=$(mktemp "$TARGET.XXXXXX")
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    [A-Z]*=CHANGE_ME)
      name=${line%%=*}
      case "$name" in
        # base64url: hex has only 16 symbols and the backend demands >= 16 DISTINCT characters
        SECRET_KEY) val=$(openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_') ;;
        MQTT_DEVICE_SECRET) val=$(openssl rand -hex 32) ;;
        METRICS_TOKEN) val=$(openssl rand -hex 24) ;;
        *) val=$(openssl rand -hex 16) ;;
      esac
      printf '%s=%s\n' "$name" "$val" ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$EXAMPLE" > "$tmp"
chmod 600 "$tmp"
# link(2) fails if TARGET appeared meanwhile: no overwrite race
ln "$tmp" "$TARGET" 2>/dev/null || { echo "$TARGET appeared meanwhile, not overwriting" >&2; exit 1; }
echo "created $TARGET (mode 600) with generated secrets."
echo "next: set PUBLIC_DOMAIN / CADDY_TLS, then 'make check-env'; TELEGRAM_BOT_TOKEN and BACKUP_AGE_RECIPIENT are yours to supply."

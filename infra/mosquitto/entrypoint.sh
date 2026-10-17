#!/bin/sh
# Render the broker password file, ACL and config from environment, then exec mosquitto.
#
#   MQTT_USERNAME / MQTT_PASSWORD         backend service account (readwrite aquaguard/#)   [required]
#   MQTT_DEVICES                          comma/space separated "<society-slug>/<unit>" list
#   MQTT_DEVICES_FILE                     same, one per line (use for hundreds of devices)
#   MQTT_DEVICE_SECRET                    >= 32 chars; required if any device is listed. The password
#                                         of device "<slug>/<unit>" is the first 32 hex chars of
#                                         sha256("<secret>:<slug>/<unit>") (see device_password.sh)
#   MQTT_CONTROL_USERNAME / _PASSWORD     optional simulator control client (read aquaguard/control/#)
#   MQTT_LISTEN_ADDR (127.0.0.1) MQTT_LISTEN_PORT (1883)   plain-text listener for the backend only;
#                                         set MQTT_LISTEN_ADDR=0.0.0.0 inside a container whose
#                                         network is private (compose) - never publish it
#   MQTT_TLS_CERT_FILE / MQTT_TLS_KEY_FILE  enable the TLS listener for devices (TLS >= 1.2,
#                                         username/password + ACL, no client certificate required);
#                                         MQTT_TLS_CA_FILE (optional chain), MQTT_TLS_PORT (8883),
#                                         MQTT_TLS_ADDR (0.0.0.0)
#   MQTT_RUNTIME_DIR (/tmp/mosquitto)  MOSQUITTO_CONFIG_DIR (dir of this script)
#   MQTT_DATA_DIR (/mosquitto/data)
#   MOSQUITTO_BIN (mosquitto)  MQTT_RENDER_ONLY=1 (render files, do not start the broker)
set -eu
umask 077

CONF_DIR="${MOSQUITTO_CONFIG_DIR:-$(cd "$(dirname "$0")" && pwd)}"
RUN_DIR="${MQTT_RUNTIME_DIR:-/tmp/mosquitto}"
NAME_RE='^[A-Za-z0-9_.-]{1,100}$'

die() { echo "mosquitto entrypoint: $*" >&2; exit 1; }
valid() { printf '%s' "$1" | grep -Eq "$NAME_RE"; }

: "${MQTT_USERNAME:?MQTT_USERNAME is required}"
: "${MQTT_PASSWORD:?MQTT_PASSWORD is required}"
valid "$MQTT_USERNAME" || die "MQTT_USERNAME must match $NAME_RE"
case "$MQTT_PASSWORD" in CHANGE_ME*) die "refusing placeholder MQTT_PASSWORD";; esac
[ "${#MQTT_PASSWORD}" -ge 12 ] || die "MQTT_PASSWORD must be at least 12 characters"
case "$MQTT_PASSWORD" in *'
'*) die "MQTT_PASSWORD must not contain newlines";; esac

mkdir -p "$RUN_DIR"
PLAIN="$RUN_DIR/passwd.plain"
printf '%s:%s\n' "$MQTT_USERNAME" "$MQTT_PASSWORD" > "$PLAIN"

ACL="$RUN_DIR/acl"
sed "s|__BACKEND_USER__|$MQTT_USERNAME|g" "$CONF_DIR/acl.template" > "$ACL.head"

CONTROL=""
if [ -n "${MQTT_CONTROL_USERNAME:-}" ]; then
  : "${MQTT_CONTROL_PASSWORD:?MQTT_CONTROL_PASSWORD is required with MQTT_CONTROL_USERNAME}"
  valid "$MQTT_CONTROL_USERNAME" || die "MQTT_CONTROL_USERNAME must match $NAME_RE"
  [ "${#MQTT_CONTROL_PASSWORD}" -ge 12 ] || die "MQTT_CONTROL_PASSWORD must be at least 12 characters"
  printf '%s:%s\n' "$MQTT_CONTROL_USERNAME" "$MQTT_CONTROL_PASSWORD" >> "$PLAIN"
  CONTROL=$(sed "s|__CONTROL_USER__|$MQTT_CONTROL_USERNAME|g" "$CONF_DIR/acl.control.template")
fi
awk -v block="$CONTROL" '$0=="__CONTROL_BLOCK__"{ if (block!="") print block; next } {print}' \
  "$ACL.head" > "$ACL"
rm -f "$ACL.head"

DEVICES_RAW="$(printf '%s\n' "${MQTT_DEVICES:-}" | tr ', ' '\n\n')"
if [ -n "${MQTT_DEVICES_FILE:-}" ]; then
  [ -r "$MQTT_DEVICES_FILE" ] || die "cannot read MQTT_DEVICES_FILE=$MQTT_DEVICES_FILE"
  DEVICES_RAW="$DEVICES_RAW
$(cat "$MQTT_DEVICES_FILE")"
fi
SECRET="${MQTT_DEVICE_SECRET:-}"
n=0
for dev in $(printf '%s\n' "$DEVICES_RAW" | sed 's/#.*//' | tr -s ' \t\n' '\n\n\n'); do
  [ -n "$dev" ] || continue
  society=${dev%%/*}
  unit=${dev#*/}
  [ "$society/$unit" = "$dev" ] || die "bad device '$dev' (expected <society>/<unit>)"
  valid "$society" && valid "$unit" || die "bad device '$dev': segments must match $NAME_RE"
  [ "$society" != "control" ] || die "society slug 'control' is reserved"
  [ "${#SECRET}" -ge 32 ] || die "MQTT_DEVICE_SECRET (>= 32 chars) is required when devices are listed"
  pw=$(printf '%s:%s' "$SECRET" "$dev" | sha256sum | cut -c1-32)
  printf '%s:%s\n' "$dev" "$pw" >> "$PLAIN"
  sed -e "s|__DEVICE_USER__|$dev|g" -e "s|__SOCIETY__|$society|g" -e "s|__UNIT__|$unit|g" \
    "$CONF_DIR/acl.device.template" >> "$ACL"
  n=$((n + 1))
done

# hash the plaintext file in place (mosquitto_passwd -U); the plaintext never survives
mv "$PLAIN" "$RUN_DIR/passwd"
mosquitto_passwd -U "$RUN_DIR/passwd" >/dev/null

DATA_DIR="${MQTT_DATA_DIR:-/mosquitto/data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true
LISTENERS="listener ${MQTT_LISTEN_PORT:-1883} ${MQTT_LISTEN_ADDR:-127.0.0.1}
protocol mqtt"
TLS=no
if [ -n "${MQTT_TLS_CERT_FILE:-}${MQTT_TLS_KEY_FILE:-}" ]; then
  [ -r "${MQTT_TLS_CERT_FILE:-}" ] || die "cannot read MQTT_TLS_CERT_FILE"
  [ -r "${MQTT_TLS_KEY_FILE:-}" ] || die "cannot read MQTT_TLS_KEY_FILE"
  # copy into the (mosquitto-owned, 0700) runtime dir: independent of mount permissions and
  # of whether the broker reads key material before or after dropping privileges
  cp "$MQTT_TLS_CERT_FILE" "$RUN_DIR/tls-cert.pem"
  cp "$MQTT_TLS_KEY_FILE" "$RUN_DIR/tls-key.pem"
  CAFILE=""
  if [ -n "${MQTT_TLS_CA_FILE:-}" ]; then
    [ -r "$MQTT_TLS_CA_FILE" ] || die "cannot read MQTT_TLS_CA_FILE"
    cp "$MQTT_TLS_CA_FILE" "$RUN_DIR/tls-ca.pem"
    CAFILE="cafile $RUN_DIR/tls-ca.pem"
  fi
  LISTENERS="$LISTENERS

listener ${MQTT_TLS_PORT:-8883} ${MQTT_TLS_ADDR:-0.0.0.0}
protocol mqtt
certfile $RUN_DIR/tls-cert.pem
keyfile $RUN_DIR/tls-key.pem
$CAFILE
require_certificate false"
  TLS=yes
fi
sed -e "s|__RUNTIME_DIR__|$RUN_DIR|g" -e "s|__DATA_DIR__|${DATA_DIR%/}|g" \
    "$CONF_DIR/mosquitto.conf.template" \
  | awk -v block="$LISTENERS" '$0=="__LISTENERS__"{print block; next} {print}' \
  > "$RUN_DIR/mosquitto.conf"

# mosquitto drops privileges to the "mosquitto" user when started as root
if [ "$(id -u)" = 0 ] && id mosquitto >/dev/null 2>&1; then
  chown mosquitto:mosquitto "$RUN_DIR" "$RUN_DIR"/* 2>/dev/null || true
  chown mosquitto:mosquitto "$DATA_DIR" 2>/dev/null || true
fi
chmod 0700 "$RUN_DIR"; chmod 0600 "$RUN_DIR"/*

echo "mosquitto entrypoint: rendered $RUN_DIR (devices=$n tls=$TLS control=$([ -n "$CONTROL" ] && echo yes || echo no))" >&2
[ "${MQTT_RENDER_ONLY:-0}" = 1 ] && exit 0
exec "${MOSQUITTO_BIN:-mosquitto}" -c "$RUN_DIR/mosquitto.conf"

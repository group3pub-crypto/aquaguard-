#!/bin/sh
# Generate the password file from env at container start, then exec mosquitto.
set -eu
: "${MQTT_USERNAME:?MQTT_USERNAME is required}"
: "${MQTT_PASSWORD:?MQTT_PASSWORD is required}"
case "$MQTT_PASSWORD" in CHANGE_ME*) echo "refusing placeholder MQTT_PASSWORD" >&2; exit 1;; esac

mkdir -p /tmp/mosquitto
: > /tmp/mosquitto/passwd
mosquitto_passwd -b /tmp/mosquitto/passwd "$MQTT_USERNAME" "$MQTT_PASSWORD"
chown mosquitto:mosquitto /tmp/mosquitto/passwd 2>/dev/null || true
chmod 0700 /tmp/mosquitto/passwd
chown mosquitto:mosquitto /mosquitto/data 2>/dev/null || true

exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf

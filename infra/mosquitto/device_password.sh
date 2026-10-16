#!/bin/sh
# Print the broker password of a device: device_password.sh <society-slug>/<unit>
# Needs MQTT_DEVICE_SECRET (the same value the broker container was started with).
set -eu
: "${MQTT_DEVICE_SECRET:?MQTT_DEVICE_SECRET is required}"
printf '%s:%s' "$MQTT_DEVICE_SECRET" "$1" | sha256sum | cut -c1-32

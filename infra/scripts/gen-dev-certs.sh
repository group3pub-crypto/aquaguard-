#!/bin/sh
# Generate a LOCAL, throw-away CA and a server certificate for the Mosquitto TLS listener.
# For development and tests only - never use these for production traffic.
#
#   sh infra/scripts/gen-dev-certs.sh [OUT_DIR]      (default: ./.dev-certs, git-ignored)
#   MQTT_TLS_SANS="DNS:broker.example,IP:10.0.0.5"  extra subjectAltNames
#
# Writes ca.crt (give to clients: mosquitto_pub --cafile ca.crt, aiomqtt tls_params),
# server.crt and server.key (give to the broker: MQTT_TLS_CERT_FILE / MQTT_TLS_KEY_FILE).
set -eu
umask 077
OUT="${1:-.dev-certs}"
SANS="DNS:localhost,DNS:mosquitto,IP:127.0.0.1${MQTT_TLS_SANS:+,$MQTT_TLS_SANS}"
mkdir -p "$OUT"
openssl genrsa -out "$OUT/ca.key" 3072 2>/dev/null
openssl req -x509 -new -key "$OUT/ca.key" -sha256 -days 30 -subj "/CN=AquaGuard local dev CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out "$OUT/ca.crt"
openssl genrsa -out "$OUT/server.key" 2048 2>/dev/null
openssl req -new -key "$OUT/server.key" -subj "/CN=aquaguard-mosquitto" -out "$OUT/server.csr"
printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SANS" > "$OUT/server.ext"
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -days 30 -sha256 -extfile "$OUT/server.ext" -out "$OUT/server.crt" 2>/dev/null
rm -f "$OUT/server.csr" "$OUT/server.ext" "$OUT/ca.srl"
chmod 0644 "$OUT/ca.crt" "$OUT/server.crt"; chmod 0600 "$OUT/ca.key" "$OUT/server.key"
echo "wrote $OUT/{ca.crt,server.crt,server.key} (SAN: $SANS; valid 30 days; CA key stays local)"

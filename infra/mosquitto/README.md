# Mosquitto (auth + per-device ACL)

Nothing secret is stored here. `entrypoint.sh` renders `passwd` (hashed), `acl` and
`mosquitto.conf` into `$MQTT_RUNTIME_DIR` (default `/tmp/mosquitto`, a tmpfs in compose) from
environment variables and the `*.template` files, then `exec`s mosquitto. `allow_anonymous false`.

| Env | Meaning |
|---|---|
| `MQTT_USERNAME` / `MQTT_PASSWORD` | Backend account: `readwrite aquaguard/#`. Password >= 12 chars, not `CHANGE_ME*`. |
| `MQTT_DEVICES` / `MQTT_DEVICES_FILE` | Devices as `<society-slug>/<unit>` (comma/space list, or one per line in a file). |
| `MQTT_DEVICE_SECRET` | >= 32 chars. Device password = first 32 hex of `sha256("<secret>:<slug>/<unit>")`. |
| `MQTT_CONTROL_USERNAME` / `_PASSWORD` | Optional simulator control client: `read aquaguard/control/#` only. |
| `MQTT_RUNTIME_DIR`, `MQTT_DATA_DIR`, `MQTT_LISTEN_ADDR`, `MQTT_LISTEN_PORT`, `MOSQUITTO_BIN`, `MQTT_RENDER_ONLY=1` | Native/CI runs. |

## ACL model

* Device user `<slug>/<unit>` (MQTT username == path prefix): `write` to
  `aquaguard/<slug>/<unit>/{flow,heartbeat,valve/state}`, `read` of `aquaguard/<slug>/<unit>/valve/cmd`.
  Nothing else (not other flats, not `control/#`, not its own `valve/cmd` for writing).
* Unlisted users have no rights (mosquitto default deny). Names/segments must match
  `[A-Za-z0-9_.-]{1,100}`, so wildcards/newlines cannot be injected into the ACL; slug `control` is reserved.
* Mosquitto 2 drops unauthorised PUBLISHes silently (MQTT 3.1.1 has no negative ack for QoS 0/1
  publish ACL failures); verify with a privileged subscriber, as in the integration test.
* Get a device password: `MQTT_DEVICE_SECRET=... sh infra/mosquitto/device_password.sh green-valley/101`.

Native example: `MQTT_RUNTIME_DIR=/tmp/aq MQTT_DATA_DIR=/tmp/aq/data MQTT_LISTEN_ADDR=127.0.0.1 MQTT_LISTEN_PORT=51883 MQTT_USERNAME=... MQTT_PASSWORD=... sh infra/mosquitto/entrypoint.sh`.
Rotating `MQTT_DEVICE_SECRET` and restarting the broker rotates every device password.

## TLS (devices) and listeners

* `1883` plain MQTT: bound to `MQTT_LISTEN_ADDR` (default **127.0.0.1**) - for the backend only. Compose sets
  `0.0.0.0` *inside* the container because the backend reaches it over the private compose network; the
  published host port is `127.0.0.1:1883`.
* `8883` TLS: enabled when `MQTT_TLS_CERT_FILE` + `MQTT_TLS_KEY_FILE` are set (`MQTT_TLS_CA_FILE` optional).
  Username/password + the same ACL; `require_certificate false`. The cert files are copied into the
  broker's private runtime dir. TLS 1.0/1.1 are rejected and 1.2/1.3 accepted (verified with
  `openssl s_client` against Mosquitto 2.0.18 on Ubuntu's OpenSSL 3.0; the stock `eclipse-mosquitto`
  image was not tested).
* Dev certificates: `sh infra/scripts/gen-dev-certs.sh [dir]` (local CA + server cert, 30 days, output is
  git-ignored). Clients: `mosquitto_pub --cafile ca.crt -h localhost -p 8883 ...`, backend
  `MQTT_CA_FILE`, simulator `--tls-ca` / `MQTT_CA_FILE` (the broker-side `MQTT_TLS_CA_FILE` is a different thing).

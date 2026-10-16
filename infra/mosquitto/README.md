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

# MQTT contract

This page describes what the backend actually does (`backend/app/mqtt`, `backend/app/services/ingest.py`,
`backend/app/schemas/mqtt.py`, `backend/app/services/valve_control.py`). Broker setup and the ACL
templates live in `infra/mosquitto/` (see its README).

## Topics

`{slug}` is the society slug, `{unit}` the flat's unit number; both match `[A-Za-z0-9_.-]{1,100}`
and `control` is a reserved slug.

| Topic | Direction | QoS | Retain | Payload |
|---|---|---|---|---|
| `aquaguard/{slug}/{unit}/flow` | device -> backend | 1 | no | flow reading (below) |
| `aquaguard/{slug}/{unit}/heartbeat` | device -> backend | 1 | no | `{"v":1,"ts","uptime"?,"bat"?,"fw"?}` |
| `aquaguard/{slug}/{unit}/valve/state` | device -> backend | 1 | **yes** (firmware) | `{"v":1,"cmd_id","state":"open\|closed","ts"}` |
| `aquaguard/{slug}/{unit}/valve/cmd` | backend -> device | 1 | no | `{"v":1,"id","cmd":"open\|close","src","ts"}` |

The backend subscribes (QoS 1) to `aquaguard/+/+/flow`, `+/+/heartbeat` and `+/+/valve/state`, and
publishes only `valve/cmd`. **There are no control topics**: the former `aquaguard/control/fault` and
`aquaguard/control/speed` (simulator fault injection) were removed from the backend; the backend neither
publishes nor subscribes to anything under `aquaguard/control/`, and a topic with the slug `control` is
rejected (`unrecognised_topic`). Anything else (wrong depth, unknown kind, bad characters) is dropped and
shown in `GET /health/details` -> `dead_letters`.

## Flow payload

```json
{"v":1,"id":"a1b2c3d4","ts":1760000000,"flow":0.4,"vol":12.3,"psi":null,"tmp":null,"bat":87,"rssi":-61,
 "tot":98765.4,"vbat":4.01,"valve":"open","fw":"2.0.0-dev","err":["bat_low"]}
```

| Field | Type / range | Notes |
|---|---|---|
| `v` | `1` | required |
| `id` | 8 hex chars | required; with the flat it is the idempotency key `(flat, id)` |
| `ts` | epoch seconds, > 0 | required; see "Freshness" |
| `flow` | 0..500 L/min | required |
| `vol` | 0..100000 L | required; litres since boot |
| `psi` / `tmp` / `bat` / `rssi` | optional (`null` ok) | `bat` 0..100 (decimals rounded), `tmp` -50..150, `psi` 0..500, `rssi` -150..0 |
| `tot` | 0..1e9 L | optional; lifetime litres |
| `vbat` | 2.5..4.5 V | optional |
| `valve` | `open\|closed` | optional; the device's own view of its valve |
| `fw` | string <= 32 | optional firmware version |
| `err` | up to 8 strings (1..24 chars) | optional; currently active fault codes |

Unknown keys are ignored; payloads larger than 2048 bytes are rejected (`oversize_payload`); invalid
payloads are rejected with `invalid_payload:<field>`. Every accepted reading is stored in
`flow_readings` including the optional fields (`total_volume`, `battery_volts`, `device_valve_state`,
`firmware_version`, `fault_codes`). The newest values are also kept in `device_status` (one row per
flat) and exposed to staff in `GET /api/v1/flats/{id}` (`device`) and
`GET /api/v1/societies/{id}/devices`.

### Device error codes (`err`)

A code that is newly present in a report (it was not in the previous one) is recorded in the audit log
(`device_fault`), counted (`device_fault` in `/health/details` ingest counters) and broadcast on the
WebSocket as `{"type":"device_fault","flat_id","code","severity"}`; `sensor` events carry the new
`device` snapshot whenever firmware, valve position or error list changed.

| Severity | Codes |
|---|---|
| critical | `close_unconf` (close commanded but flow did not stop: *check the valve on site*), `flow_impossible`, `stuck_line`, `adc_fault`, `sensor_fault`, `coil_duty`, `bat_crit` |
| warning | `bat_low`, `batt_low`, `ts_est`, `time_invalid`, `backfill_drop`, `store_fail`, `rst_brownout`, `rst_wdt` |
| stored only | `burst`, `leak_cont`, `leak_night` (the device's local detectors; the backend raises its own alerts) |

A `heartbeat` with `fw`/`bat` updates the same status row but never the error list.

## Freshness, duplicates and rate limiting

* **Stale readings are rejected**: `ts` older than `MQTT_MAX_READING_AGE_S` (default **600 s**) ->
  `stale_reading` (counter `stale_reading`). **Future** `ts` beyond `MQTT_MAX_FUTURE_SKEW_S` (default 300 s)
  -> `invalid_payload:ts`. Detection windows run on server time, never on a device-chosen `ts`.
* **Backfill**: the firmware replays buffered readings after an outage only if they are at most
  `MAX_BACKFILL_S` = 540 s old (inside the 600 s window); older ones are dropped on the device and flagged
  `backfill_drop`. There is deliberately **no MQTT path for history import**; an admin-only service
  function (`ingest.backfill_readings`, audited, not exposed on MQTT) exists for that.
* **Duplicates** (QoS 1 redelivery, double publish) are absorbed by the unique `(flat, id)` key
  (`flow_duplicate` counter); a duplicate does not trigger detection or WebSocket events.
* **Per-device rate limit**: token bucket per `(slug, unit)`, `MQTT_DEVICE_RATE_PER_MIN` (default 120/min,
  burst `max(10, rate/4)`, `0` disables). Excess messages are dropped silently apart from the
  `rate_limited` counter.
* An unknown flat (no such `slug`/`unit`) is rejected (`unknown_flat`).

## Delivery guarantees

The backend connects with a persistent session (`clean_session=false`, client id `MQTT_CLIENT_ID`) and
acknowledges a QoS 1 message to the broker only **after** its handler finished (manual ack), so a crash
mid-processing leads to redelivery, and messages published while the backend is down are queued by the
broker. Delivery is at-least-once; storage is idempotent. The broker's `max_queued_messages` bounds the
backlog. Valve commands are published QoS 1, not retained.

## Valve commands and acknowledgements

`valve/cmd`: `id` is the first 8 hex chars of the command's UUID in `valve_commands`, `src` is
`auto | manual_dashboard | manual_bot | manual_override`, `ts` epoch seconds. The command row is
**committed before it is published** (outbox style); a publish failure marks it `failed`.

`valve/state` (the ack) echoes `cmd_id`:

* it **confirms** the matching pending command only if `state` is the one the command asked for
  (`close` -> `closed`, `open` -> `open`); a different state marks the command `failed`;
* with no ack within `VALVE_ACK_TIMEOUT_S` (60 s) the command becomes `timeout`; a **late** ack of a timed-out command
  still counts (the valve did move);
* an ack with an unknown or old `cmd_id` (also the retained message re-delivered on every backend
  subscribe) only updates the flat's `valve_state` and is audited as `matched: false`;
* only a confirmed **automatic close** moves an alert to tier 4 (`auto_shutoff`); until then the alert
  stays at `ticket`. Automatic closing additionally requires the per-flat commissioning gate
  (`PUT /api/v1/flats/{id}/auto-shutoff-validation`, admin) and is never attempted otherwise.
* The device-side checks (freshness, anti-replay, source policy) are described in
  `docs/hardware/valve-safety.md`.

## Authentication, ACL, TLS

* `allow_anonymous false`. The backend has its own account (`MQTT_USERNAME`/`MQTT_PASSWORD`,
  `readwrite aquaguard/#`).
* **Per-device users**: the broker username is `{slug}/{unit}` (same string the edge simulator uses; the
  password is derived from `MQTT_DEVICE_SECRET` by `infra/mosquitto/device_password.sh`, or issued
  individually with `python -m app.cli provision-sensor`, which prints the `mosquitto_passwd` command and
  the ACL lines in exactly the format of `infra/mosquitto/acl.device.template`). A device may **write**
  its own `flow`, `heartbeat`, `valve/state` and **read** its own `valve/cmd`; nothing else (not other
  flats, not its own `valve/cmd` for writing, not `control/#`). Mosquitto drops unauthorised publishes
  silently. This is a different identifier from the flat's `sensor_id` (an inventory label).
* **TLS**: devices use the TLS listener (port 8883, TLS >= 1.2, username/password + the same ACL) when
  `MQTT_TLS_CERT_FILE`/`MQTT_TLS_KEY_FILE` are set on the broker; plain 1883 is for the backend on the
  private network (published on 127.0.0.1 only). The backend verifies a TLS broker against `MQTT_CA_FILE`.
  See `infra/mosquitto/README.md` and `docs/hardware/mqtt-tls-setup.md`.

`infra/loadtest/load_test.py` publishes `flow` messages on these topics.

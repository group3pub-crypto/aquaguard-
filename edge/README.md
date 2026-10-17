# AquaGuard edge

Two things live here:

* `firmware/aquaguard_sensor/` - the ESP32 firmware (YF-S201 flow sensor + valve relay). See
  `docs/hardware/` for wiring, valve safety, provisioning and the HIL test plan.
* `simulator/` - a **development and test tool only**: it emulates sensors over MQTT so the backend,
  dashboard and ML can be exercised without hardware. It is never part of a production deployment, and
  **all its data is synthetic**. The backend has no demo or fault-injection code of its own.

```bash
pip install -e edge[dev]            # aiomqtt, pydantic, numpy
python -m edge.simulator --society green-valley --units 101,102 --speed 1 --broker localhost:1883
pytest edge/tests                   # pure-logic tests, no broker needed
```

Options: `--seed`, `--unit-base 101` (flat units are 101, 102, ...), `--flat-map '{"101":"<flat uuid>"}'`
(or env `AQUAGUARD_FLAT_MAP`), `--username/--password` (or `MQTT_USERNAME`/`MQTT_PASSWORD`),
`--device-secret` (per-device broker accounts, see below), `--tls-ca` (TLS, broker port 8883).
Docker: `docker build -t aquaguard-edge edge/` (python:3.11-slim, non-root).

## Topics

The wire contract is `docs/mqtt.md`; the simulator follows it.

| Direction | Topic | Payload |
|---|---|---|
| out, QoS1 | `aquaguard/{society}/{unit}/flow` (every simulated minute) | `{"v":1,"id":"<8hex>","ts":epoch_s,"flow":L/min,"vol":litres since start,"psi":null,"tmp":null,"bat":%,"rssi":dBm}` (no optional firmware extras) |
| out, QoS1 | `.../heartbeat` (every 5 simulated min) | `{"v":1,"ts","uptime","bat","fw":"1.2.0"}` |
| in | `.../valve/cmd` | `{"v":1,"id","cmd":"open\|close","src","ts"}` |
| out, QoS1, retained | `.../valve/state` | `{"v":1,"cmd_id","state":"open\|closed","ts"}` (closed => flow 0) |
| in, simulator only | `aquaguard/control/fault` | `{"flat_id"\|"unit", "fault_type", "params"?, "action":"inject\|clear"}` |
| in, simulator only | `aquaguard/control/speed` | `{"speed": 60}` |

The `control/*` topics exist **only between an operator and the simulator**. The backend neither
publishes nor subscribes to them, the device ACL does not allow them, and a production broker needs no
control user. Per-device accounts: with `--device-secret` (same value as the broker's
`MQTT_DEVICE_SECRET`) each simulated flat connects as `<society>/<unit>` with the derived password,
exactly like a real device; `--username/--password` then only serve the control topics.

- Faults: `slow_leak`, `running_toilet`, `dripping_tap`, `burst`, `overuse`. `params`:
  `rate` (L/min added; multiplier for overuse), `duration_min` (default: until cleared).
  `clear` without `fault_type` removes all faults of that flat. `flat_id` may be a backend id from
  `--flat-map`, a unit number, or `"all"`.
- Speed: 1 = realtime, 60 = one simulated hour per real minute. Timestamps are *simulated* time
  (start = now), so at speed > 1 they run ahead of wall clock; a backend that rejects future
  `ts` must be told. After a stall/outage at most 500 simulated minutes are back-filled.
- Reconnects with exponential backoff (1 -> 30 s); SIGINT/SIGTERM shut down cleanly.
- `id` is unique per flat for 2^32 messages (dedupe key); `bat` is percent (slow linear drain).

## Honest limits

The simulator was exercised against a real Mosquitto only through the backend's integration tests
(`backend/tests/integration`); its data is synthetic and is not evidence about real plumbing. Valve
`state` is not published at boot (no cmd_id to correlate).

The firmware in `firmware/aquaguard_sensor/` is a reference implementation, not a certified product. It
uses `espMqttClient` (QoS 1 for `flow`, `heartbeat` and the retained `valve/state`; TLS on port 8883 with
a pinned CA PEM, see `docs/hardware/mqtt-tls-setup.md`), persists the lifetime total and message-id
counter, replays buffered readings no older than 540 s, and reports `tot`, `vbat`, `valve`, `fw` and
`err` codes that the backend stores and raises alerts for. There is no OTA, the YF-S201 constant (7.5
Hz per L/min) needs per-unit calibration, and valve position is the *commanded* state (no feedback).
Copy `config.h.example` to `config.h` (git-ignored) for credentials; set `RELAY_ACTIVE_LOW` to match
your relay board (`1` if it energises on a LOW input) - the old `VALVE_CLOSE_LEVEL` setting no longer
exists. Automatic closing is additionally gated per flat on the backend
(`PUT /api/v1/flats/{id}/auto-shutoff-validation`).

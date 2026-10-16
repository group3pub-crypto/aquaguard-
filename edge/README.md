# AquaGuard edge simulator

Simulates the in-flat flow sensors (ESP32 + YF-S201 + solenoid valve) over MQTT so the backend,
dashboard and ML can be demoed without hardware. **All data is synthetic.**

```bash
pip install -e edge[dev]            # aiomqtt, pydantic, numpy
python -m edge.simulator --society demo --flats 6 --speed 60 --broker localhost:1883
pytest edge/tests                   # pure-logic tests, no broker needed
```

Options: `--seed`, `--unit-base 101` (flat units are 101, 102, ...), `--flat-map '{"101":"<flat uuid>"}'`
(or env `AQUAGUARD_FLAT_MAP`), `--username/--password` (or `MQTT_USERNAME`/`MQTT_PASSWORD`).
Docker: `docker build -t aquaguard-edge edge/` (python:3.11-slim, non-root).

## Topics

| Direction | Topic | Payload |
|---|---|---|
| out, QoS1 | `aquaguard/{society}/{unit}/flow` (every simulated minute) | `{"v":1,"id":"<8hex>","ts":epoch_s,"flow":L/min,"vol":cumulative L,"psi":null,"tmp":null,"bat":%,"rssi":dBm}` |
| out, QoS1 | `.../heartbeat` (every 5 simulated min) | `{"v":1,"ts","uptime","bat","fw":"1.2.0"}` |
| in | `.../valve/cmd` | `{"v":1,"id","cmd":"open\|close","src","ts"}` |
| out, retained | `.../valve/state` | `{"v":1,"cmd_id","state":"open\|closed","ts"}` (closed => flow 0) |
| in | `aquaguard/control/fault` | `{"flat_id"\|"unit", "fault_type", "params"?, "action":"inject\|clear"}` |
| in | `aquaguard/control/speed` | `{"speed": 60}` |

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

Wire behaviour against a real broker was exercised only through the broker-independent core and a
connection-refused/backoff smoke test; run it against your Mosquitto before relying on it. Valve
`state` is not published at boot (no cmd_id to correlate). The reference sketch in
`firmware/aquaguard_sensor/` is illustrative: PubSubClient publishes QoS 0, there is no TLS/OTA,
and the YF-S201 constant (7.5 Hz per L/min) needs per-unit calibration. Copy `config.h.example`
to `config.h` (git-ignored) for credentials.

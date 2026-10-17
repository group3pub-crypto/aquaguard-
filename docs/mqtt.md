# MQTT

Broker: Mosquitto 2, `allow_anonymous false`, username/password from `MQTT_USERNAME` / `MQTT_PASSWORD`. Port 1883 bound to 127.0.0.1. No TLS or per-topic ACLs in the default setup (see `docs/limitations.md`).

| Topic | Direction | QoS | Retain | Payload |
|---|---|---|---|---|
| `aquaguard/{society_slug}/{flat_unit}/flow` | device -> backend | 1 | no | flow reading |
| `aquaguard/{society_slug}/{flat_unit}/heartbeat` | device -> backend | 1 | no | `{"v":1,"ts","uptime","bat","fw"}` |
| `aquaguard/{society_slug}/{flat_unit}/valve/cmd` | backend -> device | 1 | no | `{"v":1,"id","cmd":"open\|close","src","ts"}` |
| `aquaguard/{society_slug}/{flat_unit}/valve/state` | device -> backend | 1 | no | `{"v":1,"cmd_id","state":"open\|closed","ts"}` |
| `aquaguard/control/fault` | operator -> simulator | 1 | no | simulator fault injection |
| `aquaguard/control/speed` | operator -> simulator | 1 | no | simulator speed control |

Flow payload (`psi`, `tmp`, `bat`, `rssi` may be `null`; `tot`, `vbat`, `valve`, `fw`, `err` are optional firmware extras, persisted alongside the rest):

```json
{"v":1,"id":"a1b2c3d4","ts":1760000000,"flow":0.4,"vol":1234.5,"psi":null,"tmp":null,"bat":87,"rssi":-61,"tot":1234.5,"valve":"open","fw":"1.2.0"}
```

`flow` is L/min, `vol` litres since boot, `bat` 0-100, `ts` epoch seconds, `id` an 8-hex message id (useful for de-duplication). `tot` is the device's persisted lifetime litre count (survives reboots, unlike `vol`); `vbat` is raw battery volts when that ADC is fitted; `valve` is the device's own view of its valve position; `fw` is the firmware version; `err` (when present) is a list of up to 8 short fault codes (e.g. `stuck_line`, `adc_fault`) the device is currently reporting. Readings are not retained so stale values are never replayed. QoS 1 is at-least-once; consumers must tolerate duplicates. `infra/loadtest/load_test.py` publishes `flow` messages on these topics.

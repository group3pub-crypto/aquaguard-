# MQTT

Broker: Mosquitto 2, `allow_anonymous false`, username/password from `MQTT_USERNAME` / `MQTT_PASSWORD`. Port 1883 bound to 127.0.0.1. No TLS or per-topic ACLs in the default setup (see `docs/limitations.md`).

| Topic | Direction | QoS | Retain | Payload |
|---|---|---|---|---|
| `aquaguard/<society>/<unit>/telemetry` | device -> backend | 1 | no | JSON telemetry |
| `aquaguard/<society>/<unit>/status` | device -> backend | 1 | yes | `{"online":bool,"fw":str}` (last-will sets offline) |
| `aquaguard/<society>/<unit>/command` | backend -> device | 1 | no | `{"cmd":"valve","action":"open"\|"close","id":str}` |

Telemetry payload:

```json
{"device_id":"A-101","ts":1760000000.0,"flow_lpm":0.4,"pressure_kpa":240.0,"total_l":1234.5}
```

Telemetry is not retained: stale readings must not be replayed to new subscribers. QoS 1 gives at-least-once delivery, so consumers must tolerate duplicates.

Topic names and fields above are the documented contract of this repo's infra; if the edge/backend implementations differ, the code is authoritative and this file should be updated. `infra/loadtest/load_test.py` uses the telemetry topic.

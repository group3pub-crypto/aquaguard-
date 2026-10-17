# Hardware / firmware limitations

* **Nothing here has run on hardware.** Host tests and a compile check only (README, "Verification status").
* **No valve position feedback**: `valve` is the commanded state; confirmation is flow-based and impossible when no water flows.
* **NO valve fails open** on any power or controller failure; see [valve-safety.md](valve-safety.md).
* **YF-S201** is rated about 1-30 L/min and is uncalibrated by default (450 pulses/L, nominal). Slow leaks below about 1 L/min are not reliably measurable; the leak thresholds are engineering starting points, not validated against real leak data.
* `stuck_line` cannot distinguish a dead sensor from a quiet flat; it is advisory (7 days).
* Battery percent is an approximate open-circuit table; no charge/mains-presence information; ESP32 ADC is non-linear and per-chip.
* **Pressure (`psi`) and temperature (`tmp`) are not implemented** (always `null`); model and wiring are unspecified, no driver was invented.
* **No LWT**, no OTA, no secure boot / flash encryption configured, no NVS credential provisioning; credentials are compiled into the image.
* QoS 1 is used for publishes (espMqttClient). The reading ring holds 16 readings (about 16 min at 60 s) in RAM and is lost on reset; only readings younger than `MAX_BACKFILL_S` (540 s) are sent after reconnect, matching the backend's 600 s live-reading limit.
* `vol` is litres since boot; `tot` is persisted but can lose up to about 1 L or 60 s of flow at an unplanned power cut.
* Time zone is a fixed offset (`TZ_OFFSET_S`), no DST; night-leak detection needs synced time.
* Valve command authenticity relies on broker ACL + TLS + per-device credentials; there is no message signing.
* Single valve, single flow sensor per node; no tamper/door sensing.
* Safety: remote closure can affect fire-fighting or sanitation supply; keep manual isolation and bypass valves (`docs/limitations.md`).

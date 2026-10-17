# Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| Flow always 0 | wiring or level shifter; check GPIO27 with a scope while water flows; sensor needs 5-18 V; flow arrow direction; `session_pulses` in the serial log (10 s) |
| Flow too high / noisy | no debounce margin or EMI: shielded cable, RC on the signal, check `MIN_PULSE_GAP_US`; `flow_impossible` flag |
| Flow off by a constant factor | `PULSES_PER_LITRE` uncalibrated; follow the jug procedure |
| `stuck_line` | advisory: no edge for 7 days (also on vacancy). Check cable/sensor; it resets while the valve is closed |
| `bat:null`, `adc_fault` | divider disconnected/shorted, battery < 2.5 V or > 4.5 V at the divided input, wrong `BATTERY_DIVIDER_RATIO` |
| Battery % jumps | percent comes from an approximate resting-voltage table; Wi-Fi bursts sag the cell. Use a discharge-based table |
| Relay inverted at boot (valve closed at power-up) | `RELAY_ACTIVE_LOW` wrong or missing pull resistor. Test with the valve **disconnected** |
| Valve re-opened by itself | power loss/reset/brown-out/watchdog (check `rst_brownout`, `rst_wdt` in `err`), relay fuse, supply dip. NO valves fail open by design ([valve-safety.md](valve-safety.md)) |
| `close_unconf` | water still flowing after close: bypass open, relay not switching, valve failed, supply fuse, or an upstream leak. Verify physically |
| Command ignored | serial log shows the verdict: `rate_limited` (<30 s since last / >10 per hour), `stale` (clock skew > 120 s or old `ts`), `time_invalid`, `source_not_allowed` (auto close with `ACCEPT_AUTO_CLOSE 0`, or non-manual open), `duplicate` |
| Backend never sees data, serial says "NOT connecting" | placeholder credentials/CA (fail closed) |
| Wi-Fi up but no MQTT | clock not synced (UDP 123 blocked), CA mismatch, host not in certificate SAN, wrong port, ACL mismatch (Mosquitto silently drops unauthorised publishes) |
| Gaps in history after an outage | by design: only readings <= 540 s old are replayed because the backend rejects older live readings; the next reading has `backfill_drop`; `tot` still shows the volume |
| Duplicate readings | QoS 1 at-least-once; ids are stable across replays so the backend de-duplicates |
| Reboot loop | brown-out (power), watchdog (blocking TLS > `WDT_TIMEOUT_S`), flash/NVS problems; watch the reset reason on the serial console |
| Sketch will not compile | `config.h` missing; `espMqttClient` also needs `AsyncTCP` installed on ESP32 core 3.x (its async variant is compiled with the library); ArduinoJson must be v7 |

# Hardware-in-the-loop test plan

All stages are **UNTESTED**: this is the plan, not results. Record results (date, firmware commit,
operator, raw logs) in your own commissioning record. A stage passes only if **every** criterion passes.
Do not proceed to the next stage on a partial pass. Keep `ACCEPT_AUTO_CLOSE 0` and
`LOCAL_AUTOSHUTOFF_ENABLED 0` throughout Stages 0-3a.

Equipment: function generator (3.3 V square), bench PSU, multimeter, oscilloscope/logic analyser
(recommended), MQTT subscriber with the privileged account, measuring jug or scale, stopwatch, test
rig (below), spare relay and valve.

## Stage 0 - software gates (no hardware)

| ID | Test | Pass criterion |
|---|---|---|
| 0.1 | `make -C edge/firmware test` | 0 failures |
| 0.2 | `pytest edge/tests/test_firmware_payload.py` | all pass |
| 0.3 | sketch compiles with your real `config.h` (no `ALLOW_INSECURE_DEV`) | 0 errors, 0 warnings in project files |

## Stage 1 - bench, valve DISCONNECTED from water and from the valve supply

Relay output drives a lamp/LED load or only a multimeter on the contacts. No pipe, no water.

| ID | Test | Pass criterion |
|---|---|---|
| 1.1 | Relay polarity: set `RELAY_ACTIVE_LOW` per board; power-up with empty NVS | relay de-energised at boot and during reset (observe with scope on the contacts/LED), energised only after a close command |
| 1.2 | Flow input: 3.3 V square wave 7.5 Hz, 75 Hz, 225 Hz through the **real** level shifter | reported L/min = 1.0 / 10.0 / 30.0 within +/-2 % (450 pulses/L); no count at 100 us glitches |
| 1.3 | Level shifter: measure GPIO27 with sensor supply at max expected V | never > 3.3 V (+0.3), never < 0 V |
| 1.4 | Disconnect signal wire for > `STUCK_LINE_S` (shorten for the test) | `stuck_line` appears, clears when pulses return |
| 1.5 | Inject > 36 L/min (270 Hz) for 5 s | `flow_impossible`, payload `flow <= 500`, valve unchanged |
| 1.6 | Battery: PSU at 3.3 / 3.6 / 3.9 / 4.2 V on the divider | `vbat` within +/-50 mV of multimeter (after setting `BATTERY_DIVIDER_RATIO`); `bat_low` set <= 20 %, clears >= 25 % |
| 1.7 | ADC wire removed | `adc_fault`, `bat:null` within 90 s |
| 1.8 | Power-cycle with valve intent closed (send manual close first) | relay energised within **<= 10 s of power return** (measure) and before the first Wi-Fi log line; `valve:"closed"` in the first reading |
| 1.9 | Unplug power while closed | relay de-energises (expected: valve would OPEN) - note and photograph the evidence |
| 1.10 | Wi-Fi AP off 15 min then on | readings after reconnect arrive oldest-first, original `ts`, no duplicate `id` in DB; readings older than 540 s are dropped and the next carries `backfill_drop` |
| 1.11 | Broker down then up | reconnect with backoff <= 60 s; no relay change during the outage |
| 1.12 | TLS: wrong CA / hostname / expired cert / clock not synced | connection refused each time; no plaintext fallback |
| 1.13 | Commands: replay same id; stale `ts` (-5 min); `open` with `src:"auto"`; `open` without `ts`; 11 toggles in an hour; chatter < 30 s | replay not re-applied; stale/auto/no-ts refused; 11th and chatter refused; every refusal acked with the *actual* state |
| 1.14 | Brown-out: lower supply until reset | `rst_brownout` in next reading; valve intent re-asserted after boot |
| 1.15 | Watchdog: debug build with a deliberate loop hang | reset within `WDT_TIMEOUT_S`; `rst_wdt` reported; intent re-asserted |
| 1.16 | Soak: 24 h with simulated flow pattern | no unexplained reset; heap stable; NVS write count (add logging) consistent with the policy (<= 1 per min) |

## Stage 2 - water test rig (valve installed, closed-loop tank, manual bypass, drain, pressure gauge)

Pre-conditions: Stage 1 passed; valve datasheet reviewed; coil protection fitted; fuse fitted; a person
present; the rig can be isolated by hand-valves.

| ID | Test | Pass criterion |
|---|---|---|
| 2.1 | **YF-S201 calibration** (below) at 2, 6, 12 L/min, >= 3 runs each | per-rate mean within +/-3 % of reference; spread between runs <= +/-3 %; set `PULSES_PER_LITRE` to the 6 L/min result; verify afterwards at all three rates within +/-5 % |
| 2.2 | Close under flow (6 L/min) | flow < 0.2 L/min within `close_settle_s` (15 s); no `close_unconf`; record real closing time |
| 2.3 | Close with bypass open | `close_unconf` within 60 s (this must trigger) |
| 2.4 | Water hammer: close at 6 and 12 L/min while watching the gauge / listening | no pressure spike beyond the rig/valve rating; record |
| 2.5 | Coil temperature: valve closed 4 h (target 24 h) | coil surface temperature below the datasheet limit; no `coil_duty` surprise |
| 2.6 | Power loss while closed | valve re-opens (expected); after power return it closes again within <= 10 s without a command |
| 2.7 | Open with `manual_dashboard`/`manual_bot` | opens, ack `open`; `auto` open refused |
| 2.8 | Leak scenarios: 0.5-1 L/min continuous, 3 L/min continuous (shortened thresholds), burst 20+ L/min | flags `leak_cont` / `burst` appear at the configured thresholds; no flags during normal on/off use patterns |
| 2.9 | Night window (set clock/`TZ_OFFSET_S`) 0.5-1 L/min | `leak_night` after `NIGHT_SUSTAIN_S`; none outside the window; none with unsynced clock |
| 2.10 | 72 h soak with scripted draws | zero false `close_unconf`; zero unexpected relay actuations (log relay edges) |

## Stage 3 - live

3a. Install on one real flat with manual bypass accessible, `ACCEPT_AUTO_CLOSE 0`. Run >= 14 days.
Pass: flow/vol within +/-5 % of the building's meter reading over the period (if available); no
unexplained reset; false-positive `err` rate recorded; manual close/open from dashboard works 5 times
with correct acks; resident informed about the manual bypass.

3b. Sign-off for auto shut-off requires: all of Stage 2 passed on the *same* valve/relay/PSU; 3a passed;
the backend alert path reviewed; a named owner accepts the fire-fighting/supply risk; then set
`ACCEPT_AUTO_CLOSE 1` for that unit and enable `auto` for the flat in the backend. Repeat 2.2/2.3 live.

## YF-S201 calibration procedure (measuring jug)

1. Flow rate set with a tap/valve to about 2, 6 and 12 L/min; let it stabilise.
2. Use a container of **>= 10 L** (a larger volume dilutes start/stop error to < 1 %). Prefer weighing:
   1 kg of water at about 20 C is about 1 L; use a scale accurate to +/-0.5 %. A graduated jug is only
   about +/-2-5 %, so use it for sanity checks, not final calibration.
3. Note `session_pulses` (serial log) or the pulse counter before and after filling exactly N litres:
   `pulses_per_litre = pulses / litres`.
4. Repeat >= 3 times per rate. Discard a run if the flow visibly changed.
5. Accept when run-to-run spread is within +/-3 %. If the three rates differ by more than 10 %, the
   sensor is non-linear in your range: calibrate at the rate that matters (about 6 L/min) and accept the stated error elsewhere.
6. Put the result in `config.h` (`#define PULSES_PER_LITRE 4xx.x`), reflash, repeat the check.
   The datasheet nominal is 450 pulses/L (7.5 Hz per L/min); commonly quoted accuracy is about +/-10 %
   uncalibrated (check your datasheet). `tot` is stored as pulses, so recalibrating rescales lifetime litres.
7. Record sensor serial/batch, date, temperature, results.

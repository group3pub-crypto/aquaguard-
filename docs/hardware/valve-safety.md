# Normally-open valve safety

**Core fact:** for a normally-open (NO) solenoid valve, *de-energised = OPEN (water flows)* and
*energised = CLOSED*. Holding it closed needs the coil powered continuously. Therefore:

> Power loss, ESP32 reset, brown-out, watchdog, relay failure, a blown valve fuse or a lost supply
> all **re-open the valve**. Nothing in this system may assume that a fault stops the water.

Status: the rules below are implemented in `aquaguard_core` and covered by host tests; **none of it
has run against a real valve**. Behaviour on real hardware must be confirmed in
[hil-test-plan.md](hil-test-plan.md).

## Behaviour contract (enforced in code)

| Situation | Behaviour | Where / test |
|---|---|---|
| Boot | Relay driver is set de-energised, NVS is read, and if the persisted intent is `closed` the relay is energised **before Wi-Fi/MQTT start** (seconds after reset; the valve is open for the outage plus boot time) | `setup()`, `ValveController::bootAssert`; `valve_boot_open_by_default_and_reassert_closed` |
| Intent persistence | Written to NVS on every change (rare, rate-limited) | `applyState`; store failure sets `store_fail` but the state is still applied |
| Wi-Fi / MQTT / NTP loss | **Keep the last commanded state.** No auto-open, no auto-close | no code path exists; test runs ~100 000 s of ticks |
| Sensor fault (stuck line, impossible flow, ADC fault) | Reported in `err` only. No auto-close, no auto-open | `valve_no_auto_open_paths`, `node_local_autoshutoff_...` |
| `open` command | Accepted only if `src` starts with `manual_`, time is valid, `ts` is present and fresh (<= 120 s old, <= 60 s in future). `src:"auto"` open is **rejected** | `handleCommand` |
| `close` command | `manual_*` accepted; `auto` accepted **only** if built with `ACCEPT_AUTO_CLOSE 1` (default 0 = commissioning gate). Without valid time a close is still accepted (safe direction) | `valve_auto_close_gate_default_off` |
| Replay | Last 16 command ids remembered: a repeat is **not re-applied** but re-acked with the current state | `valve_anti_replay_ring` |
| Rate limiting | >= 30 s between actuations (a `manual_override` close bypasses this one), <= 10 actuations per rolling hour (no exceptions) - prevents relay chatter and water hammer | `valve_rate_limit_...` |
| Malformed command | Validated: `v == 1`, id `[A-Za-z0-9_-]{1,16}`, cmd `open|close`, src token; otherwise dropped without ack | `valve_command_validation` |
| Rejected valid command | Acked with the **actual** state and the command's `cmd_id`, so the backend can see it was not honoured (it only confirms a command if the reported state matches) | `.ino` `onMqttMessage` |
| Local autonomous shut-off | **OFF** (`LOCAL_AUTOSHUTOFF_ENABLED 0`). If enabled: burst active for >= 120 s, no sensor fault, rate limit applies. Never opens | `node_local_autoshutoff_...` |
| Remote auto shut-off | Governed by the backend alert state machine; the device additionally requires `ACCEPT_AUTO_CLOSE 1` | |

## No position feedback: "commanded", not "observed"

No limit switch or position sensor is specified, so `valve` in `flow` payloads and the `state` in
`valve/state` are the **commanded** state. The firmware adds a flow-based check after a close:

1. Pending for `close_settle_s` (15 s, must exceed the 10 s averaging window).
2. If flow was present around the command and the window flow is <= 0.2 L/min after the settle time -> **confirmed**.
3. If flow has not stopped after 60 s -> `err: close_unconf` (also reported when flow returns for >= 30 s while "closed", e.g. relay/valve failure or an open bypass).
4. If there was no flow when the close was commanded, zero flow proves nothing -> **unverifiable** (no error, no claim).

This catches a dead relay, a failed coil or a bypass left open; it cannot tell a stuck-open valve
from a leak upstream, and it cannot confirm anything while no water is being used. Add a real
position/limit switch if you need an observed state.

## Hardware recommendations

* Consider a **normally-closed** valve (stops water on power loss, but cuts supply, including fire
  fighting and sanitation; see `docs/limitations.md`) or a **latching** valve (keeps its state without
  power, needs a pulse from a reversing driver; **not implemented** by this firmware). The NO design
  chosen here is fail-open: choose consciously per building and local rules.
* Keep a **manual isolation valve and a manual bypass** that does not depend on electronics.
* Provide a **UPS / battery backup** for controller *and* valve supply; a closed NO valve is held by
  continuous coil current, so battery backup of the valve supply (not just the ESP32) is what keeps it closed.
* Check the coil's **continuous-duty rating** and thermal limits (many small valves are intermittent duty);
  set `COIL_DUTY_WARN_S` accordingly (flag only: the firmware never opens the valve to cool the coil).
* Slow-closing valve, or air chamber, to limit **water hammer**; rate limits above protect the relay and plumbing.
* Fuse and isolate the valve supply ([wiring-and-electrical.md](wiring-and-electrical.md)).

## Commissioning gate (auto shut-off is DISABLED by default)

Auto shut-off is considered **not validated** (`auto_shutoff_validated: false`; this flag is
documentation only - the backend does not have it yet, see README follow-ups) until
[hil-test-plan.md](hil-test-plan.md) passes in order:

1. Stage 1 - bench, valve **disconnected** from water (relay driving a dummy load).
2. Stage 2 - test rig with water, valve installed, manual bypass present.
3. Stage 3 - live pilot with manual commands only (`ACCEPT_AUTO_CLOSE 0`), then sign-off for auto.

Until then: leave `ACCEPT_AUTO_CLOSE` and `LOCAL_AUTOSHUTOFF_ENABLED` at 0 and have the backend refuse
`auto` for that flat.

## Known residual risks

* A reset loop (brown-out, watchdog) toggles the relay at the reset rate; the rate limiter does not protect against resets. Fix the power supply.
* Between power loss and re-assert the valve is open; if the building's water is on during an extended outage the leak continues.
* NVS corruption or erased flash loses the closed intent -> valve comes up open (fail-open by design for a NO valve).
* An attacker with broker credentials can still send `manual_override` closes: use per-device ACLs and TLS ([mqtt-tls-setup.md](mqtt-tls-setup.md)); `src` is not authenticated end-to-end.

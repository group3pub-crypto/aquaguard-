# Wiring and electrical review

**Status: design review only. Nothing in this document has been built or measured.** Component
values are starting points to be verified against the real parts' datasheets. Mains voltage work
must be done by a qualified electrician; prefer a 12/24 V DC valve so no mains reaches the enclosure.

## 1. Pin review (ESP32 classic, e.g. DevKit)

| Function | GPIO | Verdict |
|---|---|---|
| Flow pulses (`FLOW_PIN`) | 27 | OK as digital input with interrupt. Not a strapping pin. **3.3 V logic only** (see 2). No internal pull-up is used; the external network defines the idle level |
| Valve relay (`VALVE_RELAY_PIN`) | 26 | OK as output. Not a strapping pin (strapping pins are 0, 2, 5, 12, 15). The pin is not driven by firmware until `setup()`, so fit a pull resistor to hold the relay de-energised during reset/boot (see 4) |
| Battery sense (`BATTERY_ADC_PIN`) | 34 | Input-only, ADC1 channel, so usable while Wi-Fi is active (ADC2 is not). **No internal pull-up/down** -> the external divider fully defines the level. Never leave the pin floating |

Avoid GPIO 6-11 (flash), 0/2/5/12/15 (strapping) and ADC2 pins for analog reads. Verify against the
pinout of *your* module revision.

## 2. YF-S201 flow sensor interface (critical)

* The YF-S201 is typically specified for roughly **5-18 V** supply; its output swings to about the
  supply voltage (commonly via a pull-up on the sensor board; **unknown for your unit - measure**).
* An output of 5 V (or more) **must not** be connected to an ESP32 GPIO (absolute maximum is about
  3.6 V). The original sketch comment "3.3 V logic!" was a warning, not a protection.
* Options, in order of robustness (all UNTESTED):
  1. **Opto-isolator or BSS138-style bidirectional level shifter** between sensor and ESP32; good
     for 12 V supplies and noisy cabling. Pulse rate is at most 225 Hz (30 L/min), so slow optocouplers are fine.
  2. **Resistor divider** for a 5 V supply: 10 k series + 15 k to GND -> 3.0 V high (2.7 V at 4.5 V,
     3.15 V at 5.25 V; ESP32 V_IH is about 0.75 x 3.3 V = 2.5 V). For other supply voltages recompute
     `Vout = Vcc * R2/(R1+R2)` at Vcc_max <= 3.3 V and at Vcc_min >= 2.6 V. A divider only works if
     the sensor output is actively driven or has its own pull-up (confirm!). Optional 4.7 nF to GND
     (RC about 70 us with 10 k) for noise; do not exceed about 100 us.
  3. If the output is a bare open-collector: pull up to **3.3 V** with 4.7-10 k and power the sensor
     from its own 5 V; never pull an open-collector to 5 V into the ESP32.
* Protection: series 1 k at the GPIO, ESD/TVS clamp on the signal and supply lines near the connector,
  reverse-polarity protection on the sensor supply (series Schottky or P-MOSFET), TVS on 12 V rails.
  Use shielded/twisted cable for runs beyond about 1 m; the firmware also rejects edges closer than
  `MIN_PULSE_GAP_US` (1500 us, i.e. >666 Hz, far above 225 Hz at 30 L/min).
* Sensor limits: about 1-30 L/min rated range; slow drips below roughly 1 L/min are **not** measurable.
  The default `MIN_LEAK_FLOW_LPM` (0.5) therefore only helps for the upper part of the low range.

## 3. Battery divider (GPIO34)

* Li-ion 1S: 3.0-4.2 V. Divider 100 k / 100 k (ratio 2.0) -> 2.1 V at full charge, inside the
  11 dB attenuation range (about 0.15-2.45 V nominal). Add **100 nF** from the ADC pin to GND (also
  lowers the source impedance seen by the sampling capacitor); draw is about 21 uA at 4.2 V. Use 47 k / 47 k
  for better accuracy at about 45 uA, or a MOSFET-switched divider for long battery life.
* Firmware: `analogReadMilliVolts()` (uses the eFuse calibration if the chip has it), 16-sample
  average every 30 s, `ADC_11db`. The ESP32 ADC is **non-linear** near the ends of the range and
  varies per chip: measure the battery with a multimeter at 3 voltages and set `BATTERY_DIVIDER_RATIO`
  to the empirically corrected value.
* Percent = piecewise-linear lookup of **resting open-circuit** voltage
  (`vbatToPercent`, 3.27 V = 0 % .. 4.20 V = 100 %). **APPROXIMATE**: depends on chemistry, temperature,
  age and load; under Wi-Fi TX bursts the voltage sags. Replace with a discharge-log-derived table.
* Hysteresis: low at <= 20 %, clears at >= 25 %; critical at <= 10 %, clears at >= 15 %.
* Out-of-range (<2.5 V or >4.5 V, 3 samples) => `adc_fault`, `bat:null`, no `vbat` (never a made-up value).
* Use a protected cell (PCM) and a proper charger/power-path IC; do not charge below 0 C.
  UNKNOWN: charger, battery capacity, whether the unit is mains-fed with the battery as backup,
  and whether mains presence can be sensed (the firmware cannot tell).

## 4. Relay and solenoid valve

* **Polarity is configurable**: `RELAY_ACTIVE_LOW` (0 = HIGH energises). Many opto-isolated relay
  boards are active-LOW. Check with a multimeter *with the valve disconnected* (HIL Stage 1).
* The relay *coil* needs 5 V/12 V and tens of mA: power it from a regulator that can supply it, not
  from the ESP32 pin. Some 5 V relay boards do not reliably switch from a 3.3 V logic signal;
  choose a board specified for 3.3 V drive, or add a transistor stage.
* Reset/boot: the pin is high-impedance until firmware drives it. Fit **10 k pull-down** (active-high board)
  or **10 k pull-up to 3.3 V** (active-low board) at the relay input so the relay stays de-energised,
  unless the board already has one.
* Contacts vs load: rate the contacts for the *inductive* load type and voltage (DC inductive ratings
  are much lower than the headline AC/resistive number). Check valve inrush and holding current
  against the contact rating with >= 2x margin. If in doubt use a MOSFET (DC) or SSR (AC) rated for
  the load.
* **Transient suppression** (mandatory, at the coil):
  * DC coil: flyback diode across the coil (cathode to +), or bi-directional TVS if faster release is
    needed. *Do not* use a diode on an AC coil.
  * AC coil: RC snubber or MOV across the coil (and consider across the contacts).
* **Separate valve supply** (own fuse, sized from the datasheet), switched only by the relay contacts.
  The ESP32 + relay-logic side shares one ground (star point); keep the high-current coil return
  away from the sensor/ADC ground. With an opto-isolated relay board the valve circuit can be galvanically separate.
* **Fusing**: fuse the valve supply (time-lag, about 2x holding current - set from the datasheet),
  the sensor supply, and the battery line. Mains side per local code (breaker/RCD by an electrician).
* **Brown-out**: Wi-Fi TX peaks (hundreds of mA) and relay switching can sag the 3.3 V rail and reset
  the ESP32 -> **a NO valve then re-opens**. Fit 470-1000 uF near the ESP32 and a regulator rated >= 1 A,
  keep the brown-out detector enabled (the firmware reports `rst_brownout`), and treat any reset
  loop as a safety issue. See [valve-safety.md](valve-safety.md).
* **Enclosure**: IP65 or better, cable glands with drip loops, mount above the valve/piping,
  desiccant/conformal coat for the board; the place most likely to get wet is the place where this fails.
* Continuous coil duty: closing a NO valve means energising the coil for as long as it should stay
  closed (hours or days). Confirm the coil is rated for 100 % duty; otherwise heat and failure
  (and then *re-opening*). Optionally set `COIL_DUTY_WARN_S` to report `coil_duty`.

## 5. Wiring summary (to be drawn up and checked by the integrator)

```
5-18V -- fuse -- reverse-polarity -- TVS -- YF-S201 (+)        YF-S201 (S) -- level shifter/divider -- 1k -- GPIO27
GND ---------------------------------------YF-S201 (-)
Battery(PCM) -- fuse -- charger/power path -- 3V3 reg (>=1A, 470uF) -- ESP32
Battery+ -- 100k -- GPIO34 -- 100k -- GND, 100nF GPIO34-GND
GPIO26 -- relay IN (+10k pull per polarity)    relay contacts: valve supply -- fuse -- contacts -- coil (+ diode/snubber) -- return
```

## 6. UNKNOWN - must be filled in before a build

| Item | Value |
|---|---|
| Valve model, coil type/voltage, inrush/holding current, duty rating, closing time | UNKNOWN |
| Relay board model, active level, logic/coil voltage, contact ratings | UNKNOWN |
| Valve supply and 5 V/3.3 V supply, battery pack, charger | UNKNOWN |
| YF-S201 supply voltage, output level, pull-up present? | UNKNOWN |
| Enclosure rating, cable lengths, mains presence sensing | UNKNOWN |

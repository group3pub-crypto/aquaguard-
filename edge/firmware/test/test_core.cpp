// Host unit tests for aquaguard_core. Plain assert harness (survives NDEBUG).
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "aquaguard_core.h"
using namespace aqg;

static int g_checks = 0, g_fail = 0, g_tests = 0;
#define CHECK(c) do { g_checks++; if (!(c)) { g_fail++; printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); } } while (0)
#define NEAR(a, b, tol) CHECK(fabs((double)(a) - (double)(b)) <= (tol))
#define TEST(name) static void name(); struct R_##name { R_##name() { reg(#name, name); } } r_##name; static void name()
typedef void (*Fn)();
static struct { const char *n; Fn f; } g_t[128];
static int g_n = 0;
static void reg(const char *n, Fn f) { g_t[g_n].n = n; g_t[g_n++].f = f; }

static const uint32_t EP = 1760000000u;  // valid epoch (Oct 2025), 08:53 UTC

// feed `secs` ticks of constant flow (L/min) to a flow meter+detector
static uint32_t pulsesFor(float lpm, float ppl = 450) { return (uint32_t)(lpm * ppl / 60.0f + 0.5f); }

// ---------------------------------------------------------------- flow maths
TEST(flow_calibration_constants) {
  NEAR(pulsesToLpm(450, 60000, 450), 1.0, 1e-4);          // 450 pulses in 60 s = 1 L/min
  NEAR(pulsesToLpm(7, 1000, 450), 0.9333, 1e-3);          // 7.5 Hz = 1 L/min (7 pulses ~ 0.93)
  NEAR(pulsesToLpm(225, 1000, 450), 30.0, 1e-3);          // 225 Hz = 30 L/min
  NEAR(pulsesToLpm(450, 60000, 400), 1.125, 1e-4);        // calibrated ppl changes result
  NEAR(pulsesToLpm(100, 0, 450), 0, 0);                   // dt 0 safe
  NEAR(pulsesToLpm(100, 1000, 0), 0, 0);                  // ppl 0 safe
  FlowMeter f; Config c; c.pulses_per_litre = 400; f.begin(c, 0);
  for (int i = 0; i < 60; i++) f.addSample(400 / 60 * 6 / 6 * 1, 1000);  // 6 pulses/s -> 360 pulses
  NEAR(f.sessionLitres(), 360.0 / 400.0, 1e-9);
}

TEST(flow_window_average_and_period) {
  FlowMeter f; Config c; c.window_s = 5; f.begin(c, 0);
  for (int i = 0; i < 5; i++) f.addSample(0, 1000);
  for (int i = 0; i < 5; i++) f.addSample(pulsesFor(10), 1000);
  NEAR(f.windowLpm(), 10.0, 0.1);
  f.addSample(0, 1000);  // window slides
  NEAR(f.windowLpm(), 8.0, 0.1);
  uint32_t ms = 0; float p = f.takePeriodLpm(&ms);
  CHECK(ms == 11000); NEAR(p, 50.0 * 450 / 60.0 * 60000.0 / (450.0 * 11000.0), 0.01);
  CHECK(f.takePeriodLpm() == 0);
}

TEST(debounce_rejects_close_edges_and_wraps) {
  PulseDebouncer d;
  CHECK(d.onEdge(1000, 1500));
  CHECK(!d.onEdge(1400, 1500));      // contact bounce / EMI
  CHECK(d.onEdge(2600, 1500));
  CHECK(!d.onEdge(4000, 1500));      // 1.4 ms after previous accepted edge: rejected
  CHECK(d.onEdge(4200, 1500));
  // micros() wrap-around
  d.last_us = 0xFFFFFF00u;
  CHECK(!d.onEdge(0x00000010u, 1500));  // 272 us after wrap
  CHECK(d.onEdge(0x00001000u, 1500));
  // max real rate: 225 Hz => 4444 us period must never be rejected with 1500 us gap
  uint32_t t = 0; int acc = 0; PulseDebouncer e;
  for (int i = 0; i < 1000; i++) { t += 4444; acc += e.onEdge(t, 1500); }
  CHECK(acc == 1000);
}

// ---------------------------------------------------------------- leak detector
struct Harness {
  Config c; LeakDetector d;
  Harness() { d.begin(c); }
  void run(int secs, float lpm, bool night = false) { for (int i = 0; i < secs; i++) d.step(lpm, night); }
};

TEST(normal_use_and_short_spike_do_not_trigger) {
  Harness h;
  for (int k = 0; k < 20; k++) { h.run(90, 8.0f); h.run(300, 0); }   // showers/taps with pauses
  CHECK(h.d.mask() == 0);
  h.run(30, 25.0f); h.run(200, 0);                                    // 30 s spike < burst_sustain
  CHECK(!h.d.burstActive());
  h.run(59, 25.0f); h.run(1, 0); h.run(200, 0);                       // 59 s then drop
  CHECK(!h.d.burstActive());
}

TEST(sustained_continuous_leak_detected_and_cleared_by_zero_gap) {
  Harness h;  // 120 min, gap 120 s
  h.run(120 * 60 - 1, 1.0f); CHECK(!h.d.continuousActive());
  h.run(1, 1.0f);            CHECK(h.d.continuousActive());
  h.run(119, 0);             CHECK(h.d.continuousActive());           // gap < reset keeps it
  h.run(1, 0);               CHECK(!h.d.continuousActive());          // gap == reset clears
  h.run(60, 1.0f);           CHECK(!h.d.continuousActive());          // restarts from 0
}

TEST(short_gaps_do_not_reset_a_leak_run) {
  Harness h;  // toggling 100 s on / 60 s off for >120 min must still flag
  for (int i = 0; i < 60; i++) { h.run(100, 1.0f); h.run(60, 0); }
  CHECK(h.d.continuousActive());
}

TEST(below_min_leak_flow_is_ignored) {
  Harness h;
  h.run(3 * 3600, 0.4f);
  CHECK(h.d.mask() == 0);
}

TEST(night_leak_only_in_window) {
  Harness h;
  h.run(2000, 1.0f, false); CHECK(!h.d.nightActive());               // day flow: no night alert
  h.run(599, 1.0f, true);   CHECK(!h.d.nightActive());
  h.run(1, 1.0f, true);     CHECK(h.d.nightActive());
  h.run(1, 1.0f, false);    CHECK(!h.d.nightActive());               // window ended
  h.run(300, 1.0f, true);   CHECK(!h.d.nightActive());               // restarted, needs 600 s again
  CHECK(inNightWindow(1, 1, 5)); CHECK(!inNightWindow(5, 1, 5)); CHECK(!inNightWindow(0, 1, 5));
  CHECK(inNightWindow(23, 22, 6)); CHECK(inNightWindow(3, 22, 6)); CHECK(!inNightWindow(12, 22, 6));
  CHECK(!inNightWindow(3, 3, 3));
  CHECK(localHour(EP, 0) == 8); CHECK(localHour(EP, 19800) == 14); CHECK(localHour(100, -7200) == 22);
}

TEST(burst_detected_with_hysteresis) {
  Harness h;
  h.run(59, 22.0f); CHECK(!h.d.burstActive());
  h.run(1, 22.0f);  CHECK(h.d.burstActive());
  h.run(100, 16.0f);                                                   // 16 >= 0.7*20=14: stays
  CHECK(h.d.burstActive());
  h.run(29, 5.0f);  CHECK(h.d.burstActive());                          // below clear threshold < 30 s
  h.run(1, 20.0f);  h.run(29, 5.0f); CHECK(h.d.burstActive());         // recovery resets clear timer
  h.run(1, 5.0f);   CHECK(!h.d.burstActive());
  // dips within hysteresis band while arming do not reset the run
  Harness g; g.run(30, 21.0f); g.run(10, 15.0f); g.run(20, 21.0f); CHECK(g.d.burstActive());
  // dip below band while arming resets
  Harness k; k.run(30, 21.0f); k.run(1, 5.0f); k.run(40, 21.0f); CHECK(!k.d.burstActive());
}

// ---------------------------------------------------------------- persistence
TEST(volume_persistence_wear_policy) {
  MemStore st; Config c; VolumePersister p; p.begin(&st, c);
  CHECK(p.load() == 0);
  uint64_t total = 0; uint32_t t = 0;
  // 0.2 L/min for 4 min (< 1 L, < 5 min): no write at all
  for (; t < 240; t++) { if (t % 7 == 0) total++; p.update(total, t); }
  CHECK(st.writes == 0);
  // after >=300 s with unsaved delta: one time-based write
  for (; t < 301; t++) p.update(total, t);
  CHECK(st.writes == 1);
  // heavy flow 20 L/min: volume trigger but never more often than persist_min_gap_s
  uint32_t w0 = st.writes;
  for (int i = 0; i < 600; i++, t++) { total += 150; p.update(total, t); }   // 20 L/min * 10 min
  CHECK(st.writes - w0 <= 11); CHECK(st.writes - w0 >= 9);
  // no change => no writes
  w0 = st.writes; for (int i = 0; i < 1000; i++, t++) p.update(total, t);
  CHECK(st.writes - w0 <= 1);
  // pulses per write bounded: per-pulse writes never happen
  CHECK(st.writes < 40);
  p.flush(total, t);
  uint64_t v = 0; CHECK(st.getU64("pulses", &v) && v == total);
  // reboot restores total
  VolumePersister p2; p2.begin(&st, c); CHECK(p2.load() == total);
  FlowMeter f; f.begin(c, total); NEAR(f.totalLitres(), total / 450.0, 1e-9); CHECK(f.sessionPulses() == 0);
}

TEST(volume_persist_failure_flagged_and_retry_throttled) {
  MemStore st; st.fail_writes = true; Config c; VolumePersister p; p.begin(&st, c); p.load();
  uint32_t t = 0; uint64_t total = 10000;
  for (; t < 1000; t++) p.update(total, t);
  CHECK(p.lastWriteFailed()); CHECK(st.writes == 0);
  st.fail_writes = false;
  for (; t < 1100; t++) p.update(total, t);
  CHECK(!p.lastWriteFailed()); CHECK(st.writes == 1);
}

// ---------------------------------------------------------------- battery
TEST(battery_table_and_percent) {
  NEAR(vbatToPercent(4.20f), 100, 0.01); NEAR(vbatToPercent(4.5f), 100, 0.01);
  NEAR(vbatToPercent(3.27f), 0, 0.01);   NEAR(vbatToPercent(3.0f), 0, 0.01);
  NEAR(vbatToPercent(3.84f), 50, 0.01);
  float prev = -1; for (float v = 3.0f; v <= 4.3f; v += 0.01f) { float p = vbatToPercent(v); CHECK(p >= prev); prev = p; }
  Config c; BatteryMonitor b; b.begin(c);
  b.update(2100);  // 2.1 V at pin * 2.0 = 4.2 V
  CHECK(b.valid()); CHECK(b.percent() == 100); NEAR(b.vbat(), 4.2, 1e-4);
  b.update(-1); CHECK(b.valid());   // no new sample: state kept
}

TEST(battery_low_hysteresis) {
  Config c; BatteryMonitor b; b.begin(c);
  auto set = [&](float v) { b.update((int)(v / c.divider_ratio * 1000)); };
  set(3.9f); CHECK(!b.low() && !b.critical());
  set(3.73f); CHECK(b.low() && !b.critical());          // 20 %
  set(3.74f); CHECK(b.low());                           // 22 %: still low (hysteresis)
  set(3.745f); CHECK(b.low());                          // ~24 %: still low
  set(3.76f); CHECK(!b.low());                          // >= 25 %: cleared
  set(3.69f); CHECK(b.critical() && b.low());           // 10 %
  set(3.705f); CHECK(b.critical());                     // ~14 %
  set(3.72f); CHECK(!b.critical() && b.low());          // >= 15 % crit clears, low still on
  CHECK((b.mask() & errBit(ERR_BAT_LOW)) && !(b.mask() & errBit(ERR_BAT_CRIT)));
}

TEST(battery_adc_fault_detection) {
  Config c; BatteryMonitor b; b.begin(c);
  b.update(2000); CHECK(b.valid());
  b.update(0); b.update(0); CHECK(!b.adcFault()); b.update(0);   // 3 consecutive bad samples
  CHECK(b.adcFault() && !b.valid() && b.percent() == -1);
  CHECK(b.mask() & errBit(ERR_ADC_FAULT));
  b.update(2000); CHECK(!b.adcFault() && b.valid());              // recovers
  b.update(3300); b.update(3300); b.update(3300);                 // 6.6 V: out of range
  CHECK(b.adcFault());
  Config n; n.battery_present = false; BatteryMonitor nb; nb.begin(n); nb.update(2000);
  CHECK(!nb.valid() && !nb.adcFault());
}

// ---------------------------------------------------------------- sensor faults
TEST(fault_impossible_flow) {
  Config c; SensorFaults f; f.begin(c);
  f.step(1, false, 5, 40.0f, false); f.step(2, false, 5, 40.0f, false);
  CHECK(!f.impossible()); f.step(3, true, 5, 40.0f, false); CHECK(f.impossible());
  CHECK(f.mask() & errBit(ERR_FLOW_IMPOSSIBLE));
  f.step(4, false, 5, 29.0f, false); CHECK(!f.impossible());
  f.step(5, false, 5, 35.9f, false); f.step(6, false, 5, 35.9f, false); f.step(7, false, 5, 35.9f, false);
  CHECK(!f.impossible());   // 30*1.2 = 36 boundary
}

TEST(fault_stuck_line) {
  Config c; c.stuck_line_s = 1000; SensorFaults f; f.begin(c);
  for (uint32_t t = 0; t < 999; t++) f.step(t, true, 0, 0, false);
  CHECK(!(f.mask() & errBit(ERR_STUCK_LINE)));
  f.step(1000, true, 0, 0, false); CHECK(f.mask() & errBit(ERR_STUCK_LINE));
  f.step(1001, true, 3, 1, false); CHECK(!(f.mask() & errBit(ERR_STUCK_LINE)));   // pulses => alive
  for (uint32_t t = 1002; t < 3000; t++) f.step(t, t % 2, 0, 0, false);            // level toggling
  CHECK(!(f.mask() & errBit(ERR_STUCK_LINE)));
  for (uint32_t t = 3000; t < 6000; t++) f.step(t, false, 0, 0, true);             // valve closed
  CHECK(!(f.mask() & errBit(ERR_STUCK_LINE)));
}

// ---------------------------------------------------------------- message ids
TEST(message_ids_unique_across_reboots_and_formatted) {
  MemStore st; Config c; c.id_reserve = 64;
  static uint32_t seen[20000]; size_t n = 0;
  for (int boot = 0; boot < 40; boot++) {
    MessageIdGenerator g; g.begin(&st, c.id_reserve, 0xA5A5u + boot);
    CHECK(g.freshStart() == (boot == 0));
    int msgs = 100 + boot * 7;   // different run lengths, includes mid-block reboots
    for (int i = 0; i < msgs; i++) { if (n < 20000) seen[n++] = g.next(); }
  }
  size_t dup = 0;
  for (size_t i = 0; i < n; i++) for (size_t j = i + 1; j < n && j < i + 1 + 20000; j++) if (seen[i] == seen[j]) dup++;
  CHECK(dup == 0);
  MessageIdGenerator g; char b[9]; g.format(0xDEADBEEFu, b); CHECK(!strcmp(b, "deadbeef")); g.format(1, b); CHECK(!strcmp(b, "00000001"));
  CHECK(st.writes <= 40 + (n / 64) + 40);   // persisted in blocks, not per message
}

TEST(message_id_store_failure_still_jumps) {
  MemStore st; st.fail_writes = true; MessageIdGenerator g; g.begin(&st, 64, 0x12345678u);
  CHECK(g.storeFailed());
}

// ---------------------------------------------------------------- ring buffer
static Reading mk(uint32_t id, uint32_t mono, uint32_t ts) { Reading r; r.id = id; r.mono_s = mono; r.ts = ts; return r; }

TEST(ring_oldest_first_overflow_and_ack) {
  ReadingRing q;
  for (uint32_t i = 1; i <= AQG_RING_CAP + 3; i++) q.push(mk(i, i * 60, EP + i * 60));
  CHECK(q.size() == AQG_RING_CAP); CHECK(q.takeDropped() == 3); CHECK(q.takeDropped() == 0);
  CHECK(q.oldest()->id == 4);
  CHECK(!q.popOldestIfId(99)); CHECK(q.popOldestIfId(4)); CHECK(q.oldest()->id == 5);
  uint32_t last = 0; while (q.oldest()) { CHECK(q.oldest()->id > last); last = q.oldest()->id; q.popOldestIfId(last); }
  CHECK(q.size() == 0);
}

TEST(ring_stale_drop_and_timestamp_resolution) {
  ReadingRing q;
  q.push(mk(1, 100, 0)); q.push(mk(2, 160, 0)); q.push(mk(3, 220, 0));
  CHECK(q.oldest() == nullptr);                       // unresolved => nothing to send
  q.resolveTimestamps(EP, 230);                       // sync arrives at uptime 230
  CHECK(q.oldest()->ts == EP - 130); CHECK(q.oldest()->err & errBit(ERR_TS_ESTIMATED));
  CHECK(q.dropStale(230 + 500, 600) == 1);            // reading 1 is 630 s old, reading 2 is 570 s
  CHECK(q.oldest()->id == 2);
  CHECK(q.takeDropped() == 1);
}

// ---------------------------------------------------------------- valve
struct V {
  MemStore st; Config c; ValveController v; uint32_t t = 100;
  V() { c.accept_auto_close = true; v.begin(&st, c); v.bootAssert(t); }
  Verdict cmd(const char *id, const char *cmd, const char *src, int64_t ts = -1, bool timeok = true) {
    RawCommand r; r.v = 1; r.id = id; r.cmd = cmd; r.src = src;
    r.has_ts = ts != 0 || true; r.ts = ts < 0 ? (int64_t)EP + (int64_t)t : ts;
    return v.handleCommand(r, t, EP + t, timeok);
  }
};

TEST(valve_boot_open_by_default_and_reassert_closed) {
  V a; CHECK(a.v.state() == VALVE_OPEN); CHECK(!a.v.energized());
  CHECK(a.cmd("c1", "close", "manual_bot") == CMD_APPLIED);
  CHECK(a.v.energized());
  uint64_t p; CHECK(a.st.getU64("valve", &p) && p == 1);
  // power cycle: new controller, same store: closed intent must be re-asserted before anything else
  ValveController v2; v2.begin(&a.st, a.c);
  CHECK(v2.bootAssert(5) == VALVE_CLOSED); CHECK(v2.energized());
  CHECK(v2.confirm() == CC_PENDING);
  // comms loss / long idle never changes it
  for (uint32_t t = 6; t < 100000; t++) v2.tick(t, 0.0f);
  CHECK(v2.state() == VALVE_CLOSED);
}

TEST(valve_no_auto_open_paths) {
  V a;
  CHECK(a.cmd("c1", "close", "auto") == CMD_APPLIED);
  a.t += 100;
  CHECK(a.cmd("c2", "open", "auto") == CMD_REJECT_SOURCE);       // auto can never open
  CHECK(a.cmd("c3", "open", "system") == CMD_REJECT_SOURCE);
  CHECK(a.cmd("c4", "open", "manual_bot", 0) == CMD_REJECT_STALE); // ts 0 => stale/invalid
  CHECK(a.v.state() == VALVE_CLOSED);
  // sensor fault / flow anomaly / long silence: no spontaneous opening
  for (uint32_t t = 0; t < 50000; t++) a.v.tick(a.t + t, (t % 3) ? 50.0f : 0.0f);
  CHECK(a.v.state() == VALVE_CLOSED); CHECK(a.v.energized());
  a.t += 50000;
  CHECK(a.cmd("c5", "open", "manual_dashboard") == CMD_APPLIED);
  CHECK(!a.v.energized()); uint64_t p; CHECK(a.st.getU64("valve", &p) && p == 0);
}

TEST(valve_open_requires_valid_time_and_freshness) {
  V a; a.cmd("c1", "close", "manual_bot"); a.t += 100;
  CHECK(a.cmd("c2", "open", "manual_bot", -1, false) == CMD_REJECT_TIME);
  CHECK(a.cmd("c3", "open", "manual_bot", (int64_t)EP + a.t - 500) == CMD_REJECT_STALE);    // replayed old open
  CHECK(a.cmd("c4", "open", "manual_bot", (int64_t)EP + a.t + 4000) == CMD_REJECT_STALE);   // far future
  CHECK(a.v.state() == VALVE_CLOSED);
  // close is still accepted without valid time (safe direction), but with stale ts when time valid it is not
  V b; CHECK(b.cmd("k1", "close", "manual_bot", -1, false) == CMD_APPLIED);
  V c; CHECK(c.cmd("k2", "close", "manual_bot", (int64_t)EP + c.t - 500) == CMD_REJECT_STALE);
}

TEST(valve_command_validation) {
  V a; RawCommand r; r.v = 1; r.id = "abcd1234"; r.cmd = "close"; r.src = "manual_bot"; r.has_ts = true; r.ts = EP + a.t;
  CHECK(a.v.handleCommand(r, a.t, EP + a.t, true) == CMD_APPLIED);
  RawCommand b = r; b.v = 2; b.id = "x2";                                   CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  b = r; b.id = nullptr;                                                    CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  b = r; b.id = "";                                                         CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  b = r; b.id = "0123456789abcdef0";                                        CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);  // 17
  b = r; b.id = "a\"b";                                                     CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);  // JSON-injection guard
  b = r; b.cmd = "toggle"; b.id = "n1";                                     CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  b = r; b.cmd = nullptr; b.id = "n2";                                      CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  b = r; b.src = nullptr; b.id = "n3";                                      CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  b = r; b.src = "manual bot"; b.id = "n4";                                 CHECK(a.v.handleCommand(b, a.t, EP, true) == CMD_REJECT_MALFORMED);
  CHECK(verdictShouldAck(CMD_NOOP) && !verdictShouldAck(CMD_REJECT_MALFORMED));
  // backend's 8-hex id is accepted
  b = r; b.id = "00ff10ab"; b.cmd = "close"; CHECK(a.v.handleCommand(b, a.t, EP + a.t, true) == CMD_NOOP);
}

TEST(valve_anti_replay_ring) {
  V a;
  CHECK(a.cmd("id000001", "close", "manual_bot") == CMD_APPLIED);
  a.t += 100;
  CHECK(a.cmd("id000002", "open", "manual_bot") == CMD_APPLIED);
  a.t += 100;
  CHECK(a.cmd("id000001", "close", "manual_bot") == CMD_DUPLICATE);   // replay: NOT re-applied
  CHECK(a.v.state() == VALVE_OPEN);
  CHECK(a.cmd("id000002", "open", "manual_bot") == CMD_DUPLICATE);
  // ring holds the last 16 ids; the 17th evicts the oldest
  char id[17];
  for (int i = 0; i < 16; i++) { snprintf(id, sizeof id, "n%07d", i); a.t += 100; a.cmd(id, "open", "manual_bot"); }
  CHECK(a.cmd("id000001", "open", "manual_bot") != CMD_DUPLICATE);    // evicted (freshness check is the 2nd line)
}

TEST(valve_rate_limit_min_interval_and_hourly_cap) {
  V a;
  CHECK(a.cmd("r1", "close", "manual_bot") == CMD_APPLIED);
  a.t += 10; CHECK(a.cmd("r2", "open", "manual_bot") == CMD_REJECT_RATE);      // < 30 s
  CHECK(a.v.state() == VALVE_CLOSED);
  a.t += 25; CHECK(a.cmd("r2", "open", "manual_bot") == CMD_APPLIED);          // same id retried after interval (was not remembered)
  // emergency close bypasses the min-interval only
  a.t += 5;  CHECK(a.cmd("r3", "close", "manual_override") == CMD_APPLIED);
  // hourly cap: 10 actuations/hour
  V b; int applied = 0;
  for (int i = 0; i < 40; i++) {
    b.t += 31; char id[17]; snprintf(id, sizeof id, "h%07d", i);
    Verdict r = b.cmd(id, (i % 2) ? "open" : "close", "manual_bot");
    if (r == CMD_APPLIED) applied++;
  }
  CHECK(applied == 10);                       // 40 attempts in ~21 min
  b.t += 3700;                                // an hour later the window has drained
  CHECK(b.cmd("late", b.v.state() == VALVE_CLOSED ? "open" : "close", "manual_bot") == CMD_APPLIED);
}

TEST(valve_auto_close_gate_default_off) {
  Config c; CHECK(!c.accept_auto_close);
  MemStore st; ValveController v; v.begin(&st, c); v.bootAssert(10);
  RawCommand r; r.v = 1; r.id = "a1"; r.cmd = "close"; r.src = "auto"; r.has_ts = true; r.ts = EP + 10;
  CHECK(v.handleCommand(r, 10, EP + 10, true) == CMD_REJECT_SOURCE);
  CHECK(v.state() == VALVE_OPEN);
  r.src = "manual_dashboard"; r.id = "a2"; CHECK(v.handleCommand(r, 10, EP + 10, true) == CMD_APPLIED);
}

TEST(valve_close_confirmation_by_flow) {
  V a; a.v.tick(a.t, 5.0f);                        // water running
  CHECK(a.cmd("c1", "close", "manual_bot") == CMD_APPLIED);
  CHECK(a.v.confirm() == CC_PENDING);
  for (int i = 0; i < 20; i++) a.v.tick(a.t + i, i < 8 ? 5.0f : 0.0f);   // flow stops after 8 s
  CHECK(a.v.confirm() == CC_CONFIRMED); CHECK(!(a.v.mask() & errBit(ERR_CLOSE_UNCONF)));
  // flow resumes while "closed": relay/valve failure or bypass open
  for (int i = 0; i < 29; i++) { a.v.tick(a.t + 30 + i, 3.0f); }
  CHECK(a.v.confirm() == CC_CONFIRMED);
  a.v.tick(a.t + 60, 3.0f); CHECK(a.v.confirm() == CC_FAILED); CHECK(a.v.mask() & errBit(ERR_CLOSE_UNCONF));
  for (int i = 0; i < 20; i++) a.v.tick(a.t + 100 + i, 0.0f);
  CHECK(a.v.confirm() == CC_CONFIRMED);            // recovers once flow is gone
}

TEST(valve_close_unconfirmed_when_flow_never_stops) {
  V a; a.v.tick(a.t, 5.0f);
  a.cmd("c1", "close", "manual_bot");
  for (int i = 0; i < 59; i++) a.v.tick(a.t + i, 5.0f);
  CHECK(a.v.confirm() == CC_PENDING);
  a.v.tick(a.t + 60, 5.0f); CHECK(a.v.confirm() == CC_FAILED);
  CHECK(a.v.state() == VALVE_CLOSED);              // still commanded closed; never auto-opens
  CHECK(a.v.mask() & errBit(ERR_CLOSE_UNCONF));
}

TEST(valve_close_unverifiable_without_prior_flow) {
  V a;  // no flow before the command: zero flow proves nothing
  a.cmd("c1", "close", "manual_bot");
  for (int i = 0; i < 40; i++) a.v.tick(a.t + i, 0.0f);
  CHECK(a.v.confirm() == CC_UNVERIFIABLE); CHECK(a.v.mask() == 0);
}

TEST(valve_boot_reassert_with_leak_continuing_is_flagged) {
  MemStore st; Config c; st.putU64("valve", 1);
  ValveController v; v.begin(&st, c); v.bootAssert(0);
  for (uint32_t t = 1; t <= 61; t++) v.tick(t, 8.0f);
  CHECK(v.confirm() == CC_FAILED);
}

TEST(valve_store_failure_flagged_but_state_applied) {
  V a; a.st.fail_writes = true;
  CHECK(a.cmd("c1", "close", "manual_bot") == CMD_APPLIED);
  CHECK(a.v.storeFailed()); CHECK(a.v.energized());
}

TEST(valve_coil_duty_flag) {
  V a; a.c.coil_duty_warn_s = 3600; a.v.begin(&a.st, a.c); a.v.bootAssert(a.t);
  a.cmd("c1", "close", "manual_bot");
  a.v.tick(a.t + 3599, 0); CHECK(!(a.v.mask() & errBit(ERR_COIL_DUTY)));
  a.v.tick(a.t + 3600, 0); CHECK(a.v.mask() & errBit(ERR_COIL_DUTY));
  CHECK(a.v.state() == VALVE_CLOSED);              // flag only: never opens by itself
}

TEST(relay_polarity_configurable) {
  CHECK(relayPinHigh(true, false));  CHECK(!relayPinHigh(false, false));   // active-high
  CHECK(!relayPinHigh(true, true));  CHECK(relayPinHigh(false, true));     // active-low
}

// ---------------------------------------------------------------- backoff
TEST(backoff_exponential_capped_with_jitter) {
  Backoff b(1000, 60000); uint32_t prev = 0;
  for (int i = 0; i < 12; i++) { uint32_t d = b.next(12345 + i); CHECK(d >= 800); CHECK(d <= 72000); if (i < 5) CHECK(d > prev * 15 / 10 || i == 0); prev = d; }
  CHECK(prev >= 48000 && prev <= 72000);
  b.reset(); uint32_t d = b.next(0); CHECK(d >= 800 && d <= 1200);
}

// ---------------------------------------------------------------- node integration
struct N {
  MemStore st; Config c; Node n; uint32_t t = 0; uint32_t epoch = EP; int rssi = -60; int adc = 2000;
  N(bool valid_time = true, bool tweak = false) { if (tweak) c.local_autoshutoff = true; n.begin(c, &st, 0xC0FFEEu, 0); if (!valid_time) epoch = 0; }
  void run(int secs, float lpm, bool drift_epoch = true) {
    for (int i = 0; i < secs; i++) {
      TickInput in; in.mono_s = ++t; in.pulses = pulsesFor(lpm); in.dt_ms = 1000; in.adc_mv = (t % 30 == 0) ? adc : -1;
      if (epoch) { if (drift_epoch) epoch++; } in.epoch = epoch; in.rssi = rssi; in.line_level = t & 1;
      n.tick(in);
    }
  }
};

TEST(node_reading_cadence_fields_and_totals) {
  N a; a.run(60, 6.0f);
  CHECK(a.n.ring().size() == 1);
  const Reading *r = a.n.nextToSend(a.t, a.epoch);
  CHECK(r != nullptr); NEAR(r->flow, 6.0, 0.15); NEAR(r->vol, 6.0, 0.2); NEAR(r->tot, r->vol, 1e-6);
  CHECK(r->bat >= 70 && r->bat <= 85 && r->has_vbat && r->rssi == -60 && r->ts == a.epoch);
  a.run(60, 0); a.run(60, 0);
  CHECK(a.n.ring().size() == 3);
  a.n.sent(r->id);
  CHECK(a.n.ring().size() == 2);
}

TEST(node_no_bogus_timestamps_until_sntp) {
  N a(false);
  a.run(130, 1.0f);
  CHECK(!a.n.timeOk()); CHECK(a.n.ring().size() == 2);
  CHECK(a.n.nextToSend(a.t, 0) == nullptr);        // nothing publishable without time
  CHECK(!a.n.heartbeatDue(a.t));
  a.epoch = EP; // SNTP syncs
  a.run(200, 1.0f);
  const Reading *r = a.n.nextToSend(a.t, a.epoch);
  CHECK(r && r->ts == a.epoch - (a.t - r->mono_s)); CHECK(r->err & errBit(ERR_TS_ESTIMATED));
  CHECK(a.n.heartbeatDue(a.t)); CHECK(!a.n.heartbeatDue(a.t + 1));
}

TEST(node_offline_buffer_replay_oldest_first_with_original_ts) {
  N a; a.run(60 * 5, 2.0f);                       // 5 readings accumulate while MQTT is down
  CHECK(a.n.ring().size() == 5);
  uint32_t lastts = 0, cnt = 0;
  while (const Reading *r = a.n.nextToSend(a.t, a.epoch)) { CHECK(r->ts > lastts); lastts = r->ts; cnt++; a.n.sent(r->id); }
  CHECK(cnt == 5);
  CHECK(lastts == EP + 300 && lastts - 240 == EP + 60);   // ORIGINAL capture timestamps
}

TEST(node_backfill_window_and_drop_flag) {
  N a; a.run(60 * 15, 2.0f);                      // 15 min outage > MAX_BACKFILL_S (540 s) and > ring cap?
  size_t before = a.n.ring().size();
  const Reading *r = a.n.nextToSend(a.t, a.epoch);
  CHECK(r != nullptr);
  CHECK(a.t - r->mono_s <= a.c.max_backfill_s);   // only fresh-enough readings replayed
  CHECK(a.n.ring().size() < before);
  a.run(60, 2.0f);                                // next reading carries the "readings lost" flag
  bool flagged = false; while (const Reading *x = a.n.nextToSend(a.t, a.epoch)) { if (x->err & errBit(ERR_BACKFILL_DROP)) flagged = true; a.n.sent(x->id); }
  CHECK(flagged);
}

TEST(node_ring_overflow_counts_drops) {
  Config c; c.max_backfill_s = 100000; MemStore st; Node n; n.begin(c, &st, 1, 0);
  uint32_t t = 0;
  for (int i = 0; i < (int)AQG_RING_CAP + 5; i++) for (int k = 0; k < 60; k++) { TickInput in; in.mono_s = ++t; in.epoch = EP + t; n.tick(in); }
  CHECK(n.ring().size() == AQG_RING_CAP); CHECK(n.ring().overflowTotal() == 5);
}

TEST(node_leak_flags_reported_not_acted_on) {
  N a;  // default: LOCAL_AUTOSHUTOFF off
  a.run(200, 22.0f);                               // burst
  CHECK(a.n.leak().burstActive());
  CHECK(a.n.valve().state() == VALVE_OPEN);        // only reports
  CHECK(a.n.activeErr() & errBit(ERR_BURST));
  a.run(100, 22.0f);
  CHECK(a.n.valve().state() == VALVE_OPEN);
  const Reading *last = nullptr; while (const Reading *x = a.n.nextToSend(a.t, a.epoch)) { last = x; a.n.sent(x->id); }
  CHECK(last && (last->err & errBit(ERR_BURST)));
}

TEST(node_local_autoshutoff_when_enabled_but_not_on_sensor_fault) {
  N a(true, true);
  a.run(60 + 125, 22.0f);                          // burst sustained, then 120 s active
  CHECK(a.n.valve().state() == VALVE_CLOSED);      // opt-in local shut-off fired
  a.run(5000, 0);                                  // and stays closed forever
  CHECK(a.n.valve().state() == VALVE_CLOSED);
  // sensor noise (impossible flow) must NOT cause a local close
  N b(true, true); b.run(400, 60.0f);
  CHECK(b.n.valve().state() == VALVE_OPEN);
}

TEST(node_night_leak_requires_valid_time) {
  N a; a.epoch = EP - (EP % 86400) + 2 * 3600;     // 02:00 UTC, in the night window
  a.run(700, 1.0f, false);
  CHECK(a.n.leak().nightActive());
  N b(false); b.run(700, 1.0f); CHECK(!b.n.leak().nightActive());
}

TEST(node_boot_error_flag_once_and_battery_in_payload) {
  MemStore st; Config c; Node n; n.begin(c, &st, 7, errBit(ERR_RST_BROWNOUT));
  uint32_t t = 0; for (int i = 0; i < 125; i++) { TickInput in; in.mono_s = ++t; in.epoch = EP + t; in.adc_mv = 1800; n.tick(in); }
  const Reading *r = n.nextToSend(t, EP + t);
  CHECK(r && (r->err & errBit(ERR_RST_BROWNOUT)));
  n.sent(r->id); r = n.nextToSend(t, EP + t);
  CHECK(r && !(r->err & errBit(ERR_RST_BROWNOUT)));
  CHECK(r->bat >= 0 && r->bat < 20 && (r->err & errBit(ERR_BAT_CRIT)));
}

TEST(node_adc_fault_reports_null_battery) {
  N a; a.adc = 0; a.run(120, 0);
  const Reading *r = a.n.nextToSend(a.t, a.epoch); a.n.sent(r->id);   // first reading predates the 3rd bad sample
  r = a.n.nextToSend(a.t, a.epoch);
  CHECK(r && r->bat == -1 && !r->has_vbat && (r->err & errBit(ERR_ADC_FAULT)));
  char b[512]; size_t n = formatFlowJson(*r, b, sizeof b);
  CHECK(n > 0 && strstr(b, "\"bat\":null") && !strstr(b, "vbat"));
}

// ---------------------------------------------------------------- JSON
TEST(json_formats) {
  Reading r; r.id = 0xa1b2c3d4; r.ts = EP; r.flow = 0.4f; r.vol = 1234.5f; r.tot = 98765.4321; r.bat = 87; r.rssi = -61;
  r.vbat = 4.01f; r.has_vbat = true; r.valve_closed = 0; r.err = 0;
  char b[512]; size_t n = formatFlowJson(r, b, sizeof b);
  CHECK(n == strlen(b));
  const char *pre = "{\"v\":1,\"id\":\"a1b2c3d4\",\"ts\":1760000000,\"flow\":0.400,\"vol\":1234.500,\"psi\":null,\"tmp\":null,\"bat\":87,\"rssi\":-61";
  CHECK(strncmp(b, pre, strlen(pre)) == 0);
  CHECK(strstr(b, "\"valve\":\"open\"") && strstr(b, "\"fw\":\"") && !strstr(b, "\"err\""));
  r.err = errBit(ERR_BURST) | errBit(ERR_LEAK_CONT); r.valve_closed = 1; formatFlowJson(r, b, sizeof b);
  CHECK(strstr(b, "\"err\":[\"burst\",\"leak_cont\"]") && strstr(b, "\"valve\":\"closed\""));
  r.err = 0x7FFF; formatFlowJson(r, b, sizeof b);
  const char *names[16]; CHECK(errList(0x7FFF, names, 8) == 8); CHECK(errList(0x7FFF, names, 16) == ERR_COUNT);
  r.rssi = 1000; r.flow = 9999; r.vol = 1e9f; formatFlowJson(r, b, sizeof b);
  CHECK(strstr(b, "\"rssi\":null") && strstr(b, "\"flow\":500.000") && strstr(b, "\"vol\":100000.000"));
  CHECK(formatFlowJson(r, b, 40) == 0);              // too small => 0, no overflow
  char h[128]; CHECK(formatHeartbeatJson(EP, 3600, 87, h, sizeof h) > 0 && strstr(h, "\"uptime\":3600") && strstr(h, "\"bat\":87"));
  CHECK(formatHeartbeatJson(EP, 1, -1, h, sizeof h) > 0 && strstr(h, "\"bat\":null"));
  char a[128]; CHECK(formatValveAckJson("00ff10ab", true, EP, a, sizeof a) > 0);
  CHECK(!strcmp(a, "{\"v\":1,\"cmd_id\":\"00ff10ab\",\"state\":\"closed\",\"ts\":1760000000}"));
  for (int i = 0; i < ERR_COUNT; i++) { CHECK(strlen(errName((ErrCode)i)) <= 16 && strlen(errName((ErrCode)i)) >= 1); }
}

int main() {
  for (int i = 0; i < g_n; i++) { int before = g_fail; g_tests++; printf("[ RUN ] %s\n", g_t[i].n); g_t[i].f(); printf("[ %s ] %s\n", g_fail == before ? " OK " : "FAIL", g_t[i].n); }
  printf("\n%d tests, %d checks, %d failures\n", g_tests, g_checks, g_fail);
  return g_fail ? 1 : 0;
}

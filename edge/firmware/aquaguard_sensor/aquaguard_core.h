// AquaGuard firmware core: portable, allocation-free C++17, NO Arduino / ESP-IDF dependencies.
// Everything safety-relevant (flow maths, leak detection, valve state machine, command guard,
// message ids, offline ring, JSON payloads) lives here so it can be unit-tested on a host.
// The .ino only wires hardware (ISR, relay pin, NVS, Wi-Fi, MQTT) to these classes.
//
// STATUS: host-tested only. NOT validated on real hardware, valves or plumbing.
#pragma once
#include <stddef.h>
#include <stdint.h>

#ifndef AQG_RING_CAP
#define AQG_RING_CAP 16  // offline readings kept in RAM (~64 B each)
#endif
#define AQG_FW_VERSION "2.0.0-dev"

namespace aqg {

// ------------------------------------------------------------------ error / alert codes
// Order = priority when more than 8 are active (backend accepts at most 8 entries of <=24 chars).
enum ErrCode : uint8_t {
  ERR_BURST = 0,
  ERR_LEAK_CONT,
  ERR_LEAK_NIGHT,
  ERR_CLOSE_UNCONF,   // valve commanded closed but flow did not stop (or restarted)
  ERR_FLOW_IMPOSSIBLE,
  ERR_STUCK_LINE,
  ERR_ADC_FAULT,
  ERR_BAT_CRIT,
  ERR_BAT_LOW,
  ERR_COIL_DUTY,
  ERR_BACKFILL_DROP,  // readings were dropped (ring overflow / older than MAX_BACKFILL_S)
  ERR_TS_ESTIMATED,   // timestamp reconstructed after late SNTP sync
  ERR_STORE_FAIL,     // persistent storage write failed
  ERR_RST_BROWNOUT,
  ERR_RST_WDT,
  ERR_COUNT
};
typedef uint32_t ErrMask;
inline ErrMask errBit(ErrCode c) { return (ErrMask)1u << c; }
const char *errName(ErrCode c);  // <= 16 chars, [a-z_]
const int MAX_ERR_CODES = 8;

// ------------------------------------------------------------------ configuration
struct Config {
  // --- flow sensor (YF-S201). 7.5 Hz per L/min nominal => 450 pulses/L. CALIBRATE per unit.
  float pulses_per_litre = 450.0f;
  uint32_t min_pulse_gap_us = 1500;  // edges closer than this are rejected (>666 Hz impossible)
  uint8_t window_s = 10;             // averaging window for leak detection (<= 60)
  float sensor_max_lpm = 30.0f;      // YF-S201 datasheet range upper end
  float impossible_margin = 1.2f;    // flag when 1 s flow > max * margin ...
  uint8_t impossible_samples = 3;    // ... on this many consecutive seconds
  uint32_t stuck_line_s = 604800;    // advisory: no edge and constant level this long (7 d)

  // --- reporting
  uint32_t flow_period_s = 60;
  uint32_t heartbeat_period_s = 300;
  uint32_t max_backfill_s = 600;  // backend drops readings older than ~this (docs/hardware)
  uint32_t time_valid_min_epoch = 1735689600u;  // 2025-01-01: anything below = SNTP not synced

  // --- cumulative volume persistence (wear aware)
  float persist_litres = 1.0f;
  uint32_t persist_interval_s = 300;
  uint32_t persist_min_gap_s = 60;  // never write more often than this

  // --- local leak / overuse detector (REPORT ONLY unless local_autoshutoff)
  uint32_t continuous_flow_min = 120;  // minutes of flow without a zero-gap
  float min_leak_flow_lpm = 0.5f;      // YF-S201 is unreliable below ~1 L/min: slow drips invisible
  float burst_flow_lpm = 20.0f;
  uint32_t burst_sustain_s = 60;
  float burst_clear_ratio = 0.7f;  // hysteresis: stays active until flow < ratio*burst ...
  uint32_t burst_clear_s = 30;     // ... for this long
  uint32_t zero_gap_reset_s = 120;
  uint8_t night_start_h = 1, night_end_h = 5;  // local hours [start,end); wraps midnight
  uint32_t night_sustain_s = 600;
  int32_t tz_offset_s = 0;  // local = UTC + offset (no DST handling). SET FOR YOUR SITE.
  bool local_autoshutoff = false;  // LOCAL_AUTOSHUTOFF_ENABLED: default OFF
  uint32_t local_shutoff_burst_s = 120;

  // --- battery (divider ratio = (R1+R2)/R2). battery_present=false => bat/vbat reported as absent
  bool battery_present = true;
  float divider_ratio = 2.0f;
  float bat_low_set = 20, bat_low_clr = 25, bat_crit_set = 10, bat_crit_clr = 15;
  float vbat_min_ok = 2.5f, vbat_max_ok = 4.5f;  // outside => ADC fault (matches backend range)
  uint8_t adc_fault_samples = 3;

  // --- valve
  uint32_t cmd_min_interval_s = 30;
  uint32_t cmd_max_toggles_per_hour = 10;
  uint32_t cmd_max_age_s = 120;
  uint32_t cmd_max_future_s = 60;
  bool accept_auto_close = false;  // ACCEPT_AUTO_CLOSE: commissioning gate, default OFF
  uint32_t close_settle_s = 15;    // must exceed window_s
  uint32_t close_confirm_timeout_s = 60;
  float closed_flow_eps_lpm = 0.2f;
  uint32_t closed_leak_s = 30;  // flow this long while "closed" => close_unconf
  uint32_t coil_duty_warn_s = 0;  // 0 = disabled; set from the valve datasheet duty rating

  // --- message ids
  uint32_t id_reserve = 4096;
};

// ------------------------------------------------------------------ helpers
bool timeValid(uint32_t epoch, const Config &c);
int localHour(uint32_t epoch, int32_t tz_offset_s);
bool inNightWindow(int hour, uint8_t start_h, uint8_t end_h);
uint32_t fnv1a32(const char *s);
float relayLevelIsHigh(bool energized, bool active_low);  // 1.0 / 0.0 (kept float-free of Arduino)

// ------------------------------------------------------------------ persistent store
class KvStore {
 public:
  virtual ~KvStore() {}
  virtual bool getU64(const char *key, uint64_t *out) = 0;
  virtual bool putU64(const char *key, uint64_t v) = 0;
};
// RAM implementation for tests / host programs. Counts writes; can simulate failure.
class MemStore : public KvStore {
 public:
  bool fail_writes = false;
  uint32_t writes = 0;
  bool getU64(const char *key, uint64_t *out) override;
  bool putU64(const char *key, uint64_t v) override;
 private:
  struct Slot { char key[12]; uint64_t v; bool used; } slots_[8] = {};
};

// ------------------------------------------------------------------ pulse debounce (ISR safe)
struct PulseDebouncer {
  uint32_t last_us = 0;
  bool have_last = false;
  // Returns true when this edge is accepted as a pulse. Wrap-safe (uint32 micros()).
  inline bool onEdge(uint32_t now_us, uint32_t min_gap_us) {
    if (have_last && (uint32_t)(now_us - last_us) < min_gap_us) return false;
    last_us = now_us;
    have_last = true;
    return true;
  }
};

// ------------------------------------------------------------------ flow meter
class FlowMeter {
 public:
  void begin(const Config &c, uint64_t persisted_total_pulses);
  // Called once per tick (nominally 1 s). Returns instantaneous flow for this tick (L/min).
  float addSample(uint32_t pulses, uint32_t dt_ms);
  float windowLpm() const;   // mean over the last window_s ticks
  float instantLpm() const { return last_lpm_; }
  uint64_t totalPulses() const { return base_pulses_ + session_pulses_; }
  uint64_t sessionPulses() const { return session_pulses_; }
  double totalLitres() const { return totalPulses() / (double)ppl_; }
  double sessionLitres() const { return session_pulses_ / (double)ppl_; }
  // pulses/ms since the last call to takePeriod(): mean flow over a reporting period
  float takePeriodLpm(uint32_t *period_ms = nullptr);
 private:
  float ppl_ = 450;
  uint8_t win_ = 10;
  uint32_t wp_[60] = {}, wd_[60] = {};
  uint8_t idx_ = 0, filled_ = 0;
  uint64_t base_pulses_ = 0, session_pulses_ = 0;
  uint64_t period_pulses_ = 0;
  uint32_t period_ms_ = 0;
  float last_lpm_ = 0;
};
float pulsesToLpm(uint32_t pulses, uint32_t dt_ms, float pulses_per_litre);

// ------------------------------------------------------------------ cumulative persistence
class VolumePersister {
 public:
  void begin(KvStore *s, const Config &c) { store_ = s; cfg_ = c; }
  // Loads persisted lifetime pulses (0 if none). Also resets bookkeeping.
  uint64_t load();
  // Call every tick with current lifetime pulses; writes only when the wear policy says so.
  void update(uint64_t total_pulses, uint32_t mono_s);
  void flush(uint64_t total_pulses, uint32_t mono_s);  // e.g. before a planned restart
  bool lastWriteFailed() const { return failed_; }
 private:
  KvStore *store_ = nullptr;
  Config cfg_;
  uint64_t written_ = 0;
  uint32_t last_write_s_ = 0;
  bool failed_ = false;
  void write(uint64_t total, uint32_t mono_s);
};

// ------------------------------------------------------------------ leak detector
class LeakDetector {
 public:
  void begin(const Config &c) { cfg_ = c; reset(); }
  void reset();
  // 1 Hz. `night` must be false whenever wall-clock time is not valid.
  void step(float window_lpm, bool night);
  bool continuousActive() const { return cont_active_; }
  bool nightActive() const { return night_active_; }
  bool burstActive() const { return burst_active_; }
  uint32_t burstActiveSeconds() const { return burst_active_s_; }
  ErrMask mask() const;
 private:
  struct Run { uint32_t run = 0, gap = 0; };
  void stepRun(Run &r, bool flowing);
  Config cfg_;
  Run cont_, night_;
  bool cont_active_ = false, night_active_ = false, burst_active_ = false, burst_started_ = false;
  uint32_t burst_run_ = 0, burst_clear_ = 0, burst_active_s_ = 0;
};

// ------------------------------------------------------------------ sensor / battery faults
class SensorFaults {
 public:
  void begin(const Config &c) { cfg_ = c; }
  // 1 Hz. valve_closed resets the stuck timer (no flow expected).
  void step(uint32_t mono_s, bool line_level, uint32_t pulses, float inst_lpm, bool valve_closed);
  ErrMask mask() const;
  bool impossible() const { return impossible_now_; }
 private:
  Config cfg_;
  bool have_level_ = false, last_level_ = false, impossible_now_ = false;
  uint32_t last_change_s_ = 0;
  uint8_t over_ = 0;
};

class BatteryMonitor {
 public:
  void begin(const Config &c) { cfg_ = c; }
  // vbat_mv < 0 => no reading. Divider/attenuation already applied by caller? NO: pass the ADC pin
  // millivolts; the divider ratio is applied here.
  void update(int adc_mv);
  bool valid() const { return valid_; }
  float vbat() const { return vbat_; }
  int percent() const { return percent_; }  // -1 if invalid
  bool low() const { return low_; }
  bool critical() const { return crit_; }
  bool adcFault() const { return adc_fault_; }
  ErrMask mask() const;
 private:
  Config cfg_;
  bool valid_ = false, low_ = false, crit_ = false, adc_fault_ = false;
  uint8_t bad_ = 0;
  float vbat_ = 0;
  int percent_ = -1;
};
float vbatToPercent(float volts);  // APPROXIMATE open-circuit Li-ion table, piecewise linear

// ------------------------------------------------------------------ message ids
// 32-bit ids, bijective in a persisted counter, so they never repeat across reboots (until 2^32
// messages). Counter high-water mark is persisted in blocks of cfg.id_reserve.
class MessageIdGenerator {
 public:
  // entropy32: used ONLY when the store has no counter (first boot / erased flash).
  void begin(KvStore *s, uint32_t id_reserve, uint32_t entropy32);
  uint32_t next();
  void format(uint32_t id, char out[9]) const;
  bool storeFailed() const { return failed_; }
  bool freshStart() const { return fresh_; }
 private:
  KvStore *store_ = nullptr;
  uint32_t counter_ = 0, reserved_ = 0, reserve_ = 4096;
  bool failed_ = false, fresh_ = false;
};

// ------------------------------------------------------------------ readings + ring buffer
struct Reading {
  uint32_t id = 0;
  uint32_t mono_s = 0;  // uptime seconds at capture
  uint32_t ts = 0;      // epoch seconds; 0 = not yet resolved (time not valid at capture)
  float flow = 0, vol = 0, vbat = 0;
  double tot = 0;
  int8_t bat = -1;      // -1 = null
  int16_t rssi = 1000;  // 1000 = null
  bool has_vbat = false;
  uint8_t valve_closed = 0;
  ErrMask err = 0;
};

class ReadingRing {
 public:
  void push(const Reading &r);  // overwrites oldest when full (counted)
  size_t size() const { return n_; }
  // Resolve ts for entries captured before SNTP sync: ts = epoch_now - (mono_now - mono_s).
  void resolveTimestamps(uint32_t epoch_now, uint32_t mono_now);
  // Drop entries older than max_age_s (by uptime). Returns number dropped.
  uint32_t dropStale(uint32_t mono_now, uint32_t max_age_s);
  // Oldest entry with a resolved ts, or nullptr (oldest-first ordering is preserved).
  const Reading *oldest() const;
  bool popOldestIfId(uint32_t id);
  uint32_t takeDropped();  // number lost since last call (overflow + stale)
  uint32_t overflowTotal() const { return overflow_total_; }
 private:
  Reading buf_[AQG_RING_CAP];
  size_t head_ = 0, n_ = 0;
  uint32_t dropped_ = 0, overflow_total_ = 0;
};

// ------------------------------------------------------------------ valve
enum ValveState : uint8_t { VALVE_OPEN = 0, VALVE_CLOSED = 1 };
enum CloseConfirm : uint8_t { CC_NONE, CC_PENDING, CC_CONFIRMED, CC_UNVERIFIABLE, CC_FAILED };

struct RawCommand {  // fields as parsed from JSON; pointers only need to live for the call
  int v = 0;
  const char *id = nullptr;
  const char *cmd = nullptr;
  const char *src = nullptr;
  bool has_ts = false;
  int64_t ts = 0;
};
enum Verdict : uint8_t {
  CMD_APPLIED,          // state changed
  CMD_NOOP,             // valid, already in requested state (acked)
  CMD_DUPLICATE,        // id already seen: re-ack current state, do nothing
  CMD_REJECT_MALFORMED, // no ack
  CMD_REJECT_STALE,     // ack current state
  CMD_REJECT_TIME,      // open requires valid time: ack current state
  CMD_REJECT_SOURCE,    // open from non-manual / auto close not accepted: ack current state
  CMD_REJECT_RATE       // chatter protection: ack current state
};
const char *verdictName(Verdict v);
inline bool verdictShouldAck(Verdict v) { return v != CMD_REJECT_MALFORMED; }

class ValveController {
 public:
  void begin(KvStore *s, const Config &c);
  // 1) Read persisted intent. 2) Caller drives the relay from energized() IMMEDIATELY, before any
  // radio is started. Returns desired state.
  ValveState bootAssert(uint32_t mono_s);
  Verdict handleCommand(const RawCommand &c, uint32_t mono_s, uint32_t epoch, bool time_ok);
  // Local (non-remote) close request, used only by the optional local auto shut-off.
  bool requestLocalClose(uint32_t mono_s, float window_lpm);
  // 1 Hz: close verification + coil duty.
  void tick(uint32_t mono_s, float window_lpm);
  ValveState state() const { return state_; }
  bool energized() const { return state_ == VALVE_CLOSED; }  // normally-OPEN valve
  CloseConfirm confirm() const { return confirm_; }
  ErrMask mask() const;
  bool storeFailed() const { return store_failed_; }
  const char *lastCmdId() const { return last_id_; }
  const char *stateName() const { return state_ == VALVE_CLOSED ? "closed" : "open"; }
  // There is NO method that opens the valve from comms loss, a sensor fault or a timer.
 private:
  void applyState(ValveState s, uint32_t mono_s, float flow_hint);
  bool rateOk(bool is_open, bool emergency, uint32_t mono_s);
  void remember(const char *id);
  bool seen(const char *id) const;
  KvStore *store_ = nullptr;
  Config cfg_;
  ValveState state_ = VALVE_OPEN;
  CloseConfirm confirm_ = CC_NONE;
  uint32_t close_s_ = 0, energized_s_ = 0, closed_flow_s_ = 0;
  bool saw_flow_ = false, store_failed_ = false;
  char ring_[16][17] = {};
  uint8_t ring_n_ = 0, ring_i_ = 0;
  char last_id_[17] = {};
  uint32_t toggles_[32] = {};  // actuation times (uptime s), newest overwrite
  uint8_t tog_n_ = 0, tog_i_ = 0;
  uint32_t last_act_s_ = 0;
  bool have_act_ = false;
};

// ------------------------------------------------------------------ backoff
class Backoff {
 public:
  Backoff(uint32_t base_ms = 1000, uint32_t max_ms = 60000) : base_(base_ms), max_(max_ms), cur_(base_ms) {}
  uint32_t next(uint32_t rnd = 0);  // returns delay (+/-20 % jitter from rnd), then doubles
  void reset() { cur_ = base_; }
 private:
  uint32_t base_, max_, cur_;
};

// ------------------------------------------------------------------ the node (glue, 1 Hz)
struct TickInput {
  uint32_t mono_s = 0;      // uptime seconds
  uint32_t pulses = 0;      // accepted pulses since the previous tick
  uint32_t dt_ms = 1000;    // measured time since previous tick
  bool line_level = false;  // raw flow pin level
  int adc_mv = -1;          // battery ADC pin millivolts, -1 if unavailable
  uint32_t epoch = 0;       // 0 / invalid => SNTP not synced
  int rssi = 1000;          // 1000 => Wi-Fi down
};

class Node {
 public:
  void begin(const Config &c, KvStore *store, uint32_t entropy32, ErrMask boot_err);
  // Boot step 1 (before radios): returns whether the relay must be energized right now.
  bool bootValve(uint32_t mono_s) { return valve_.bootAssert(mono_s) == VALVE_CLOSED; }
  void tick(const TickInput &in);
  // Handle a command; `ack_needed` tells whether to publish valve/state with cmd id.
  Verdict command(const RawCommand &c, uint32_t mono_s, uint32_t epoch);
  bool heartbeatDue(uint32_t mono_s);
  // Next reading to transmit (oldest first, stale dropped, timestamps resolved) or nullptr.
  const Reading *nextToSend(uint32_t mono_s, uint32_t epoch);
  void sent(uint32_t id) { ring_.popOldestIfId(id); }
  bool localCloseWanted() const { return local_close_; }
  void localCloseDone() { local_close_ = false; }
  void flushPersist(uint32_t mono_s) { vol_.flush(flow_.totalPulses(), mono_s); }

  ValveController &valve() { return valve_; }
  FlowMeter &flow() { return flow_; }
  LeakDetector &leak() { return leak_; }
  BatteryMonitor &battery() { return bat_; }
  ReadingRing &ring() { return ring_; }
  bool timeOk() const { return time_ok_; }
  ErrMask activeErr() const;
  const Config &config() const { return cfg_; }
 private:
  Config cfg_;
  FlowMeter flow_;
  VolumePersister vol_;
  LeakDetector leak_;
  SensorFaults faults_;
  BatteryMonitor bat_;
  MessageIdGenerator ids_;
  ValveController valve_;
  ReadingRing ring_;
  KvStore *store_ = nullptr;
  bool time_ok_ = false, local_close_ = false;
  uint32_t epoch_now_ = 0, last_reading_s_ = 0, last_beat_s_ = 0;
  uint32_t last_tick_s_ = 0;
  ErrMask period_err_ = 0, boot_err_ = 0;
  int rssi_ = 1000;
  bool backfill_flag_ = false;
  uint32_t local_ok_run_ = 0;
};

// ------------------------------------------------------------------ JSON (no dynamic memory)
// Return length written, or 0 if the buffer is too small.
size_t formatFlowJson(const Reading &r, char *buf, size_t n);
size_t formatHeartbeatJson(uint32_t ts, uint32_t uptime_s, int bat_percent, char *buf, size_t n);
size_t formatValveAckJson(const char *cmd_id, bool closed, uint32_t ts, char *buf, size_t n);
size_t errList(ErrMask m, const char **names, size_t max);  // priority order, <= max

}  // namespace aqg

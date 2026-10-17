// See aquaguard_core.h. Pure C++17, no Arduino/ESP-IDF dependencies, no heap.
#include "aquaguard_core.h"

#include <stdio.h>
#include <string.h>

namespace aqg {

// ================================================================== codes / helpers
static const char *const kErrNames[ERR_COUNT] = {
    "burst",      "leak_cont",    "leak_night",  "close_unconf", "flow_impossible",
    "stuck_line", "adc_fault",    "bat_crit",    "bat_low",      "coil_duty",
    "backfill_drop", "ts_est",    "store_fail",  "rst_brownout", "rst_wdt"};

const char *errName(ErrCode c) { return c < ERR_COUNT ? kErrNames[c] : "unknown"; }

size_t errList(ErrMask m, const char **names, size_t max) {
  size_t n = 0;
  for (int i = 0; i < ERR_COUNT && n < max; i++)
    if (m & ((ErrMask)1u << i)) names[n++] = kErrNames[i];
  return n;
}

bool timeValid(uint32_t epoch, const Config &c) { return epoch >= c.time_valid_min_epoch; }

int localHour(uint32_t epoch, int32_t tz) {
  int64_t t = (int64_t)epoch + tz;
  int64_t s = t % 86400;
  if (s < 0) s += 86400;
  return (int)(s / 3600);
}

bool inNightWindow(int h, uint8_t start_h, uint8_t end_h) {
  if (start_h == end_h) return false;
  if (start_h < end_h) return h >= start_h && h < end_h;
  return h >= start_h || h < end_h;  // wraps midnight
}

uint32_t fnv1a32(const char *s) {
  uint32_t h = 2166136261u;
  while (*s) { h ^= (uint8_t)*s++; h *= 16777619u; }
  return h;
}

bool relayPinHigh(bool energized, bool active_low) { return energized != active_low; }

// ================================================================== MemStore
bool MemStore::getU64(const char *key, uint64_t *out) {
  for (auto &s : slots_)
    if (s.used && strncmp(s.key, key, sizeof s.key) == 0) { *out = s.v; return true; }
  return false;
}
bool MemStore::putU64(const char *key, uint64_t v) {
  if (fail_writes) return false;
  Slot *free_slot = nullptr;
  for (auto &s : slots_) {
    if (s.used && strncmp(s.key, key, sizeof s.key) == 0) { s.v = v; writes++; return true; }
    if (!s.used && !free_slot) free_slot = &s;
  }
  if (!free_slot) return false;
  strncpy(free_slot->key, key, sizeof free_slot->key - 1);
  free_slot->key[sizeof free_slot->key - 1] = 0;
  free_slot->v = v;
  free_slot->used = true;
  writes++;
  return true;
}

// ================================================================== flow meter
float pulsesToLpm(uint32_t pulses, uint32_t dt_ms, float ppl) {
  if (dt_ms == 0 || ppl <= 0) return 0;
  return (float)((double)pulses * 60000.0 / ((double)ppl * (double)dt_ms));
}

void FlowMeter::begin(const Config &c, uint64_t persisted) {
  ppl_ = c.pulses_per_litre > 0 ? c.pulses_per_litre : 450.0f;
  win_ = c.window_s < 1 ? 1 : (c.window_s > 60 ? 60 : c.window_s);
  base_pulses_ = persisted;
  session_pulses_ = period_pulses_ = 0;
  period_ms_ = 0;
  idx_ = filled_ = 0;
  last_lpm_ = 0;
  memset(wp_, 0, sizeof wp_);
  memset(wd_, 0, sizeof wd_);
}

float FlowMeter::addSample(uint32_t pulses, uint32_t dt_ms) {
  wp_[idx_] = pulses;
  wd_[idx_] = dt_ms;
  idx_ = (uint8_t)((idx_ + 1) % win_);
  if (filled_ < win_) filled_++;
  session_pulses_ += pulses;
  period_pulses_ += pulses;
  period_ms_ += dt_ms;
  last_lpm_ = pulsesToLpm(pulses, dt_ms, ppl_);
  return last_lpm_;
}

float FlowMeter::windowLpm() const {
  uint64_t p = 0, d = 0;
  for (uint8_t i = 0; i < filled_; i++) { p += wp_[i]; d += wd_[i]; }
  if (d == 0) return 0;
  return (float)((double)p * 60000.0 / ((double)ppl_ * (double)d));
}

float FlowMeter::takePeriodLpm(uint32_t *period_ms) {
  float v = period_ms_ ? (float)((double)period_pulses_ * 60000.0 / ((double)ppl_ * period_ms_)) : 0;
  if (period_ms) *period_ms = period_ms_;
  period_pulses_ = 0;
  period_ms_ = 0;
  return v;
}

// ================================================================== volume persistence
uint64_t VolumePersister::load() {
  uint64_t v = 0;
  if (!store_ || !store_->getU64("pulses", &v)) v = 0;
  written_ = v;
  last_write_s_ = 0;
  failed_ = false;
  return v;
}

void VolumePersister::write(uint64_t total, uint32_t mono_s) {
  last_write_s_ = mono_s;  // also on failure: do not hammer a failing flash
  if (store_ && store_->putU64("pulses", total)) { written_ = total; failed_ = false; }
  else failed_ = true;
}

void VolumePersister::update(uint64_t total, uint32_t mono_s) {
  if (total <= written_ && !failed_) return;
  if ((uint32_t)(mono_s - last_write_s_) < cfg_.persist_min_gap_s) return;
  double litres = (double)(total - written_) / (cfg_.pulses_per_litre > 0 ? cfg_.pulses_per_litre : 450.0);
  bool by_volume = litres >= cfg_.persist_litres;
  bool by_time = (uint32_t)(mono_s - last_write_s_) >= cfg_.persist_interval_s;
  if (by_volume || by_time) write(total, mono_s);
}

void VolumePersister::flush(uint64_t total, uint32_t mono_s) {
  if (total != written_) write(total, mono_s);
}

// ================================================================== leak detector
void LeakDetector::reset() {
  cont_ = Run();
  night_ = Run();
  cont_active_ = night_active_ = burst_active_ = burst_started_ = false;
  burst_run_ = burst_clear_ = burst_active_s_ = 0;
}

void LeakDetector::stepRun(Run &r, bool flowing) {
  if (flowing) r.gap = 0; else r.gap++;
  if (r.run == 0) {
    if (flowing) r.run = 1;
  } else if (r.gap >= cfg_.zero_gap_reset_s) {
    r.run = 0;  // a real pause: normal use, start over
  } else {
    r.run++;    // short gaps (<zero_gap_reset_s) do not interrupt a suspicious run
  }
}

void LeakDetector::step(float lpm, bool night) {
  bool flowing = lpm >= cfg_.min_leak_flow_lpm;

  stepRun(cont_, flowing);
  cont_active_ = cont_.run >= (uint64_t)cfg_.continuous_flow_min * 60u;

  if (night) {
    stepRun(night_, flowing);
    night_active_ = night_.run >= cfg_.night_sustain_s;
  } else {
    night_ = Run();
    night_active_ = false;
  }

  // burst: starts only at >= burst_flow; once started tolerates dips down to ratio*burst
  float hi = cfg_.burst_flow_lpm, lo = hi * cfg_.burst_clear_ratio;
  if (!burst_active_) {
    if (lpm >= hi || (burst_started_ && lpm >= lo)) {
      burst_started_ = true;
      burst_run_++;
    } else {
      burst_started_ = false;
      burst_run_ = 0;  // brief spike or normal use: forgotten
    }
    if (burst_run_ >= cfg_.burst_sustain_s) { burst_active_ = true; burst_clear_ = 0; burst_active_s_ = 0; }
  } else {
    burst_active_s_++;
    if (lpm >= lo) burst_clear_ = 0; else burst_clear_++;
    if (burst_clear_ >= cfg_.burst_clear_s) {
      burst_active_ = burst_started_ = false;
      burst_run_ = burst_clear_ = burst_active_s_ = 0;
    }
  }
}

ErrMask LeakDetector::mask() const {
  return (burst_active_ ? errBit(ERR_BURST) : 0) | (cont_active_ ? errBit(ERR_LEAK_CONT) : 0) |
         (night_active_ ? errBit(ERR_LEAK_NIGHT) : 0);
}

// ================================================================== sensor faults
void SensorFaults::step(uint32_t mono_s, bool level, uint32_t pulses, float inst_lpm, bool valve_closed) {
  if (!have_level_) { have_level_ = true; last_level_ = level; last_change_s_ = mono_s; }
  if (pulses > 0 || level != last_level_ || valve_closed) last_change_s_ = mono_s;
  last_level_ = level;
  if (inst_lpm > cfg_.sensor_max_lpm * cfg_.impossible_margin) { if (over_ < 255) over_++; } else over_ = 0;
  impossible_now_ = over_ >= cfg_.impossible_samples;
  mono_ = mono_s;
}

ErrMask SensorFaults::mask() const {
  ErrMask m = 0;
  if (impossible_now_) m |= errBit(ERR_FLOW_IMPOSSIBLE);
  if (have_level_ && (uint32_t)(mono_ - last_change_s_) >= cfg_.stuck_line_s) m |= errBit(ERR_STUCK_LINE);
  return m;
}

// ================================================================== battery
// APPROXIMATE resting open-circuit voltage -> state of charge for a generic 1S Li-ion/LiPo cell.
// Real cells differ with chemistry, temperature, age and load. Calibrate against a discharge log.
static const struct { float v; float pct; } kBatTable[] = {
    {3.27f, 0},  {3.61f, 5},  {3.69f, 10}, {3.71f, 15}, {3.73f, 20}, {3.75f, 25}, {3.77f, 30},
    {3.79f, 35}, {3.80f, 40}, {3.82f, 45}, {3.84f, 50}, {3.85f, 55}, {3.87f, 60}, {3.91f, 65},
    {3.95f, 70}, {3.98f, 75}, {4.02f, 80}, {4.08f, 85}, {4.11f, 90}, {4.15f, 95}, {4.20f, 100}};

float vbatToPercent(float v) {
  const int n = (int)(sizeof kBatTable / sizeof kBatTable[0]);
  if (v <= kBatTable[0].v) return 0;
  if (v >= kBatTable[n - 1].v) return 100;
  for (int i = 1; i < n; i++)
    if (v <= kBatTable[i].v) {
      float f = (v - kBatTable[i - 1].v) / (kBatTable[i].v - kBatTable[i - 1].v);
      return kBatTable[i - 1].pct + f * (kBatTable[i].pct - kBatTable[i - 1].pct);
    }
  return 100;
}

void BatteryMonitor::update(int adc_mv) {
  if (!cfg_.battery_present || adc_mv < 0) return;
  float v = adc_mv / 1000.0f * cfg_.divider_ratio;
  if (v < cfg_.vbat_min_ok || v > cfg_.vbat_max_ok) {
    if (bad_ < 255) bad_++;
    if (bad_ >= cfg_.adc_fault_samples) { adc_fault_ = true; valid_ = false; percent_ = -1; }
    return;
  }
  bad_ = 0;
  adc_fault_ = false;
  valid_ = true;
  vbat_ = v;
  float p = vbatToPercent(v);
  percent_ = (int)(p + 0.5f);
  // hysteresis: set at/below *_set, clear only at/above *_clr
  if (!crit_ && p <= cfg_.bat_crit_set) crit_ = true;
  else if (crit_ && p >= cfg_.bat_crit_clr) crit_ = false;
  if (!low_ && p <= cfg_.bat_low_set) low_ = true;
  else if (low_ && p >= cfg_.bat_low_clr) low_ = false;
  if (crit_) low_ = true;
}

ErrMask BatteryMonitor::mask() const {
  ErrMask m = 0;
  if (adc_fault_) m |= errBit(ERR_ADC_FAULT);
  if (crit_) m |= errBit(ERR_BAT_CRIT);
  else if (low_) m |= errBit(ERR_BAT_LOW);
  return m;
}

// ================================================================== message ids
static const uint32_t kIdMul = 2654435761u;  // odd => multiplication mod 2^32 is a bijection

void MessageIdGenerator::begin(KvStore *s, uint32_t reserve, uint32_t entropy32) {
  store_ = s;
  reserve_ = reserve ? reserve : 4096;
  uint64_t v = 0;
  if (s && s->getU64("idctr", &v)) { counter_ = (uint32_t)v; fresh_ = false; }
  else { counter_ = entropy32; fresh_ = true; }
  reserved_ = counter_ + reserve_;
  if (!s || !s->putU64("idctr", reserved_)) {
    failed_ = true;
    counter_ += entropy32 | 1u;  // cannot persist: jump randomly to make reuse unlikely
    reserved_ = counter_ + reserve_;
  }
  left_ = reserve_;
}

uint32_t MessageIdGenerator::next() {
  if (left_ == 0) {
    reserved_ += reserve_;
    if (!store_ || !store_->putU64("idctr", reserved_)) failed_ = true;
    left_ = reserve_;
  }
  uint32_t id = counter_ * kIdMul;
  counter_++;
  left_--;
  return id;
}

void MessageIdGenerator::format(uint32_t id, char out[9]) const { snprintf(out, 9, "%08x", (unsigned)id); }

// ================================================================== ring buffer
void ReadingRing::push(const Reading &r) {
  if (n_ == AQG_RING_CAP) {
    head_ = (head_ + 1) % AQG_RING_CAP;
    n_--;
    dropped_++;
    overflow_total_++;
  }
  buf_[(head_ + n_) % AQG_RING_CAP] = r;
  n_++;
}

void ReadingRing::resolveTimestamps(uint32_t epoch_now, uint32_t mono_now) {
  for (size_t i = 0; i < n_; i++) {
    Reading &r = buf_[(head_ + i) % AQG_RING_CAP];
    if (r.ts == 0) {
      uint32_t age = mono_now - r.mono_s;
      if (age <= epoch_now) {
        r.ts = epoch_now - age;
        r.err |= errBit(ERR_TS_ESTIMATED);
      }
    }
  }
}

uint32_t ReadingRing::dropStale(uint32_t mono_now, uint32_t max_age_s) {
  uint32_t d = 0;
  while (n_ > 0 && (uint32_t)(mono_now - buf_[head_].mono_s) > max_age_s) {
    head_ = (head_ + 1) % AQG_RING_CAP;
    n_--;
    d++;
  }
  dropped_ += d;
  return d;
}

const Reading *ReadingRing::oldest() const {
  if (n_ == 0) return nullptr;
  const Reading &r = buf_[head_];
  return r.ts ? &r : nullptr;
}

bool ReadingRing::popOldestIfId(uint32_t id) {
  if (n_ == 0 || buf_[head_].id != id) return false;
  head_ = (head_ + 1) % AQG_RING_CAP;
  n_--;
  return true;
}

uint32_t ReadingRing::takeDropped() { uint32_t d = dropped_; dropped_ = 0; return d; }

// ================================================================== valve
const char *verdictName(Verdict v) {
  switch (v) {
    case CMD_APPLIED: return "applied";
    case CMD_NOOP: return "noop";
    case CMD_DUPLICATE: return "duplicate";
    case CMD_REJECT_MALFORMED: return "malformed";
    case CMD_REJECT_STALE: return "stale";
    case CMD_REJECT_TIME: return "time_invalid";
    case CMD_REJECT_SOURCE: return "source_not_allowed";
    case CMD_REJECT_RATE: return "rate_limited";
  }
  return "?";
}

void ValveController::begin(KvStore *s, const Config &c) {
  store_ = s;
  cfg_ = c;
  if (cfg_.close_settle_s <= cfg_.window_s) cfg_.close_settle_s = cfg_.window_s + 1u;  // see header
}

ValveState ValveController::bootAssert(uint32_t mono_s) {
  uint64_t v = 0;
  now_s_ = mono_s;
  if (store_ && store_->getU64("valve", &v) && v == 1) {
    state_ = VALVE_CLOSED;
    confirm_ = CC_PENDING;
    close_s_ = energized_s_ = mono_s;
    saw_flow_ = false;
  } else {
    state_ = VALVE_OPEN;
    confirm_ = CC_NONE;
  }
  return state_;
}

static bool validToken(const char *s, size_t maxlen) {
  if (!s) return false;
  size_t n = strnlen(s, maxlen + 1);
  if (n == 0 || n > maxlen) return false;
  for (size_t i = 0; i < n; i++) {
    char ch = s[i];
    bool ok = (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || ch == '_' || ch == '-';
    if (!ok) return false;
  }
  return true;
}

bool ValveController::seen(const char *id) const {
  for (uint8_t i = 0; i < ring_n_; i++)
    if (strcmp(ring_[i], id) == 0) return true;
  return false;
}

void ValveController::remember(const char *id) {
  strncpy(ring_[ring_i_], id, 16);
  ring_[ring_i_][16] = 0;
  ring_i_ = (uint8_t)((ring_i_ + 1) % 16);
  if (ring_n_ < 16) ring_n_++;
  strncpy(last_id_, id, 16);
  last_id_[16] = 0;
}

bool ValveController::rateOk(bool is_open, bool emergency, uint32_t mono_s) {
  (void)is_open;
  if (!emergency && have_act_ && (uint32_t)(mono_s - last_act_s_) < cfg_.cmd_min_interval_s) return false;
  uint32_t cap = cfg_.cmd_max_toggles_per_hour > 32 ? 32 : cfg_.cmd_max_toggles_per_hour;
  uint32_t recent = 0;
  for (uint32_t t : toggles_)
    if (t != 0 && (uint32_t)(mono_s + 1 - t) < 3600u) recent++;
  return recent < cap;
}

void ValveController::applyState(ValveState s, uint32_t mono_s, float flow_hint) {
  state_ = s;
  if (store_ && !store_->putU64("valve", s == VALVE_CLOSED ? 1 : 0)) store_failed_ = true;
  else store_failed_ = false;
  if (s == VALVE_CLOSED) {
    energized_s_ = close_s_ = mono_s;
    confirm_ = CC_PENDING;
    saw_flow_ = flow_hint > cfg_.closed_flow_eps_lpm;
    closed_flow_s_ = calm_s_ = 0;
  } else {
    confirm_ = CC_NONE;
  }
  toggles_[tog_i_] = mono_s + 1;
  tog_i_ = (uint8_t)((tog_i_ + 1) % 32);
  last_act_s_ = mono_s;
  have_act_ = true;
}

Verdict ValveController::handleCommand(const RawCommand &c, uint32_t mono_s, uint32_t epoch, bool time_ok) {
  now_s_ = mono_s;
  if (c.v != 1 || !validToken(c.id, 16) || !c.cmd || !c.src) return CMD_REJECT_MALFORMED;
  bool is_open = strcmp(c.cmd, "open") == 0;
  if (!is_open && strcmp(c.cmd, "close") != 0) return CMD_REJECT_MALFORMED;
  if (!validToken(c.src, 24)) return CMD_REJECT_MALFORMED;
  if (seen(c.id)) return CMD_DUPLICATE;  // anti-replay: re-ack, never re-apply

  bool manual = strncmp(c.src, "manual_", 7) == 0;
  bool is_auto = strcmp(c.src, "auto") == 0;
  if (!manual && !is_auto) return CMD_REJECT_SOURCE;
  if (is_open) {
    if (!manual) return CMD_REJECT_SOURCE;  // NEVER reopen on an automatic command
    if (!time_ok) return CMD_REJECT_TIME;
    if (!c.has_ts) return CMD_REJECT_STALE;
  } else if (is_auto && !cfg_.accept_auto_close) {
    return CMD_REJECT_SOURCE;  // commissioning gate (docs/hardware/hil-test-plan.md)
  }
  if (time_ok && c.has_ts) {
    int64_t age = (int64_t)epoch - c.ts;
    if (age > (int64_t)cfg_.cmd_max_age_s || -age > (int64_t)cfg_.cmd_max_future_s) return CMD_REJECT_STALE;
  }
  ValveState target = is_open ? VALVE_OPEN : VALVE_CLOSED;
  if (target == state_) { remember(c.id); return CMD_NOOP; }
  bool emergency = !is_open && strcmp(c.src, "manual_override") == 0;
  if (!rateOk(is_open, emergency, mono_s)) return CMD_REJECT_RATE;
  applyState(target, mono_s, last_lpm_);
  remember(c.id);
  return CMD_APPLIED;
}

bool ValveController::requestLocalClose(uint32_t mono_s, float window_lpm) {
  now_s_ = mono_s;
  if (state_ == VALVE_CLOSED || !rateOk(false, false, mono_s)) return false;
  applyState(VALVE_CLOSED, mono_s, window_lpm);
  strncpy(last_id_, "local", sizeof last_id_);
  return true;
}

void ValveController::tick(uint32_t mono_s, float lpm) {
  now_s_ = mono_s;
  last_lpm_ = lpm;
  if (state_ != VALVE_CLOSED) return;
  bool flowing = lpm > cfg_.closed_flow_eps_lpm;
  uint32_t elapsed = mono_s - close_s_;
  switch (confirm_) {
    case CC_PENDING:
      if (flowing) saw_flow_ = true;
      if (elapsed >= cfg_.close_settle_s) {
        if (!flowing) confirm_ = saw_flow_ ? CC_CONFIRMED : CC_UNVERIFIABLE;
        else if (elapsed >= cfg_.close_confirm_timeout_s) confirm_ = CC_FAILED;
      }
      closed_flow_s_ = 0;
      break;
    case CC_CONFIRMED:
    case CC_UNVERIFIABLE:
      closed_flow_s_ = flowing ? closed_flow_s_ + 1 : 0;
      if (closed_flow_s_ >= cfg_.closed_leak_s) { confirm_ = CC_FAILED; calm_s_ = 0; }
      break;
    case CC_FAILED:
      calm_s_ = flowing ? 0 : calm_s_ + 1;
      if (calm_s_ >= cfg_.close_settle_s) { confirm_ = CC_CONFIRMED; closed_flow_s_ = 0; }
      break;
    default: break;
  }
}

ErrMask ValveController::mask() const {
  ErrMask m = 0;
  if (state_ == VALVE_CLOSED && confirm_ == CC_FAILED) m |= errBit(ERR_CLOSE_UNCONF);
  if (state_ == VALVE_CLOSED && cfg_.coil_duty_warn_s && (uint32_t)(now_s_ - energized_s_) >= cfg_.coil_duty_warn_s)
    m |= errBit(ERR_COIL_DUTY);
  return m;
}

// ================================================================== backoff
uint32_t Backoff::next(uint32_t rnd) {
  uint32_t d = cur_;
  uint32_t span = d / 5;  // +/-20 %
  if (span) d = d - span + (rnd % (2 * span + 1));
  cur_ = cur_ * 2 > max_ ? max_ : cur_ * 2;
  return d;
}

// ================================================================== node
void Node::begin(const Config &c, KvStore *store, uint32_t entropy32, ErrMask boot_err) {
  cfg_ = c;
  store_ = store;
  boot_err_ = boot_err;
  vol_.begin(store, cfg_);
  uint64_t persisted = vol_.load();
  flow_.begin(cfg_, persisted);
  leak_.begin(cfg_);
  faults_.begin(cfg_);
  bat_.begin(cfg_);
  ids_.begin(store, cfg_.id_reserve, entropy32);
  valve_.begin(store, cfg_);
}

ErrMask Node::activeErr() const {
  ErrMask m = leak_.mask() | faults_.mask() | bat_.mask() | valve_.mask();
  if (vol_.lastWriteFailed() || ids_.storeFailed() || valve_.storeFailed()) m |= errBit(ERR_STORE_FAIL);
  return m;
}

void Node::tick(const TickInput &in) {
  time_ok_ = timeValid(in.epoch, cfg_);
  epoch_now_ = in.epoch;
  rssi_ = in.rssi;
  float inst = flow_.addSample(in.pulses, in.dt_ms);
  float w = flow_.windowLpm();
  bat_.update(in.adc_mv);
  faults_.step(in.mono_s, in.line_level, in.pulses, inst, valve_.state() == VALVE_CLOSED);
  bool night = time_ok_ && inNightWindow(localHour(in.epoch, cfg_.tz_offset_s), cfg_.night_start_h, cfg_.night_end_h);
  leak_.step(w, night);
  valve_.tick(in.mono_s, w);
  vol_.update(flow_.totalPulses(), in.mono_s);

  // Optional local auto shut-off: OFF by default; never on a suspect sensor; never reopens.
  if (cfg_.local_autoshutoff && leak_.burstActive() && leak_.burstActiveSeconds() >= cfg_.local_shutoff_burst_s &&
      !(faults_.mask() & (errBit(ERR_FLOW_IMPOSSIBLE) | errBit(ERR_STUCK_LINE))) && !bat_.adcFault())
    valve_.requestLocalClose(in.mono_s, w);

  period_err_ |= activeErr();

  if ((uint32_t)(in.mono_s - last_reading_s_) >= cfg_.flow_period_s) {
    last_reading_s_ = in.mono_s;
    Reading r;
    r.id = ids_.next();
    r.mono_s = in.mono_s;
    r.ts = time_ok_ ? in.epoch : 0;
    float f = flow_.takePeriodLpm();
    r.flow = f > 500.0f ? 500.0f : f;
    double sv = flow_.sessionLitres();
    r.vol = (float)(sv > 100000.0 ? 100000.0 : sv);
    r.tot = flow_.totalLitres();
    if (bat_.valid()) { r.bat = (int8_t)bat_.percent(); r.vbat = bat_.vbat(); r.has_vbat = true; }
    r.rssi = (int16_t)in.rssi;
    r.valve_closed = valve_.state() == VALVE_CLOSED;
    r.err = period_err_ | activeErr() | boot_err_;
    if (ring_.takeDropped() > 0) r.err |= errBit(ERR_BACKFILL_DROP);
    boot_err_ = 0;
    period_err_ = 0;
    ring_.push(r);
  }
}

Verdict Node::command(const RawCommand &c, uint32_t mono_s, uint32_t epoch) {
  return valve_.handleCommand(c, mono_s, epoch, timeValid(epoch, cfg_));
}

bool Node::heartbeatDue(uint32_t mono_s) {
  if (!time_ok_ || (uint32_t)(mono_s - last_beat_s_) < cfg_.heartbeat_period_s) return false;
  last_beat_s_ = mono_s;
  return true;
}

const Reading *Node::nextToSend(uint32_t mono_s, uint32_t epoch) {
  if (timeValid(epoch, cfg_)) ring_.resolveTimestamps(epoch, mono_s);
  ring_.dropStale(mono_s, cfg_.max_backfill_s);
  return ring_.oldest();
}

// ================================================================== JSON
static double clampf(double v, double lo, double hi) {
  if (v != v) return lo;
  return v < lo ? lo : (v > hi ? hi : v);
}

size_t formatFlowJson(const Reading &r, char *buf, size_t n) {
  size_t o = 0;
  int w = snprintf(buf, n, "{\"v\":1,\"id\":\"%08x\",\"ts\":%u,\"flow\":%.3f,\"vol\":%.3f,\"psi\":null,\"tmp\":null,",
                   (unsigned)r.id, (unsigned)r.ts, clampf(r.flow, 0, 500), clampf(r.vol, 0, 100000));
  if (w < 0 || (size_t)w >= n) return 0;
  o = (size_t)w;
  if (r.bat >= 0) w = snprintf(buf + o, n - o, "\"bat\":%d,", (int)r.bat);
  else w = snprintf(buf + o, n - o, "\"bat\":null,");
  if (w < 0 || (size_t)w >= n - o) return 0;
  o += (size_t)w;
  if (r.rssi <= 0 && r.rssi >= -150) w = snprintf(buf + o, n - o, "\"rssi\":%d", (int)r.rssi);
  else w = snprintf(buf + o, n - o, "\"rssi\":null");
  if (w < 0 || (size_t)w >= n - o) return 0;
  o += (size_t)w;
  w = snprintf(buf + o, n - o, ",\"tot\":%.3f", clampf(r.tot, 0, 1e9));
  if (w < 0 || (size_t)w >= n - o) return 0;
  o += (size_t)w;
  if (r.has_vbat) {
    w = snprintf(buf + o, n - o, ",\"vbat\":%.3f", clampf(r.vbat, 2.5, 4.5));
    if (w < 0 || (size_t)w >= n - o) return 0;
    o += (size_t)w;
  }
  w = snprintf(buf + o, n - o, ",\"valve\":\"%s\",\"fw\":\"%s\"", r.valve_closed ? "closed" : "open", AQG_FW_VERSION);
  if (w < 0 || (size_t)w >= n - o) return 0;
  o += (size_t)w;
  const char *names[MAX_ERR_CODES];
  size_t ne = errList(r.err, names, MAX_ERR_CODES);
  if (ne) {
    w = snprintf(buf + o, n - o, ",\"err\":[");
    if (w < 0 || (size_t)w >= n - o) return 0;
    o += (size_t)w;
    for (size_t i = 0; i < ne; i++) {
      w = snprintf(buf + o, n - o, "%s\"%s\"", i ? "," : "", names[i]);
      if (w < 0 || (size_t)w >= n - o) return 0;
      o += (size_t)w;
    }
    w = snprintf(buf + o, n - o, "]");
    if (w < 0 || (size_t)w >= n - o) return 0;
    o += (size_t)w;
  }
  if (o + 2 > n) return 0;
  buf[o++] = '}';
  buf[o] = 0;
  return o;
}

size_t formatHeartbeatJson(uint32_t ts, uint32_t up, int bat, char *buf, size_t n) {
  int w;
  if (bat >= 0) w = snprintf(buf, n, "{\"v\":1,\"ts\":%u,\"uptime\":%u,\"bat\":%d,\"fw\":\"%s\"}", (unsigned)ts, (unsigned)up, bat, AQG_FW_VERSION);
  else w = snprintf(buf, n, "{\"v\":1,\"ts\":%u,\"uptime\":%u,\"bat\":null,\"fw\":\"%s\"}", (unsigned)ts, (unsigned)up, AQG_FW_VERSION);
  return (w < 0 || (size_t)w >= n) ? 0 : (size_t)w;
}

size_t formatValveAckJson(const char *cmd_id, bool closed, uint32_t ts, char *buf, size_t n) {
  int w = snprintf(buf, n, "{\"v\":1,\"cmd_id\":\"%s\",\"state\":\"%s\",\"ts\":%u}", cmd_id, closed ? "closed" : "open", (unsigned)ts);
  return (w < 0 || (size_t)w >= n) ? 0 : (size_t)w;
}

}  // namespace aqg

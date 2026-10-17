/*
 * AquaGuard sensor node (ESP32, Arduino framework) - hardware glue around aquaguard_core.
 *
 * STATUS: reference firmware. Host-tested core; the hardware wiring in this file is UNTESTED on
 * real boards, valves or plumbing. Read docs/hardware/ (esp. valve-safety.md) before connecting
 * a valve to a water supply.
 *
 *  - YF-S201 pulses on FLOW_PIN (interrupt, debounced), 1 Hz sampling -> aqg::Node
 *  - normally-OPEN solenoid valve: relay ENERGISED = valve CLOSED. Desired state is persisted and
 *    re-asserted at boot BEFORE Wi-Fi/MQTT start. Comms loss never changes the valve.
 *  - MQTT over TLS (espMqttClient, QoS 1), per-device credentials from config.h
 *  - readings buffered in RAM while offline and replayed oldest-first with original timestamps
 *
 * Libraries (verified against upstream sources, see docs/hardware/README.md):
 *   espMqttClient (bertmelis) 1.7.x, ArduinoJson 7.x. Core: arduino-esp32 3.x (ESP32 classic).
 */
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_random.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <esp_task_wdt.h>
#include <espMqttClient.h>
#include <time.h>

#include "config.h"
#include "aquaguard_core.h"
#include "optional_sensors.h"

// ------------------------------------------------------------------ build-time defaults
#ifndef RELAY_ACTIVE_LOW
#define RELAY_ACTIVE_LOW 0
#endif
#ifndef NTP_SERVER_1
#define NTP_SERVER_1 "pool.ntp.org"
#endif
#ifndef NTP_SERVER_2
#define NTP_SERVER_2 "time.cloudflare.com"
#endif
#ifndef WDT_TIMEOUT_S
#define WDT_TIMEOUT_S 60  // TLS connects can block for several seconds; keep generous
#endif
#ifndef BATTERY_ADC_PIN
#define BATTERY_ADC_PIN -1
#endif
#ifdef ALLOW_INSECURE_DEV
#warning "ALLOW_INSECURE_DEV is set: TLS certificate verification is DISABLED. Never ship this."
#endif

// ------------------------------------------------------------------ configuration mapping
static aqg::Config buildConfig() {
  aqg::Config c;
#ifdef PULSES_PER_LITRE
  c.pulses_per_litre = PULSES_PER_LITRE;
#endif
#ifdef MIN_PULSE_GAP_US
  c.min_pulse_gap_us = MIN_PULSE_GAP_US;
#endif
#ifdef FLOW_PERIOD_S
  c.flow_period_s = FLOW_PERIOD_S;
#endif
#ifdef HEARTBEAT_PERIOD_S
  c.heartbeat_period_s = HEARTBEAT_PERIOD_S;
#endif
#ifdef MAX_BACKFILL_S
  c.max_backfill_s = MAX_BACKFILL_S;
#endif
#ifdef CONTINUOUS_FLOW_MIN
  c.continuous_flow_min = CONTINUOUS_FLOW_MIN;
#endif
#ifdef MIN_LEAK_FLOW_LPM
  c.min_leak_flow_lpm = MIN_LEAK_FLOW_LPM;
#endif
#ifdef BURST_FLOW_LPM
  c.burst_flow_lpm = BURST_FLOW_LPM;
#endif
#ifdef BURST_SUSTAIN_S
  c.burst_sustain_s = BURST_SUSTAIN_S;
#endif
#ifdef NIGHT_START_H
  c.night_start_h = NIGHT_START_H;
#endif
#ifdef NIGHT_END_H
  c.night_end_h = NIGHT_END_H;
#endif
#ifdef ZERO_GAP_RESET_S
  c.zero_gap_reset_s = ZERO_GAP_RESET_S;
#endif
#ifdef TZ_OFFSET_S
  c.tz_offset_s = TZ_OFFSET_S;
#endif
#ifdef COIL_DUTY_WARN_S
  c.coil_duty_warn_s = COIL_DUTY_WARN_S;
#endif
#ifdef BATTERY_DIVIDER_RATIO
  c.divider_ratio = BATTERY_DIVIDER_RATIO;
#endif
#ifdef LOCAL_AUTOSHUTOFF_ENABLED
  c.local_autoshutoff = (LOCAL_AUTOSHUTOFF_ENABLED) != 0;
#endif
#ifdef ACCEPT_AUTO_CLOSE
  c.accept_auto_close = (ACCEPT_AUTO_CLOSE) != 0;
#endif
  c.battery_present = (BATTERY_ADC_PIN >= 0);
  return c;
}

// ------------------------------------------------------------------ NVS-backed store
class NvsStore : public aqg::KvStore {
 public:
  bool begin() { return prefs_.begin("aqg", false); }
  bool getU64(const char *key, uint64_t *out) override {
    if (!prefs_.isKey(key)) return false;
    *out = prefs_.getULong64(key, 0);
    return true;
  }
  bool putU64(const char *key, uint64_t v) override { return prefs_.putULong64(key, v) == sizeof(uint64_t); }
 private:
  Preferences prefs_;
};

// ------------------------------------------------------------------ globals
static NvsStore g_store;
static aqg::Node g_node;
static aqg::Config g_cfg;

static volatile uint32_t g_pulses = 0;
static aqg::PulseDebouncer g_deb;
static volatile uint32_t g_min_gap_us = 1500;
void IRAM_ATTR onPulse() {
  if (g_deb.onEdge(micros(), g_min_gap_us)) g_pulses = g_pulses + 1;
}

static espMqttClientSecure g_mqtt(espMqttClientTypes::UseInternalTask::NO);  // callbacks run in loop()
static char g_clientId[48], g_tFlow[96], g_tBeat[96], g_tCmd[96], g_tState[96];

static aqg::Backoff g_wifiBackoff(1000, 60000), g_mqttBackoff(2000, 60000);
static uint32_t g_nextWifiMs = 0, g_nextMqttMs = 0;
static bool g_ntpStarted = false;

static uint16_t g_inflightPid = 0;
static uint32_t g_inflightId = 0, g_inflightAtMs = 0;
static bool g_haveInflight = false;

struct PendingAck { bool pending = false; char id[17] = {0}; } g_ack;

// ------------------------------------------------------------------ relay (normally-open valve)
static void relayWrite(bool energized) {
  digitalWrite(VALVE_RELAY_PIN, aqg::relayPinHigh(energized, RELAY_ACTIVE_LOW) ? HIGH : LOW);
}
static void relayInitDeenergized() {
  // Write the latch first, then enable the driver, so the pin never glitches to the wrong level.
  // (UNTESTED on hardware; also fit the pull resistor recommended in wiring-and-electrical.md.)
  relayWrite(false);
  pinMode(VALVE_RELAY_PIN, OUTPUT);
  relayWrite(false);
}
static void syncRelay() { relayWrite(g_node.valve().energized()); }  // idempotent; called every tick

// ------------------------------------------------------------------ time
// 64-bit microsecond timer => seconds since boot never wraps (millis() wraps after 49.7 days).
static uint32_t monoS() { return (uint32_t)(esp_timer_get_time() / 1000000ULL); }
static uint32_t epochNow() {
  time_t t = time(nullptr);
  return t > 0 ? (uint32_t)t : 0;
}
static bool timeOk() { return aqg::timeValid(epochNow(), g_cfg); }

// ------------------------------------------------------------------ MQTT helpers
static void onMqttMessage(const espMqttClientTypes::MessageProperties &, const char *topic, const uint8_t *payload,
                          size_t len, size_t index, size_t total) {
  if (strcmp(topic, g_tCmd) != 0) return;
  if (index != 0 || len != total || len > 400) { Serial.println(F("cmd: oversized/fragmented, ignored")); return; }
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) { Serial.println(F("cmd: bad JSON")); return; }
  aqg::RawCommand rc;
  rc.v = doc["v"] | 0;
  rc.id = doc["id"].is<const char *>() ? doc["id"].as<const char *>() : nullptr;
  rc.cmd = doc["cmd"].is<const char *>() ? doc["cmd"].as<const char *>() : nullptr;
  rc.src = doc["src"].is<const char *>() ? doc["src"].as<const char *>() : nullptr;
  if (doc["ts"].is<int64_t>()) { rc.has_ts = true; rc.ts = doc["ts"].as<int64_t>(); }
  aqg::Verdict v = g_node.command(rc, monoS(), epochNow());
  syncRelay();  // act immediately, do not wait for the next tick
  Serial.printf("cmd %s %s src=%s -> %s\n", rc.id ? rc.id : "?", rc.cmd ? rc.cmd : "?", rc.src ? rc.src : "?",
                aqg::verdictName(v));
  if (aqg::verdictShouldAck(v)) {  // the ack always carries the ACTUAL state, so rejections cannot lie
    strncpy(g_ack.id, rc.id, 16);
    g_ack.id[16] = 0;
    g_ack.pending = true;
  }
}

static void sendAckIfPending() {
  if (!g_ack.pending || !g_mqtt.connected() || !timeOk()) return;
  char buf[160];
  size_t n = aqg::formatValveAckJson(g_ack.id, g_node.valve().state() == aqg::VALVE_CLOSED, epochNow(), buf, sizeof buf);
  if (n && g_mqtt.publish(g_tState, 1, true, (const uint8_t *)buf, n)) g_ack.pending = false;  // retained, QoS 1
}

static void startMqtt() {
#ifndef ALLOW_INSECURE_DEV
  if (strlen(MQTT_USER) == 0 || strlen(MQTT_PASSWORD) == 0 || !strcmp(MQTT_PASSWORD, "CHANGE_ME") ||
      strstr(MQTT_CA_CERT_PEM, "CHANGE_ME") != nullptr) {
    static bool warned = false;
    if (!warned) { Serial.println(F("MQTT: credentials/CA missing or placeholder -> NOT connecting (fail closed)")); warned = true; }
    return;
  }
#endif
  if (!timeOk()) return;  // TLS certificate validation needs a valid clock
#ifdef ALLOW_INSECURE_DEV
  Serial.println(F("!!!!!! ALLOW_INSECURE_DEV: TLS verification DISABLED. Dev only. !!!!!!"));
  g_mqtt.setInsecure();
#else
  g_mqtt.setCACert(MQTT_CA_CERT_PEM);
#endif
  g_mqtt.setServer(MQTT_HOST, MQTT_PORT).setCredentials(MQTT_USER, MQTT_PASSWORD).setClientId(g_clientId);
  g_mqtt.setKeepAlive(30).setCleanSession(true);
  if (!g_mqtt.connect()) Serial.println(F("MQTT: connect() could not queue CONNECT"));
}

static void setupMqttCallbacks() {
  g_mqtt.onConnect([](bool) {
    Serial.println(F("MQTT connected"));
    g_mqttBackoff.reset();
    g_mqtt.subscribe(g_tCmd, 1);  // QoS 1 so a command sent just before reconnect is not lost (clean session: best effort)
    g_haveInflight = false;
#ifdef PUBLISH_BOOT_VALVE_STATE
    strncpy(g_ack.id, "boot", sizeof g_ack.id);
    g_ack.pending = true;
#endif
  });
  g_mqtt.onDisconnect([](espMqttClientTypes::DisconnectReason r) {
    Serial.printf("MQTT disconnected: %s\n", espMqttClientTypes::disconnectReasonToString(r));
    g_haveInflight = false;
  });
  g_mqtt.onPublish([](uint16_t pid) {
    if (g_haveInflight && pid == g_inflightPid) { g_node.sent(g_inflightId); g_haveInflight = false; }
  });
  g_mqtt.onMessage(onMqttMessage);
}

static void publishPending() {
  if (!g_mqtt.connected()) return;
  uint32_t nowMs = millis();
  if (g_haveInflight && nowMs - g_inflightAtMs > 15000) g_haveInflight = false;  // will be resent; id dedupes
  sendAckIfPending();
  if (!g_haveInflight) {
    const aqg::Reading *r = g_node.nextToSend(monoS(), epochNow());
    if (r) {
      char buf[512];
      size_t n = aqg::formatFlowJson(*r, buf, sizeof buf);
      if (n) {
        uint16_t pid = g_mqtt.publish(g_tFlow, 1, false, (const uint8_t *)buf, n);
        if (pid) { g_haveInflight = true; g_inflightPid = pid; g_inflightId = r->id; g_inflightAtMs = nowMs; }
      }
    }
  }
  if (g_node.heartbeatDue(monoS())) {
    char buf[160];
    size_t n = aqg::formatHeartbeatJson(epochNow(), monoS(), g_node.battery().percent(), buf, sizeof buf);
    if (n) g_mqtt.publish(g_tBeat, 1, false, (const uint8_t *)buf, n);  // best effort, not buffered
  }
}

// ------------------------------------------------------------------ connectivity (non-blocking)
static void serviceNetwork() {
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if ((int32_t)(now - g_nextWifiMs) >= 0) {
      Serial.println(F("Wi-Fi: (re)connecting"));
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      g_nextWifiMs = now + g_wifiBackoff.next(esp_random());
    }
    return;
  }
  g_wifiBackoff.reset();
  if (!g_ntpStarted) { configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2); g_ntpStarted = true; }
  if (g_mqtt.disconnected() && (int32_t)(now - g_nextMqttMs) >= 0) {
    g_nextMqttMs = now + g_mqttBackoff.next(esp_random());
    startMqtt();
  }
}

// ------------------------------------------------------------------ ADC
static int readBatteryMv() {
  if (BATTERY_ADC_PIN < 0) return -1;
  uint32_t acc = 0;
  for (int i = 0; i < 16; i++) acc += analogReadMilliVolts(BATTERY_ADC_PIN);  // eFuse-calibrated where present
  return (int)(acc / 16);
}

// ------------------------------------------------------------------ watchdog
static void startWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t wc = {};
  wc.timeout_ms = WDT_TIMEOUT_S * 1000;
  wc.idle_core_mask = 0;
  wc.trigger_panic = true;
  esp_task_wdt_reconfigure(&wc);  // the Arduino core already initialises the TWDT
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(nullptr);
}

// ------------------------------------------------------------------ setup / loop
void setup() {
  // 1. Relay driver first, de-energised (valve OPEN), so the pin never floats longer than needed.
  relayInitDeenergized();
  Serial.begin(115200);
  Serial.println(F("\nAquaGuard sensor " AQG_FW_VERSION));

  // 2. Persistent state (no radio yet): valve intent, lifetime pulses, message-id counter.
  g_cfg = buildConfig();
  g_min_gap_us = g_cfg.min_pulse_gap_us;
  aqg::ErrMask bootErr = 0;
  switch (esp_reset_reason()) {
    case ESP_RST_BROWNOUT: bootErr |= aqg::errBit(aqg::ERR_RST_BROWNOUT); break;
    case ESP_RST_TASK_WDT:
    case ESP_RST_INT_WDT:
    case ESP_RST_WDT:
    case ESP_RST_PANIC: bootErr |= aqg::errBit(aqg::ERR_RST_WDT); break;  // watchdog or crash
    default: break;
  }
  bool nvsOk = g_store.begin();
  if (!nvsOk) { Serial.println(F("NVS init failed: state will NOT persist")); bootErr |= aqg::errBit(aqg::ERR_STORE_FAIL); }
  g_node.begin(g_cfg, &g_store, esp_random(), bootErr);

  // 3. Re-assert the persisted valve state BEFORE Wi-Fi/MQTT exist.
  if (g_node.bootValve(monoS())) { relayWrite(true); Serial.println(F("valve: re-asserted CLOSED from NVS")); }
  else Serial.println(F("valve: OPEN (nothing persisted or last state was open)"));

  // 4. Flow input.
  pinMode(FLOW_PIN, INPUT);  // external pull-up/level shifter required: see wiring-and-electrical.md
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), onPulse, FALLING);
#if BATTERY_ADC_PIN >= 0
  analogSetPinAttenuation(BATTERY_ADC_PIN, ADC_11db);
#endif

  // 5. Topics, MQTT callbacks, watchdog, then radios.
  snprintf(g_clientId, sizeof g_clientId, "aquaguard-%s-%s", SOCIETY_SLUG, FLAT_UNIT);
  snprintf(g_tFlow, sizeof g_tFlow, "aquaguard/%s/%s/flow", SOCIETY_SLUG, FLAT_UNIT);
  snprintf(g_tBeat, sizeof g_tBeat, "aquaguard/%s/%s/heartbeat", SOCIETY_SLUG, FLAT_UNIT);
  snprintf(g_tCmd, sizeof g_tCmd, "aquaguard/%s/%s/valve/cmd", SOCIETY_SLUG, FLAT_UNIT);
  snprintf(g_tState, sizeof g_tState, "aquaguard/%s/%s/valve/state", SOCIETY_SLUG, FLAT_UNIT);
  setupMqttCallbacks();
  startWatchdog();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);  // serviceNetwork() owns reconnects (exponential backoff)
  WiFi.persistent(false);
}

void loop() {
  static uint32_t lastTickMs = 0, lastBatMs = 0;
  static int lastAdc = -1;
  esp_task_wdt_reset();
  serviceNetwork();
  g_mqtt.loop();

  uint32_t now = millis();
  if (lastTickMs == 0) { lastTickMs = now; lastBatMs = now - 30000; }
  if (now - lastTickMs >= 1000) {
    uint32_t dt = now - lastTickMs;
    lastTickMs = now;
    noInterrupts();
    uint32_t p = g_pulses;
    g_pulses = 0;
    interrupts();
    if (now - lastBatMs >= 30000) { lastBatMs = now; lastAdc = readBatteryMv(); } else lastAdc = -1;
    aqg::TickInput in;
    in.mono_s = monoS();
    in.pulses = p;
    in.dt_ms = dt;
    in.line_level = digitalRead(FLOW_PIN);
    in.adc_mv = lastAdc;
    in.epoch = epochNow();
    in.rssi = (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : 1000;
    g_node.tick(in);
    syncRelay();
  }
  publishPending();
  delay(5);
}

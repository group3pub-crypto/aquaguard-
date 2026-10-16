/*
 * AquaGuard reference sensor (ESP32) - illustrative sketch, NOT production firmware.
 *
 * - YF-S201 hall-effect flow sensor, pulse counting via interrupt
 *     F[Hz] = 7.5 * Q[L/min]  ->  Q = pulses_per_second / 7.5  (datasheet nominal; calibrate!)
 * - Publishes the same JSON as the simulator once a minute (QoS 1 where supported, see note)
 * - Subscribes to valve/cmd, drives a relay, acks on valve/state (retained)
 * - Credentials come from config.h (copy config.h.example); nothing is hard-coded.
 *
 * Libraries: WiFi (core), PubSubClient, ArduinoJson (v7).
 * Caveats: PubSubClient publishes QoS 0 only (use AsyncMqttClient/esp-mqtt for QoS 1 publish);
 * no TLS, OTA, watchdog tuning, persisted valve state or NTP-failure handling here.
 */
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include "config.h"

static const float PULSES_PER_LPM_SEC = 7.5f;   // YF-S201: Hz = 7.5 * L/min
static const uint32_t FLOW_PERIOD_MS = 60000;
static const uint32_t HEARTBEAT_PERIOD_MS = 300000;

WiFiClient net;
PubSubClient mqtt(net);

volatile uint32_t pulses = 0;
void IRAM_ATTR onPulse() { pulses++; }

bool valveOpen = true;
double totalLiters = 0.0;
uint32_t seqNo = 0, lastFlow = 0, lastBeat = 0;
String tFlow, tBeat, tCmd, tState;

String msgId() {  // 8 hex chars, unique per boot sequence (mixed with the chip id)
  uint32_t v = (uint32_t)(++seqNo * 2654435761UL) + (uint32_t)ESP.getEfuseMac();
  char b[9];
  snprintf(b, sizeof b, "%08x", v);
  return String(b);
}

float batteryPercent() {
  if (BATTERY_ADC_PIN < 0) return 100.0f;
  float v = analogReadMilliVolts(BATTERY_ADC_PIN) * 2.0f / 1000.0f;  // 1:2 divider
  return constrain((v - 3.3f) / (4.2f - 3.3f) * 100.0f, 0.0f, 100.0f);
}

void setValve(bool open) {
  valveOpen = open;
  digitalWrite(VALVE_RELAY_PIN, open ? !VALVE_CLOSE_LEVEL : VALVE_CLOSE_LEVEL);
}

void publishJson(const String &topic, JsonDocument &doc, bool retain = false) {
  String out;
  serializeJson(doc, out);
  mqtt.publish(topic.c_str(), out.c_str(), retain);
}

void onMessage(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char *cmd = doc["cmd"], *id = doc["id"];
  if (!cmd || !id) return;
  if (!strcmp(cmd, "close")) setValve(false);
  else if (!strcmp(cmd, "open")) setValve(true);
  else return;
  JsonDocument ack;
  ack["v"] = 1;
  ack["cmd_id"] = id;
  ack["state"] = valveOpen ? "open" : "closed";
  ack["ts"] = (uint32_t)time(nullptr);
  publishJson(tState, ack, true);
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  configTime(0, 0, "pool.ntp.org");
}

void connectMqtt() {
  uint32_t backoff = 1000;
  while (!mqtt.connected()) {
    String cid = "aquaguard-" + String(FLAT_UNIT);
    bool ok = strlen(MQTT_USER) ? mqtt.connect(cid.c_str(), MQTT_USER, MQTT_PASSWORD)
                                : mqtt.connect(cid.c_str());
    if (ok) { mqtt.subscribe(tCmd.c_str(), 1); return; }
    delay(backoff);
    backoff = min<uint32_t>(backoff * 2, 30000);
  }
}

void setup() {
  pinMode(VALVE_RELAY_PIN, OUTPUT);
  setValve(true);
  pinMode(FLOW_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), onPulse, FALLING);
  String base = String("aquaguard/") + SOCIETY_SLUG + "/" + FLAT_UNIT;
  tFlow = base + "/flow";
  tBeat = base + "/heartbeat";
  tCmd = base + "/valve/cmd";
  tState = base + "/valve/state";
  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(512);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();

  uint32_t now = millis();
  if (now - lastFlow >= FLOW_PERIOD_MS) {
    noInterrupts();
    uint32_t p = pulses;
    pulses = 0;
    interrupts();
    float secs = (now - lastFlow) / 1000.0f;
    lastFlow = now;
    float lpm = (p / secs) / PULSES_PER_LPM_SEC;
    totalLiters += lpm * (secs / 60.0f);
    JsonDocument doc;
    doc["v"] = 1;
    doc["id"] = msgId();
    doc["ts"] = (uint32_t)time(nullptr);
    doc["flow"] = lpm;
    doc["vol"] = totalLiters;
    doc["psi"] = nullptr;
    doc["tmp"] = nullptr;
    doc["bat"] = batteryPercent();
    doc["rssi"] = WiFi.RSSI();
    publishJson(tFlow, doc);
  }
  if (now - lastBeat >= HEARTBEAT_PERIOD_MS) {
    lastBeat = now;
    JsonDocument doc;
    doc["v"] = 1;
    doc["ts"] = (uint32_t)time(nullptr);
    doc["uptime"] = now / 1000;
    doc["bat"] = batteryPercent();
    doc["fw"] = "1.2.0";
    publishJson(tBeat, doc);
  }
}

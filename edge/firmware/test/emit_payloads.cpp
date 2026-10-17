// Emits the firmware's real JSON payloads for scripted scenarios, one per line:
//   <scenario>\t<kind>\t<topic-suffix>\t<json>
// usage: emit_payloads [epoch_now]   (default: a fixed epoch). Consumed by edge/tests/test_firmware_payload.py
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "aquaguard_core.h"
using namespace aqg;

static uint32_t g_epoch0 = 1760000000u;
static const char *g_scn = "";

static void emitFlow(const Reading &r) {
  char b[512];
  size_t n = formatFlowJson(r, b, sizeof b);
  if (n) printf("%s\tflow\tflow\t%s\n", g_scn, b);
  else printf("%s\terror\tflow\tBUFFER_TOO_SMALL\n", g_scn);
}
static void emitHeartbeat(uint32_t ts, uint32_t up, int bat) {
  char b[256];
  if (formatHeartbeatJson(ts, up, bat, b, sizeof b)) printf("%s\theartbeat\theartbeat\t%s\n", g_scn, b);
}
static void emitAck(const char *id, bool closed, uint32_t ts) {
  char b[256];
  if (formatValveAckJson(id, closed, ts, b, sizeof b)) printf("%s\tack\tvalve/state\t%s\n", g_scn, b);
}

struct Rig {
  MemStore st; Config c; Node n; uint32_t t = 0, epoch; int adc = 2000; int rssi = -62; bool wifi = true;
  Rig(uint32_t ep, const Config &cfg, ErrMask boot = 0) : c(cfg), epoch(ep) { n.begin(c, &st, 0x1234abcdu, boot); n.bootValve(0); }
  void run(int secs, float lpm, uint32_t pulses_override = 0xFFFFFFFFu, bool level = false) {
    for (int i = 0; i < secs; i++) {
      TickInput in; in.mono_s = ++t; in.dt_ms = 1000;
      in.pulses = pulses_override != 0xFFFFFFFFu ? pulses_override : (uint32_t)(lpm * c.pulses_per_litre / 60.0f + 0.5f);
      in.adc_mv = (t % 30 == 0) ? adc : -1;
      if (epoch) epoch++;
      in.epoch = epoch; in.rssi = wifi ? rssi : 1000; in.line_level = level || (t & 1);
      n.tick(in);
    }
  }
  // transmit everything that is publishable (as an online device would)
  void drain() { while (const Reading *r = n.nextToSend(t, epoch)) { emitFlow(*r); n.sent(r->id); } }
};

int main(int argc, char **argv) {
  if (argc > 1) g_epoch0 = (uint32_t)strtoul(argv[1], nullptr, 10);
  const uint32_t E = g_epoch0 - 7200;  // simulated run starts two hours before "now", ends before it
  Config base;

  g_scn = "normal";
  { Rig r(E, base); for (int m = 0; m < 12; m++) { r.run(30, (m % 3) ? 0.0f : 6.0f); r.run(30, 0); r.drain(); }
    emitHeartbeat(r.epoch, r.t, r.n.battery().percent()); }

  g_scn = "abnormal_flow";
  { Config c = base; c.continuous_flow_min = 3; Rig r(E, c);
    r.run(150, 22.0f); r.drain();           // burst
    r.run(300, 1.0f); r.drain();            // continuous
    RawCommand cmd; cmd.v = 1; cmd.id = "00ff10ab"; cmd.cmd = "close"; cmd.src = "manual_dashboard"; cmd.has_ts = true; cmd.ts = r.epoch;
    Verdict v = r.n.command(cmd, r.t, r.epoch);
    emitAck(cmd.id, r.n.valve().state() == VALVE_CLOSED, r.epoch); (void)v;
    r.run(120, 0.0f); r.drain(); }

  g_scn = "low_battery";
  { Rig r(E, base); const int mv[] = {2100, 1900, 1860, 1840, 1830, 1810, 1800, 1850, 1900};
    for (int mvv : mv) { r.adc = mvv; r.run(60, 0.5f); r.drain(); }
    emitHeartbeat(r.epoch, r.t, r.n.battery().percent()); }

  g_scn = "sensor_error";
  { Config c = base; c.stuck_line_s = 200; Rig r(E, c);
    r.adc = 0; r.run(120, 0); r.drain();                       // ADC disconnected / shorted
    r.adc = 2000; r.run(60, 0, 400);  r.drain();               // 400 pulses/s = 53 L/min: impossible
    r.run(260, 0, 0, true); r.drain();                         // line stuck HIGH, no pulses
  }

  g_scn = "no_battery_no_wifi";
  { Config c = base; c.battery_present = false; Rig r(E, c); r.wifi = false; r.run(60, 2.0f); r.drain(); }

  g_scn = "disconnect_reconnect_replay";
  { Rig r(E, base);
    r.run(120, 1.0f); r.drain();                                // online
    r.run(300, 2.0f);                                           // MQTT down for 5 min: readings stay in the ring
    r.run(60, 0.0f);
    r.drain();                                                  // reconnect: oldest first, ORIGINAL timestamps
  }

  g_scn = "outage_longer_than_backfill";
  { Rig r(E, base);
    r.run(60 * 14, 1.0f);                                       // 14 min down > MAX_BACKFILL_S
    r.drain(); r.run(60, 1.0f); r.drain(); }

  g_scn = "late_sntp";
  { Rig r(0, base, errBit(ERR_RST_BROWNOUT)); r.run(180, 1.0f); r.drain();   // nothing printed: no valid time
    r.epoch = E; r.run(60, 1.0f); r.drain(); }                 // reconstructed timestamps, flagged ts_est

  return 0;
}

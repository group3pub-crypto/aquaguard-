import json
import re

import numpy as np
import pytest

from edge.simulator.faults import FaultType, apply_faults, make_fault
from edge.simulator.patterns import demand_probability, sample_flow
from edge.simulator.profiles import make_profiles
from edge.simulator.simulator import (
    CONTROL_FAULT_TOPIC,
    CONTROL_SPEED_TOPIC,
    SPEED_MAX,
    SimClock,
    Simulator,
    heartbeat_payload,
    make_msg_id,
)

START = 1_736_150_400  # 2025-01-06 08:00 UTC (a Monday)


def mk(**kw):
    return Simulator("demo", kw.pop("flats", 3), start_epoch=START, real_now=0.0, **kw)


def decode(msgs):
    return [(m.topic, json.loads(m.payload)) for m in msgs]


def test_flow_payload_schema():
    sim = mk()
    out = decode(sim.due(0.0))
    topic, p = next((t, p) for t, p in out if t.endswith("/flow"))
    assert re.fullmatch(r"aquaguard/demo/10[123]/flow", topic)
    assert list(p) == ["v", "id", "ts", "flow", "vol", "psi", "tmp", "bat", "rssi"]
    assert p["v"] == 1 and re.fullmatch(r"[0-9a-f]{8}", p["id"])
    assert p["ts"] == START and p["psi"] is None and p["tmp"] is None
    assert isinstance(p["flow"], float) and isinstance(p["rssi"], int)


def test_heartbeat_schema_and_qos():
    sim = mk()
    msgs = sim.due(0.0)  # START is minute-aligned and divisible by 5 min
    hb = [m for m in msgs if m.topic.endswith("/heartbeat")]
    assert len(hb) == 3 and all(m.qos == 1 for m in msgs)
    body = json.loads(hb[0].payload)
    assert set(body) == {"v", "ts", "uptime", "bat", "fw"} and body["fw"] == "1.2.0"
    assert heartbeat_payload(10, 5, 99.94)["bat"] == 99.9


def test_dedupe_ids_unique():
    sim = mk(flats=1, speed=60.0)
    ids = []
    for t in range(1, 3001):  # 3000 real s at 60x = 3000 simulated minutes
        ids += [json.loads(m.payload)["id"] for m in sim.due(float(t)) if m.topic.endswith("flow")]
    assert len(ids) > 2900 and len(set(ids)) == len(ids)
    assert len({make_msg_id(i, 123) for i in range(50000)}) == 50000


def test_deterministic_with_seed():
    a, b, c = mk(seed=5), mk(seed=5), mk(seed=6)
    ra = [m.payload for m in a.due(3600.0 / 60)]
    rb = [m.payload for m in b.due(3600.0 / 60)]
    rc = [m.payload for m in c.due(3600.0 / 60)]
    assert ra == rb and ra != rc


def test_speed_math():
    clk = SimClock(1000.0, speed=60.0, real_now=10.0)
    assert clk.now(10.0) == 1000.0
    assert clk.now(70.0) == 1000.0 + 3600.0  # one real minute = one simulated hour
    clk.set_speed(1.0, 70.0)  # continuity preserved
    assert clk.now(70.0) == 4600.0 and clk.now(130.0) == 4660.0
    clk.set_speed(1e12, 130.0)
    assert clk.speed == SPEED_MAX


def test_one_flow_message_per_simulated_minute():
    sim = mk(flats=2, speed=60.0)
    msgs = sim.due(60.0)  # 60 s real at 60x = 60 simulated minutes (+ the start minute)
    flows = [m for m in msgs if m.topic.endswith("/flow")]
    assert len(flows) == 2 * 61


def test_speed_control_message():
    sim = mk()
    assert sim.handle_message(CONTROL_SPEED_TOPIC, b'{"speed": 120}', real_now=0.0) == []
    assert sim.clock.speed == 120.0
    assert sim.handle_message(CONTROL_SPEED_TOPIC, b"garbage", real_now=0.0) == []
    assert sim.clock.speed == 120.0


def test_valve_closed_zero_flow_and_ack():
    sim = mk(flats=1)
    sim.flats["101"].faults.append(make_fault("burst", START, np.random.default_rng(0)))
    cmd = json.dumps({"v": 1, "id": "c1", "cmd": "close", "src": "api", "ts": START})
    acks = sim.handle_message("aquaguard/demo/101/valve/cmd", cmd.encode(), real_now=0.0)
    assert len(acks) == 1 and acks[0].retain and acks[0].topic == "aquaguard/demo/101/valve/state"
    body = json.loads(acks[0].payload)
    assert body["cmd_id"] == "c1" and body["state"] == "closed" and body["v"] == 1
    flows = [json.loads(m.payload)["flow"] for m in sim.due(600.0) if m.topic.endswith("/flow")]
    assert len(flows) > 5 and all(f == 0.0 for f in flows)
    reopen = json.dumps({"v": 1, "id": "c2", "cmd": "open", "src": "api", "ts": START})
    ack = sim.handle_message("aquaguard/demo/101/valve/cmd", reopen.encode(), real_now=600.0)
    assert json.loads(ack[0].payload)["state"] == "open"
    later = [json.loads(m.payload)["flow"] for m in sim.due(1200.0) if m.topic.endswith("/flow")]
    assert max(later) > 10  # the burst is still active once reopened


def test_invalid_valve_cmd_ignored():
    sim = mk()
    assert sim.handle_message("aquaguard/demo/101/valve/cmd", b'{"id":"x","cmd":"explode"}') == []
    assert sim.handle_message("aquaguard/demo/999/valve/cmd", b'{"id":"x","cmd":"open"}') == []


@pytest.mark.parametrize(
    ("ft", "lo", "hi"),
    [
        ("slow_leak", 0.4, 3.3),
        ("running_toilet", 0.4, 0.6),
        ("dripping_tap", 0.04, 0.23),
        ("burst", 14, 55),
    ],
)
def test_additive_fault_application(ft, lo, hi):
    rng = np.random.default_rng(1)
    f = make_fault(ft, 0, rng)
    vals = [apply_faults(0.0, [f], 60.0 * i, rng) for i in range(200)]
    assert lo <= min(vals) and max(vals) <= hi  # added on a zero (night) base


def test_overuse_multiplies_and_duration_expires():
    rng = np.random.default_rng(2)
    f = make_fault(FaultType.OVERUSE, 0, rng, {"rate": 3.0, "duration_min": 10})
    assert apply_faults(2.0, [f], 60.0, rng) >= 6.0
    assert f.expired(600.0) and apply_faults(2.0, [f], 600.0, rng) == 2.0


def test_fault_control_by_unit_and_flat_id_and_clear():
    sim = Simulator("demo", 3, start_epoch=START, real_now=0.0, flat_ids={"102": "uuid-b"})

    def send(d):
        sim.handle_message(CONTROL_FAULT_TOPIC, json.dumps(d).encode(), real_now=0.0)

    send({"unit": 101, "fault_type": "slow_leak", "action": "inject", "params": {"rate": 2.0}})
    send({"flat_id": "uuid-b", "fault_type": "burst", "action": "inject"})
    send({"flat_id": "103", "fault_type": "dripping_tap", "action": "inject"})
    assert [f.fault_type for f in sim.flats["101"].faults] == [FaultType.SLOW_LEAK]
    assert sim.flats["101"].faults[0].magnitude == 2.0
    assert [f.fault_type for f in sim.flats["102"].faults] == [FaultType.BURST]
    assert [f.fault_type for f in sim.flats["103"].faults] == [FaultType.DRIPPING_TAP]
    send({"unit": "102", "action": "clear"})
    assert sim.flats["102"].faults == []
    send({"unit": "nope", "fault_type": "burst", "action": "inject"})  # ignored, no crash


def test_injected_leak_visible_at_night():
    sim = Simulator("demo", 1, start_epoch=START + 17 * 3600, real_now=0.0)  # 01:00 next day
    payload = json.dumps({"unit": "101", "fault_type": "slow_leak", "params": {"rate": 2.0}})
    sim.handle_message(CONTROL_FAULT_TOPIC, payload.encode(), 0.0)
    flows = [
        json.loads(m.payload)["flow"] for m in sim.due(3 * 3600 / 60) if m.topic.endswith("/flow")
    ]
    assert np.percentile(flows, 10) > 1.5


def test_patterns_night_quiet_and_peaks():
    p = make_profiles(1, seed=1)[0]
    night = np.mean([demand_probability(p, START + 17 * 3600 + 60 * m) for m in range(120)])
    peak = max(demand_probability(p, START - 8 * 3600 + 60 * m) for m in range(1440))
    assert night < 0.02 < 0.1 < peak
    rng = np.random.default_rng(0)
    assert all(sample_flow(p, START, rng) >= 0 for _ in range(100))


def test_volume_is_cumulative():
    sim = mk(flats=1)
    vols = [json.loads(m.payload)["vol"] for m in sim.due(600.0) if m.topic.endswith("/flow")]
    assert vols == sorted(vols)

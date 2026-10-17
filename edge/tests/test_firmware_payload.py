"""Firmware payloads vs. the real backend schema and the simulator (field-for-field parity).

Compiles ``edge/firmware/test/emit_payloads.cpp`` (the firmware's REAL JSON formatter driven by the
real core logic through scripted scenarios) and validates every emitted message. Skipped when no
C++ compiler is available. Optional: a private Mosquitto round trip (skipped without the binary).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from edge.simulator.simulator import ack_payload, flow_payload, heartbeat_payload

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "edge" / "firmware"
CXX = shutil.which("g++") or shutil.which("c++") or shutil.which("clang++")

pytestmark = pytest.mark.skipif(CXX is None, reason="no C++ compiler available")

EXTRA_FIELDS = {"tot", "vbat", "valve", "fw", "err"}
BASE_ORDER = ["v", "id", "ts", "flow", "vol", "psi", "tmp", "bat", "rssi"]


# ------------------------------------------------------------------ schema loading
def _load_backend_models():
    """Import backend/app/schemas/mqtt.py by file path (it only needs pydantic), so the backend
    package's __init__ side effects (DB, settings) are never triggered."""
    path = ROOT / "backend" / "app" / "schemas" / "mqtt.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_aqg_backend_mqtt", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        sys.modules["_aqg_backend_mqtt"] = mod
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - fall back to the JSON-schema copy
        return None
    return mod


BACKEND = _load_backend_models()

# JSON-schema copy derived from docs/mqtt.md + the optional-field contract. Used only if the real
# backend module cannot be imported.
FLOW_JSON_SCHEMA = {
    "type": "object",
    "required": ["v", "id", "ts", "flow", "vol"],
    "properties": {
        "v": {"const": 1},
        "id": {"type": "string", "pattern": "^[0-9a-fA-F]{8}$"},
        "ts": {"type": "number", "exclusiveMinimum": 0},
        "flow": {"type": "number", "minimum": 0, "maximum": 500},
        "vol": {"type": "number", "minimum": 0, "maximum": 100000},
        "psi": {"type": ["number", "null"], "minimum": 0, "maximum": 500},
        "tmp": {"type": ["number", "null"], "minimum": -50, "maximum": 150},
        "bat": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
        "rssi": {"type": ["integer", "null"], "minimum": -150, "maximum": 0},
        "tot": {"type": ["number", "null"], "minimum": 0, "maximum": 1e9},
        "vbat": {"type": ["number", "null"], "minimum": 2.5, "maximum": 4.5},
        "valve": {"enum": ["open", "closed", None]},
        "fw": {"type": ["string", "null"], "maxLength": 32},
        "err": {
            "type": ["array", "null"],
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 24},
        },
    },
}


def validate_flow(d: dict) -> None:
    if BACKEND is not None:
        BACKEND.FlowPayload.model_validate(d)
    else:  # pragma: no cover
        import jsonschema

        jsonschema.validate(d, FLOW_JSON_SCHEMA)


def validate_heartbeat(d: dict) -> None:
    if BACKEND is not None:
        BACKEND.HeartbeatPayload.model_validate(d)
    else:  # pragma: no cover
        assert d["v"] == 1 and d["ts"] > 0 and 0 <= (d.get("bat") or 0) <= 100


def validate_ack(d: dict) -> None:
    if BACKEND is not None:
        BACKEND.ValveAckPayload.model_validate(d)
    else:  # pragma: no cover
        assert d["v"] == 1 and 1 <= len(d["cmd_id"]) <= 16 and d["state"] in ("open", "closed")


# ------------------------------------------------------------------ build + run the emitter
@pytest.fixture(scope="module")
def emitted(tmp_path_factory) -> list[dict]:
    out = tmp_path_factory.mktemp("fw") / "emit_payloads"
    cmd = [
        CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
        f"-I{FW / 'aquaguard_sensor'}",
        str(FW / "test" / "emit_payloads.cpp"),
        str(FW / "aquaguard_sensor" / "aquaguard_core.cpp"),
        "-o", str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    now = int(time.time())
    res = subprocess.run([str(out), str(now)], check=True, capture_output=True, text=True)
    rows = []
    for line in res.stdout.splitlines():
        scenario, kind, suffix, body = line.split("\t", 3)
        assert kind != "error", f"{scenario}: {body}"
        rows.append({"scenario": scenario, "kind": kind, "suffix": suffix, "raw": body,
                     "data": json.loads(body), "now": now})  # fmt: skip
    return rows


def of(rows, scenario, kind="flow"):
    return [r["data"] for r in rows if r["scenario"] == scenario and r["kind"] == kind]


# ------------------------------------------------------------------ schema validation
def test_every_payload_validates_against_backend_schema(emitted):
    assert len(emitted) > 50
    kinds = {r["kind"] for r in emitted}
    assert kinds == {"flow", "heartbeat", "ack"}
    for r in emitted:
        assert len(r["raw"].encode()) < 600  # backend limit is 2048 B; keep ESP32 buffers small
        d = r["data"]
        if r["kind"] == "flow":
            validate_flow(d)
            assert set(d) <= set(BASE_ORDER) | EXTRA_FIELDS, f"unknown field in {d}"
            assert d["ts"] <= r["now"] + 300
        elif r["kind"] == "heartbeat":
            validate_heartbeat(d)
        else:
            validate_ack(d)


def test_backend_model_is_the_real_one():
    # Guards against silently validating with the weaker fallback schema.
    assert BACKEND is not None, "backend/app/schemas/mqtt.py could not be imported"
    assert "tot" in BACKEND.FlowPayload.model_fields and "err" in BACKEND.FlowPayload.model_fields


def test_psi_tmp_always_null_without_driver(emitted):
    for r in emitted:
        if r["kind"] == "flow":
            assert r["data"]["psi"] is None and r["data"]["tmp"] is None


def test_ids_unique_within_each_scenario(emitted):
    for scenario in {r["scenario"] for r in emitted}:
        ids = [d["id"] for d in of(emitted, scenario)]
        assert len(ids) == len(set(ids)), scenario


# ------------------------------------------------------------------ simulator parity
def test_flow_field_for_field_parity_with_simulator(emitted):
    sim = flow_payload("a1b2c3d4", 1_760_000_000, 0.4, 1234.5, 87.0, -61)
    fw = of(emitted, "normal")[0]
    # the firmware keeps the exact existing fields, in the same order, before any extension
    assert list(fw)[: len(sim)] == list(sim) == BASE_ORDER
    for key in sim:
        fv, sv = fw[key], sim[key]
        if sv is None:
            assert fv is None, key
        elif key == "bat":  # sim: float percent, fw: int percent; both within backend rules
            assert isinstance(fv, int) and 0 <= fv <= 100
        else:
            assert isinstance(fv, type(sv)) or (
                isinstance(fv, int | float) and isinstance(sv, int | float)
            ), key
    assert re.fullmatch(r"[0-9a-f]{8}", fw["id"])
    assert set(fw) - set(sim) <= EXTRA_FIELDS


def test_heartbeat_parity_with_simulator(emitted):
    sim = heartbeat_payload(1_760_000_000, 3600, 87.0)
    fw = of(emitted, "normal", "heartbeat")[0]
    assert list(fw) == list(sim)
    assert fw["v"] == 1 and isinstance(fw["uptime"], int) and isinstance(fw["fw"], str)


def test_valve_ack_parity_with_simulator(emitted):
    sim = ack_payload("00ff10ab", "closed", 1_760_000_000)
    fw = of(emitted, "abnormal_flow", "ack")[0]
    assert list(fw) == list(sim) and fw["state"] == "closed" and fw["cmd_id"] == "00ff10ab"


# ------------------------------------------------------------------ scenario semantics
def test_normal_flow_has_no_fault_codes_and_cumulative_is_monotonic(emitted):
    rows = of(emitted, "normal")
    assert all("err" not in d for d in rows)
    assert all(d["valve"] == "open" for d in rows)
    tots = [d["tot"] for d in rows]
    assert tots == sorted(tots) and tots[-1] > 0
    assert all(abs(d["tot"] - d["vol"]) < 1e-6 for d in rows)  # fresh store: lifetime == session


def test_abnormal_flow_reports_but_does_not_act(emitted):
    rows = of(emitted, "abnormal_flow")
    codes = {c for d in rows for c in d.get("err", [])}
    assert {"burst", "leak_cont"} <= codes
    # valve stays "open" until the (manual) close command from the ack message was applied
    first_closed = next(i for i, d in enumerate(rows) if d["valve"] == "closed")
    assert all(d["valve"] == "open" for d in rows[:first_closed]) and first_closed > 0


def test_low_battery_sequence_with_hysteresis(emitted):
    rows = of(emitted, "low_battery")
    seq = [("bat_crit" in d.get("err", []), "bat_low" in d.get("err", []), d["bat"]) for d in rows]
    assert seq[0][:2] == (False, False)
    assert any(c for c, _, _ in seq)  # critical reached
    assert seq[-1][0] or seq[-1][1]  # partial recovery stays flagged (hysteresis)
    assert all(0 <= d["bat"] <= 100 and 2.5 <= d["vbat"] <= 4.5 for d in rows)


def test_sensor_error_codes(emitted):
    rows = of(emitted, "sensor_error")
    codes = {c for d in rows for c in d.get("err", [])}
    assert {"adc_fault", "flow_impossible", "stuck_line"} <= codes
    faulty = [d for d in rows if "adc_fault" in d.get("err", [])]
    # while the ADC is broken the battery is reported as unknown (null), never a made-up value
    assert faulty and faulty[0]["bat"] is None and "vbat" not in faulty[0]
    assert all(d["flow"] <= 500 for d in rows)


def test_no_battery_and_wifi_down_are_null_not_fabricated(emitted):
    d = of(emitted, "no_battery_no_wifi")[0]
    assert d["bat"] is None and "vbat" not in d and d["rssi"] is None
    validate_flow(d)


def test_reconnect_replay_oldest_first_with_original_timestamps(emitted):
    rows = of(emitted, "disconnect_reconnect_replay")
    ts = [d["ts"] for d in rows]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    assert len(rows) == 8  # 2 online + 6 replayed
    # replayed readings keep their capture time (60 s spacing), they are not re-stamped "now"
    assert all(b - a == 60 for a, b in zip(ts, ts[1:], strict=False))


def test_outage_longer_than_backfill_drops_and_flags(emitted):
    rows = of(emitted, "outage_longer_than_backfill")
    ts = [d["ts"] for d in rows]
    assert ts == sorted(ts)
    assert any("backfill_drop" in d.get("err", []) for d in rows)
    assert len(rows) < 15  # 14 min outage + 1 min online, but stale ones were not sent


def test_late_sntp_publishes_nothing_until_time_valid_then_flags_estimate(emitted):
    rows = of(emitted, "late_sntp")
    assert rows, "readings must be published once time is valid"
    assert all(d["ts"] > 1_700_000_000 for d in rows)  # never a bogus 1970 timestamp
    assert any("ts_est" in d["err"] for d in rows)
    assert "rst_brownout" in rows[0]["err"] and not any(
        "rst_brownout" in d.get("err", []) for d in rows[1:]
    )


# ------------------------------------------------------------------ private Mosquitto round trip
MOSQ = shutil.which("mosquitto") or (
    "/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(MOSQ is None, reason="mosquitto not installed")
def test_simulator_over_real_broker_matches_firmware_schema(emitted):
    """Run the unmodified simulator CLI against a private anonymous Mosquitto and validate what a
    subscriber receives with the same schema the firmware payloads were checked against."""
    aiomqtt = pytest.importorskip("aiomqtt")
    import asyncio

    port = _free_port()
    with tempfile.TemporaryDirectory() as td:
        conf = Path(td) / "m.conf"
        conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
        broker = subprocess.Popen(
            [MOSQ, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        sim = None
        try:
            for _ in range(50):
                with socket.socket() as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.1)
            seen: dict[str, list[dict]] = {"flow": [], "heartbeat": []}

            async def collect():
                async with aiomqtt.Client("127.0.0.1", port, identifier="fw-test-sub") as c:
                    await c.subscribe("aquaguard/fwtest/+/flow", qos=1)
                    await c.subscribe("aquaguard/fwtest/+/heartbeat", qos=1)
                    nonlocal sim
                    sim = subprocess.Popen(
                        [sys.executable, "-m", "edge.simulator", "--society", "fwtest",
                         "--flats", "2", "--speed", "3000", "--broker", f"127.0.0.1:{port}"],
                        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )  # fmt: skip
                    end = time.monotonic() + 6
                    while time.monotonic() < end:
                        try:
                            m = await asyncio.wait_for(c.messages.__anext__(), 1.0)
                        except TimeoutError:
                            continue
                        leaf = str(m.topic).rsplit("/", 1)[1]
                        seen[leaf].append(json.loads(m.payload))

            asyncio.run(collect())
        finally:
            for p in (sim, broker):
                if p is not None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:  # pragma: no cover
                        p.kill()
    assert len(seen["flow"]) >= 10 and seen["heartbeat"]
    fw_keys = set(of(emitted, "normal")[0])
    for d in seen["flow"]:
        # simulator ts runs ahead of wall clock at speed>1, so only check structure here
        assert set(d) <= fw_keys | EXTRA_FIELDS
        assert list(d)[: len(BASE_ORDER)] == BASE_ORDER
        assert set(BASE_ORDER) <= set(d) and d["psi"] is None and d["tmp"] is None
        BACKEND.FlowPayload.model_validate(d, context={"max_future_skew_s": 10**9})
    for d in seen["heartbeat"]:
        validate_heartbeat(d)

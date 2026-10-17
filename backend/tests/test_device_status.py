"""Persisted firmware extras (device_status), device-fault events, API exposure."""

from __future__ import annotations

import json
import time

from sqlalchemy import select

from app.models import AuditLog
from app.repositories import DeviceStatusRepository
from app.services import ingest
from tests.helpers import TOPIC, flow_msg


async def _status(db, flat_id):
    async with db.session() as s:
        return await DeviceStatusRepository(s).get_for_flat(flat_id)


async def _faults(db):
    async with db.session() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == "device_fault"))).all()
    return [r[0].details for r in rows]


async def test_extras_are_stored_as_latest_device_status(rt, db, world):
    msg = flow_msg(tot=98765.4, vbat=4.01, valve="open", fw="2.0.0-dev", err=["bat_low"])
    await ingest.handle_message(rt, TOPIC, msg)
    row = await _status(db, world.flats[0].id)
    assert (row.firmware_version, row.battery_volts, row.total_volume) == (
        "2.0.0-dev",
        4.01,
        98765.4,
    )
    assert row.valve_reported == "open" and row.err_codes == ["bat_low"] and row.battery_pct == 90


async def test_plain_reading_without_extras_creates_no_status_row_only_bat(rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg())
    row = await _status(db, world.flats[0].id)
    assert row is not None and row.firmware_version is None and row.err_codes == []


async def test_safety_codes_raise_audit_and_websocket_once(rt, db, world, monkeypatch):
    sent = []
    real = rt.ws.broadcast

    async def spy(msg, keys):
        sent.append(msg)
        return await real(msg, keys)

    monkeypatch.setattr(rt.ws, "broadcast", spy)
    t = time.time() - 30
    await ingest.handle_message(rt, TOPIC, flow_msg(mid="00000001", ts=t, err=["close_unconf"]))
    await ingest.handle_message(
        rt, TOPIC, flow_msg(mid="00000002", ts=t + 1, err=["close_unconf", "stuck_line"])
    )
    faults = await _faults(db)
    assert [f["code"] for f in faults] == ["close_unconf", "stuck_line"]  # each appears once
    assert faults[0]["severity"] == "critical" and "check the valve" in faults[0]["action_required"]
    assert [m["code"] for m in sent if m["type"] == "device_fault"] == [
        "close_unconf",
        "stuck_line",
    ]
    assert any(m["type"] == "sensor" and "device" in m for m in sent)
    assert rt.counters["device_fault"] == 2


async def test_warning_and_informational_codes(rt, db, world):
    t = time.time() - 30
    await ingest.handle_message(
        rt, TOPIC, flow_msg(mid="00000001", ts=t, err=["backfill_drop", "ts_est", "leak_cont"])
    )
    faults = await _faults(db)
    assert {f["code"]: f["severity"] for f in faults} == {
        "backfill_drop": "warning",
        "ts_est": "warning",
    }  # leak_cont is the device's own detector: stored, not escalated


async def test_cleared_and_reraised_code_alerts_again(rt, db, world):
    t = time.time() - 60
    rt.device_cache.clear()
    for i, err in enumerate((["bat_low"], [], ["bat_low"])):
        await ingest.handle_message(rt, TOPIC, flow_msg(mid=f"0000000{i}", ts=t + i, err=err))
    assert [f["code"] for f in await _faults(db)] == ["bat_low", "bat_low"]


async def test_older_report_does_not_overwrite_newer(rt, db, world):
    t = time.time() - 30
    await ingest.handle_message(rt, TOPIC, flow_msg(mid="00000001", ts=t, fw="2.0", err=["x_y"]))
    await ingest.handle_message(rt, TOPIC, flow_msg(mid="00000002", ts=t - 20, fw="1.0", err=[]))
    row = await _status(db, world.flats[0].id)
    assert row.firmware_version == "2.0" and row.err_codes == ["x_y"]


async def test_heartbeat_updates_firmware_and_battery(rt, db, world):
    hb = json.dumps({"v": 1, "ts": time.time() - 1, "uptime": 5, "bat": 77.5, "fw": "2.1"}).encode()
    await ingest.handle_message(rt, "aquaguard/green-valley/101/heartbeat", hb)
    row = await _status(db, world.flats[0].id)
    assert (row.firmware_version, row.battery_pct) == ("2.1", 77.5)


async def test_api_exposes_device_to_staff_only(client, rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg(fw="2.0", err=["close_unconf"], valve="closed"))
    fid = world.flats[0].id
    admin = (await client.get(f"/api/v1/flats/{fid}", headers=world.admin)).json()
    assert admin["device"]["err_codes"] == ["close_unconf"]
    assert admin["device"]["valve_reported"] == "closed"
    maint = (await client.get(f"/api/v1/flats/{fid}", headers=world.maintenance)).json()
    assert maint["device"]["firmware_version"] == "2.0"
    res = (await client.get(f"/api/v1/flats/{fid}", headers=world.resident)).json()
    assert res["device"] is None and res["auto_shutoff_notes"] is None


async def test_society_devices_list_is_staff_scoped(client, rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg(fw="2.0", err=["bat_low"]))
    sid = world.society.id
    rows = (await client.get(f"/api/v1/societies/{sid}/devices", headers=world.maintenance)).json()
    assert len(rows) == 6
    first = next(r for r in rows if r["unit_number"] == "101")
    assert first["device"]["err_codes"] == ["bat_low"] and first["sensor_status"]["status"]
    assert next(r for r in rows if r["unit_number"] == "102")["device"] is None
    assert (
        await client.get(f"/api/v1/societies/{sid}/devices", headers=world.resident)
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/societies/{sid}/devices", headers=world.maintenance_b)
    ).status_code == 404

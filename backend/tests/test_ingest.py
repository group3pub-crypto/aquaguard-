import json
import time

import pytest

from app.repositories import (
    AlertRepository,
    AnomalyRepository,
    ReadingRepository,
    SensorHealthRepository,
)
from app.services import ingest, maintenance
from tests.helpers import TOPIC, flow_msg

HB_TOPIC = "aquaguard/green-valley/101/heartbeat"


async def flat_id(db):
    from app.repositories import FlatRepository

    async with db.session() as s:
        return (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id


async def readings(db, fid):
    async with db.session() as s:
        return await ReadingRepository(s).find(fid, limit=1000)


async def test_valid_flow_message_is_stored_and_marks_sensor_online(rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg())
    fid = await flat_id(db)
    rows = await readings(db, fid)
    assert len(rows) == 1 and rows[0].flow_rate == 1.5 and rows[0].message_id == "a1b2c3d4"
    async with db.session() as s:
        health = await SensorHealthRepository(s).get_for_flat(fid)
    assert health.status == "online" and rt.counters["flow_ok"] == 1


async def test_duplicate_message_is_idempotent(rt, db, world):
    msg = flow_msg()
    for _ in range(3):
        await ingest.handle_message(rt, TOPIC, msg)
    assert len(await readings(db, await flat_id(db))) == 1
    assert rt.counters["flow_ok"] == 1 and rt.counters["flow_duplicate"] == 2
    assert not rt.dead_letters


async def test_same_id_for_other_flat_is_not_a_duplicate(rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg())
    await ingest.handle_message(rt, "aquaguard/green-valley/102/flow", flow_msg())
    assert rt.counters["flow_ok"] == 2


async def test_out_of_order_messages_are_all_stored(rt, db, world):
    now = time.time()
    for i, ts in enumerate([now - 10, now - 300, now - 100, now - 500]):
        await ingest.handle_message(rt, TOPIC, flow_msg(mid=f"0000000{i}", ts=ts))
    rows = await readings(db, await flat_id(db))
    assert len(rows) == 4
    stamps = [r.timestamp for r in rows]
    assert stamps == sorted(stamps, reverse=True)  # repo returns newest first regardless of arrival


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"not json", "invalid_payload"),
        (b"[]", "invalid_payload"),
        (flow_msg(flow=-1.0), "invalid_payload:flow"),
        (flow_msg(flow=501.0), "invalid_payload:flow"),
        (flow_msg(mid="zzzzzzzz"), "invalid_payload:id"),
        (flow_msg(mid="abc"), "invalid_payload:id"),
        (flow_msg(ts=time.time() + 86_400), "invalid_payload:ts"),
        (flow_msg(bat=150), "invalid_payload:bat"),
        (b'{"v":1,"id":"a1b2c3d4","ts":1.7e9,"flow":NaN,"vol":0}', "invalid_payload"),
        (
            json.dumps({"v": 2, "id": "a1b2c3d4", "ts": 1.7e9, "flow": 1, "vol": 0}).encode(),
            "invalid_payload:v",
        ),
    ],
)
async def test_malformed_payloads_are_rejected_and_dead_lettered(rt, db, world, payload, reason):
    await ingest.handle_message(rt, TOPIC, payload)
    assert await readings(db, await flat_id(db)) == []
    assert rt.counters["flow_failed"] == 1
    assert rt.dead_letters[-1]["reason"].startswith(reason)


async def test_oversize_payload_rejected(rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg() + b" " * 2100)
    assert rt.dead_letters[-1]["reason"] == "oversize_payload"
    assert await readings(db, await flat_id(db)) == []


async def test_unknown_flat_and_bad_topic_dead_lettered(rt, db, world):
    await ingest.handle_message(rt, "aquaguard/green-valley/999/flow", flow_msg())
    assert rt.dead_letters[-1]["reason"] == "unknown_flat"
    await ingest.handle_message(rt, "other/topic", flow_msg())
    assert rt.dead_letters[-1]["reason"] == "unrecognised_topic"
    await ingest.handle_message(rt, "aquaguard/control/fault", b"{}")
    assert len(rt.dead_letters) == 3


async def test_dead_letter_queue_is_bounded(rt, world):
    rt.settings.mqtt_device_rate_per_min = 0  # not under test here
    for i in range(rt.settings.dead_letter_size + 25):
        await ingest.handle_message(rt, TOPIC, b"junk%d" % i)
    assert len(rt.dead_letters) == rt.settings.dead_letter_size


async def test_health_details_exposes_dead_letters_without_payloads(client, rt, world):
    await ingest.handle_message(rt, TOPIC, b'{"secret": "payload"}')
    assert (await client.get("/health")).json() == {"status": "ok"}  # public: nothing else
    body = (await client.get("/health/details", headers=world.admin)).json()
    assert body["db"] == "ok" and body["dead_letters"]["count"] == 1
    assert "secret" not in json.dumps(body)
    assert body["ingest"]["flow_failed"] == 1


async def test_db_failure_is_contained_and_counted(rt, world, monkeypatch):
    async def boom(self, reading):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ReadingRepository, "add_idempotent", boom)
    await ingest.handle_message(rt, TOPIC, flow_msg())  # must not raise
    assert rt.counters["flow_failed"] == 1
    assert rt.dead_letters[-1]["reason"] == "error:RuntimeError"


async def test_heartbeat_updates_sensor_health(rt, db, world):
    hb = json.dumps({"v": 1, "ts": time.time(), "uptime": 100, "bat": 80, "fw": "1.0.2"}).encode()
    await ingest.handle_message(rt, HB_TOPIC, hb)
    async with db.session() as s:
        h = await SensorHealthRepository(s).get_for_flat(await flat_id(db))
    assert h.status == "online" and rt.counters["heartbeat_ok"] == 1


@pytest.mark.parametrize("payload", [b"xx", b'{"v":1}', b'{"v":1,"ts":1,"bat":999}'])
async def test_heartbeat_errors_are_handled(rt, world, payload):
    await ingest.handle_message(rt, HB_TOPIC, payload)
    assert rt.counters["heartbeat_failed"] == 1 and rt.dead_letters


async def test_heartbeat_for_unknown_flat(rt, world):
    hb = json.dumps({"v": 1, "ts": time.time()}).encode()
    await ingest.handle_message(rt, "aquaguard/green-valley/999/heartbeat", hb)
    assert rt.dead_letters[-1]["reason"] == "unknown_flat"


def test_sensor_status_thresholds():
    from datetime import timedelta

    from tests.conftest import T0

    assert ingest.sensor_status(None, T0) == "offline"
    assert ingest.sensor_status(T0 - timedelta(seconds=90), T0) == "online"
    assert ingest.sensor_status(T0 - timedelta(minutes=3), T0) == "late"
    assert ingest.sensor_status(T0 - timedelta(minutes=5, seconds=1), T0) == "offline"


async def test_refresh_sensor_health_marks_offline_and_broadcasts(rt, db, world):
    from datetime import UTC, datetime, timedelta

    fid = await flat_id(db)
    async with db.session() as s:
        await SensorHealthRepository(s).upsert(
            fid, last_seen=datetime.now(UTC) - timedelta(minutes=10), status="online"
        )
    assert await maintenance.refresh_sensor_health(rt, datetime.now(UTC)) == 1
    async with db.session() as s:
        h = await SensorHealthRepository(s).get_for_flat(fid)
    assert h.status == "offline" and h.missed_readings >= 9


async def test_valve_ack_confirms_pending_command(client, rt, db, world, publisher):
    fid = str(world.flats[0].id)
    cmd = (await client.post(f"/api/v1/valves/{fid}/close", json={}, headers=world.admin)).json()
    short = publisher.sent[0][1]["id"]
    assert cmd["status"] == "pending" and cmd["id"].replace("-", "").startswith(short)
    ack = json.dumps({"v": 1, "cmd_id": short, "state": "closed", "ts": time.time()}).encode()
    await ingest.handle_message(rt, "aquaguard/green-valley/101/valve/state", ack)
    status = (await client.get(f"/api/v1/valves/{fid}", headers=world.admin)).json()
    assert status["valve_state"] == "closed" and status["pending"] == []
    assert status["recent"][0]["status"] == "confirmed"


async def test_flow_triggers_detection_alert_and_ws_broadcast(rt, db, world):
    from datetime import UTC, datetime

    sent = []

    async def capture(message, key=None):
        sent.append(message)
        return 0

    rt.ws.broadcast = capture
    base = datetime.now(UTC).timestamp() - 540
    for i in range(12):  # 5.5 min of 30 L/min continuous flow: looks like a burst
        await ingest.handle_message(
            rt, TOPIC, flow_msg(mid=f"{i:08x}", ts=base + i * 30, flow=30.0)
        )
    async with db.session() as s:
        fid = await flat_id(db)
        anomalies = await AnomalyRepository(s).find(flat_id=fid)
        alerts = await AlertRepository(s).find(flat_id=fid)
    assert len(anomalies) == 1 and anomalies[0].anomaly_type == "burst"
    assert len(alerts) == 1 and alerts[0].message_source == "template"
    types = {m["type"] for m in sent}
    assert {"reading", "anomaly", "alert"} <= types


async def test_fractional_battery_percent_is_accepted(rt, db, world):
    """The simulator/firmware report battery with decimals (e.g. 99.9)."""
    await ingest.handle_message(rt, TOPIC, flow_msg(bat=99.9))
    assert rt.counters["flow_ok"] == 1 and rt.counters["flow_failed"] == 0
    rows = await readings(db, await flat_id(db))
    assert rows[0].battery_pct == 100


async def test_stale_reading_is_rejected_and_counted(rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg(ts=time.time() - 3600))
    assert rt.counters["flow_ok"] == 0 and rt.counters["stale_reading"] == 1
    assert rt.counters["flow_failed"] == 1
    assert list(rt.dead_letters)[-1]["reason"] == "stale_reading"


async def test_reading_inside_age_window_is_accepted(rt, db, world):
    await ingest.handle_message(rt, TOPIC, flow_msg(ts=time.time() - 540))
    assert rt.counters["flow_ok"] == 1


async def test_unknown_society_or_unit_is_rejected(rt, db, world):
    await ingest.handle_message(rt, "aquaguard/nowhere/101/flow", flow_msg())
    await ingest.handle_message(rt, "aquaguard/green-valley/999/flow", flow_msg())
    assert rt.counters["flow_ok"] == 0 and rt.counters["flow_failed"] == 2


async def test_per_device_rate_limit_isolates_devices(rt, db, world):
    rt.settings.mqtt_device_rate_per_min = 60  # burst capacity 15
    for i in range(40):
        await ingest.handle_message(rt, TOPIC, flow_msg(mid=f"{i:08x}"))
    assert rt.counters["flow_ok"] == 15 and rt.counters["rate_limited"] == 25
    # another device is unaffected by 101 being throttled
    await ingest.handle_message(rt, "aquaguard/green-valley/102/flow", flow_msg(mid="ffffffff"))
    assert rt.counters["flow_ok"] == 16


async def test_backfill_path_accepts_old_rows_without_detection(rt, db, world):
    from datetime import UTC, datetime, timedelta

    from app.repositories import FlatRepository

    fid = await flat_id(db)
    old = datetime.now(UTC) - timedelta(days=3)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        n = await ingest.backfill_readings(
            s,
            flat,
            [(old + timedelta(minutes=i), 1.0, 1.0, f"{i:08x}") for i in range(5)],
            actor="admin",
        )
        again = await ingest.backfill_readings(
            s, flat, [(old, 1.0, 1.0, "00000000")], actor="admin"
        )
    assert n == 5 and again == 0 and len(await readings(db, fid)) == 5
    assert rt.last_detect == {}

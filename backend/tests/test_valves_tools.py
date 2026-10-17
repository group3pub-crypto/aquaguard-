import uuid
from datetime import UTC, datetime, timedelta

from app.models import FlowReading
from app.repositories import AuditRepository
from tests.conftest import make_alert

API = "/api/v1"


async def test_close_valve_publishes_mqtt_and_records_pending_command(client, world, publisher, db):
    fid = str(world.flats[0].id)
    r = await client.post(
        f"{API}/valves/{fid}/close",
        json={"source": "manual_dashboard", "issued_by": "mallory", "reason": "leak"},
        headers=world.admin,
    )
    body = r.json()
    assert r.status_code == 200 and body["status"] == "pending" and body["command"] == "close"
    assert body["issued_by"] == "admin"  # authenticated actor, not the body
    topic, payload = publisher.sent[0]
    assert topic == "aquaguard/green-valley/101/valve/cmd"
    assert set(payload) == {"v", "id", "cmd", "src", "ts"} and len(payload["id"]) == 8
    async with db.session() as s:
        audit = await AuditRepository(s).find(entity_type="valve")
    assert audit[0].action == "valve_close_pending" and audit[0].details["reason"] == "leak"


async def test_open_valve_and_status(client, world, publisher):
    fid = str(world.flats[1].id)
    assert (
        await client.post(f"{API}/valves/{fid}/open", json={}, headers=world.admin)
    ).status_code == 200
    st = (await client.get(f"{API}/valves/{fid}", headers=world.resident)).json()
    assert (
        st["valve_state"] == "unknown"
        and len(st["pending"]) == 1
        and publisher.sent[0][1]["cmd"] == "open"
    )


async def test_valve_publish_failure_marks_command_failed(client, world, publisher):
    publisher.fail = True
    r = await client.post(f"{API}/valves/{world.flats[0].id}/close", json={}, headers=world.admin)
    assert r.status_code == 200 and r.json()["status"] == "failed"


async def test_valve_validation_and_unknown_flat(client, world):
    assert (
        await client.post(f"{API}/valves/{uuid.uuid4()}/close", json={}, headers=world.admin)
    ).status_code == 404
    bad = await client.post(
        f"{API}/valves/{world.flats[0].id}/close", json={"source": "auto"}, headers=world.admin
    )
    assert bad.status_code == 422  # clients may not claim to be the auto controller
    extra = await client.post(
        f"{API}/valves/{world.flats[0].id}/close", json={"x": 1}, headers=world.admin
    )
    assert extra.status_code == 422


async def test_pending_valve_commands_time_out(client, world, rt, db):
    from app.services import valve_control

    await client.post(f"{API}/valves/{world.flats[0].id}/close", json={}, headers=world.admin)
    async with db.session() as s:
        assert await valve_control.expire_pending(s, datetime.now(UTC) + timedelta(minutes=5)) == 1
    st = (await client.get(f"{API}/valves/{world.flats[0].id}", headers=world.admin)).json()
    assert st["pending"] == [] and st["recent"][0]["status"] == "timeout"


# -------------------------------------------------------------------- tools, flats, societies
async def test_tools_active_alerts_and_losses(client, db, rt, world):
    now = datetime.now(UTC)
    await make_alert(db, rt, world.flats[0].id, now=now - timedelta(hours=2))
    r = await client.post(
        f"{API}/tools/active-alerts",
        json={"society_id": str(world.society.id)},
        headers=world.resident,
    )
    assert r.json()["count"] == 1
    r = await client.post(
        f"{API}/tools/top-losses",
        json={"society_id": str(world.society.id), "days": 7},
        headers=world.maintenance,
    )
    losses = r.json()["losses"]
    assert losses[0]["unit"] == "101" and losses[0]["estimated_loss_l"] > 0


async def test_tools_flat_usage_and_weekly_report(client, db, world):
    now = datetime.now(UTC)
    async with db.session() as s:
        for i in range(5):
            s.add(
                FlowReading(
                    flat_id=world.flats[0].id,
                    timestamp=now - timedelta(minutes=i),
                    flow_rate=1,
                    volume=2.0,
                    message_id=f"{i:08x}",
                )
            )
    usage = await client.post(
        f"{API}/tools/flat-usage",
        json={"flat_id": str(world.flats[0].id), "hours": 1},
        headers=world.resident,
    )
    assert usage.json()["volume_l"] == 10.0
    rep = await client.post(
        f"{API}/tools/weekly-report",
        json={"society_id": str(world.society.id)},
        headers=world.maintenance,
    )
    md = rep.json()["markdown"]
    assert md.startswith("# Weekly water report: Green Valley Apartments") and "10 L" in md
    ghost = await client.post(
        f"{API}/tools/weekly-report",
        json={"society_id": str(uuid.uuid4())},
        headers=world.maintenance,
    )
    assert ghost.status_code == 404


async def test_tool_valve_control_uses_bot_source(client, world, publisher):
    r = await client.post(
        f"{API}/tools/valve-control",
        json={"flat_id": str(world.flats[0].id), "action": "close", "reason": "tg"},
        headers=world.admin,
    )
    assert (
        r.status_code == 200
        and r.json()["source"] == "manual_bot"
        and publisher.sent[0][1]["src"] == "manual_bot"
    )


async def test_societies_and_flats_endpoints(client, db, world):
    soc = (await client.get(f"{API}/societies", headers=world.resident)).json()
    assert len(soc) == 1 and soc[0]["slug"] == "green-valley"
    assert len((await client.get(f"{API}/societies", headers=world.admin)).json()) == 2
    stats = (
        await client.get(f"{API}/societies/{world.society.id}/stats", headers=world.maintenance)
    ).json()
    assert stats["flats"] == 6 and stats["sensors_offline"] == 6 and stats["active_alerts"] == 0
    assert (
        await client.get(f"{API}/societies/{uuid.uuid4()}", headers=world.resident)
    ).status_code == 404
    flats = (
        await client.get(
            f"{API}/flats?society_id={world.society.id}&limit=4", headers=world.maintenance
        )
    ).json()
    assert len(flats) == 4
    assert (
        await client.get(f"{API}/flats?society_id={uuid.uuid4()}", headers=world.maintenance)
    ).json() == []


async def test_flat_detail_readings_and_baseline_flow(client, db, world):
    fid = world.flats[0].id
    now = datetime.now(UTC)
    async with db.session() as s:
        for i in range(10):
            s.add(
                FlowReading(
                    flat_id=fid,
                    timestamp=now - timedelta(minutes=i),
                    flow_rate=float(i),
                    volume=0.1,
                    message_id=f"{i:08x}",
                )
            )
    detail = (await client.get(f"{API}/flats/{fid}", headers=world.resident)).json()
    assert detail["current_flow"] == 0.0 and detail["sensor_status"]["status"] == "offline"
    rows = (await client.get(f"{API}/flats/{fid}/readings?limit=3", headers=world.resident)).json()
    assert [r["flow_rate"] for r in rows] == [0.0, 1.0, 2.0]
    window = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert (
        len(
            (
                await client.get(
                    f"{API}/flats/{fid}/readings", params={"from": window}, headers=world.resident
                )
            ).json()
        )
        == 6
    )
    assert (
        await client.get(f"{API}/flats/{fid}/readings?limit=5001", headers=world.resident)
    ).status_code == 422
    assert (
        await client.get(f"{API}/flats/{fid}/baseline", headers=world.resident)
    ).status_code == 404
    trained = await client.post(f"{API}/flats/{fid}/baseline/retrain", headers=world.admin)
    assert (
        trained.status_code == 200
        and len(trained.json()["slots"]) == 168
        and trained.json()["version"] == 1
    )
    assert (await client.get(f"{API}/flats/{fid}/baseline", headers=world.resident)).json()[
        "version"
    ] == 1
    assert (
        await client.get(f"{API}/flats/{uuid.uuid4()}", headers=world.resident)
    ).status_code == 404


async def test_forecast_generate_and_fetch(client, world):
    fid = world.flats[0].id
    assert (
        await client.get(f"{API}/flats/{fid}/forecast", headers=world.resident)
    ).status_code == 404
    r = await client.post(
        f"{API}/flats/{fid}/forecast/generate", json={"horizon_hours": 12}, headers=world.admin
    )
    body = r.json()
    assert (
        r.status_code == 200
        and len(body["predictions"]) == 12
        and body["model_version"] == "baseline-median"
    )
    assert (await client.get(f"{API}/flats/{fid}/forecast", headers=world.resident)).json()[
        "id"
    ] == body["id"]
    assert (
        await client.post(
            f"{API}/flats/{fid}/forecast/generate", json={"horizon_hours": 999}, headers=world.admin
        )
    ).status_code == 422

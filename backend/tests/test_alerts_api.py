import uuid
from datetime import UTC, datetime, timedelta

from app.repositories import AuditRepository
from tests.conftest import make_alert

API = "/api/v1"


async def new(db, rt, world, **kw):
    now = datetime.now(UTC)
    _, aid = await make_alert(db, rt, world.flats[0].id, now=now, **kw)
    return str(aid)


async def test_list_filter_and_pagination(client, db, rt, world):
    for i, atype in enumerate(["leak", "burst", "overuse"]):
        await make_alert(db, rt, world.flats[i].id, atype=atype, severity=0.6)
    r = await client.get(f"{API}/alerts", headers=world.maintenance)
    assert r.status_code == 200 and len(r.json()) == 3
    assert len((await client.get(f"{API}/alerts?limit=2", headers=world.maintenance)).json()) == 2
    assert (
        len((await client.get(f"{API}/alerts?limit=2&offset=2", headers=world.maintenance)).json())
        == 1
    )
    by_flat = await client.get(f"{API}/alerts?flat_id={world.flats[1].id}", headers=world.maintenance)
    assert len(by_flat.json()) == 1
    assert (await client.get(f"{API}/alerts?state=alert", headers=world.maintenance)).json() != []
    assert (await client.get(f"{API}/alerts?state=resolved", headers=world.maintenance)).json() == []


async def test_pagination_bounds(client, world):
    for q in ("limit=0", "limit=501", "offset=-1", "state=bogus"):
        assert (await client.get(f"{API}/alerts?{q}", headers=world.resident)).status_code == 422


async def test_get_alert_and_404(client, db, rt, world):
    aid = await new(db, rt, world)
    assert (await client.get(f"{API}/alerts/{aid}", headers=world.resident)).json()["id"] == aid
    assert (
        await client.get(f"{API}/alerts/{uuid.uuid4()}", headers=world.resident)
    ).status_code == 404
    assert (await client.get(f"{API}/alerts/not-a-uuid", headers=world.resident)).status_code == 422


async def test_acknowledge_records_authenticated_user_not_body(client, db, rt, world):
    aid = await new(db, rt, world)
    r = await client.post(
        f"{API}/alerts/{aid}/acknowledge",
        json={"acknowledged_by": "mallory"},
        headers=world.maintenance,
    )
    assert r.status_code == 200
    body = r.json()
    assert (
        body["state"] == "acknowledged"
        and body["acknowledged_by"] == "maint"
        and body["acknowledged_at"]
    )
    async with db.session() as s:
        row = (await AuditRepository(s).find(action="acknowledge"))[0]
    assert row.actor == "maint" and row.flat_id == world.flats[0].id


async def test_acknowledge_without_body_works(client, db, rt, world):
    aid = await new(db, rt, world)
    assert (
        await client.post(f"{API}/alerts/{aid}/acknowledge", headers=world.admin)
    ).status_code == 200


async def test_snooze_valid_and_invalid_durations(client, db, rt, world):
    aid = await new(db, rt, world, severity=0.4)
    for bad in (0, 10, 45, 180, "60"):
        r = await client.post(
            f"{API}/alerts/{aid}/snooze",
            json={"duration_minutes": bad, "snoozed_by": "x"},
            headers=world.admin,
        )
        assert r.status_code == 422, bad
    r = await client.post(
        f"{API}/alerts/{aid}/snooze",
        json={"duration_minutes": 30, "snoozed_by": "x"},
        headers=world.admin,
    )
    body = r.json()
    assert r.status_code == 200 and body["state"] == "snoozed"
    until = datetime.fromisoformat(body["snoozed_until"])
    assert timedelta(minutes=29) < until - datetime.now(UTC) <= timedelta(minutes=30)


async def test_override_admin_only_needs_reason_and_sets_cooldown(client, db, rt, world):
    aid = await new(db, rt, world)
    assert (
        await client.post(
            f"{API}/alerts/{aid}/override", json={"reason": "x"}, headers=world.maintenance
        )
    ).status_code == 403
    assert (
        await client.post(f"{API}/alerts/{aid}/override", json={"reason": ""}, headers=world.admin)
    ).status_code == 422
    r = await client.post(
        f"{API}/alerts/{aid}/override",
        json={"reason": "garden", "overridden_by": "zed"},
        headers=world.admin,
    )
    assert r.json()["state"] == "overridden"
    async with db.session() as s:
        row = (await AuditRepository(s).find(action="override"))[0]
    assert row.actor == "admin" and row.details["reason"] == "garden"


async def test_resolve_and_conflicts(client, db, rt, world):
    aid = await new(db, rt, world)
    r = await client.post(
        f"{API}/alerts/{aid}/resolve", json={"resolved_by": "x"}, headers=world.maintenance
    )
    assert r.status_code == 200 and r.json()["state"] == "resolved" and r.json()["resolved_at"]
    for action, body in (
        ("resolve", {}),
        ("acknowledge", {}),
        ("snooze", {"duration_minutes": 15}),
    ):
        again = await client.post(f"{API}/alerts/{aid}/{action}", json=body, headers=world.admin)
        assert again.status_code == 409, action


async def test_unknown_alert_actions_return_404(client, world):
    ghost = uuid.uuid4()
    assert (
        await client.post(f"{API}/alerts/{ghost}/resolve", headers=world.admin)
    ).status_code == 404


async def test_anomalies_endpoints(client, db, rt, world):
    anomaly_id, _ = await make_alert(
        db, rt, world.flats[0].id, atype="burst", now=datetime.now(UTC)
    )
    r = await client.get(f"{API}/anomalies?type=burst&active_only=true", headers=world.resident)
    assert [a["id"] for a in r.json()] == [str(anomaly_id)]
    assert (await client.get(f"{API}/anomalies?type=leak", headers=world.resident)).json() == []
    assert (
        await client.get(f"{API}/anomalies?type=nonsense", headers=world.resident)
    ).status_code == 422
    one = await client.get(f"{API}/anomalies/{anomaly_id}", headers=world.resident)
    assert one.json()["anomaly_type"] == "burst"
    assert (
        await client.get(f"{API}/anomalies/{uuid.uuid4()}", headers=world.resident)
    ).status_code == 404

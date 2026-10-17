"""The audit actor is the authenticated account; only service accounts may assert on_behalf_of."""

import pytest
from sqlalchemy import select

from app.actor import sanitize_on_behalf_of
from app.models import ValveCommand
from app.repositories import AlertRepository, AuditRepository
from tests.conftest import make_alert

API = "/api/v1"


async def new_alert(db, rt, world, **kw):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=kw.pop("severity", 0.6), **kw)
    return str(aid)


async def audit_for(db, entity_type, action):
    async with db.session() as s:
        return [
            r
            for r in await AuditRepository(s).find(entity_type=entity_type, limit=100)
            if r.action == action
        ]


# ------------------------------------------------------------------ body fields never set the actor
@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        ("acknowledge", {}, "acknowledged_by"),
        ("snooze", {"duration_minutes": 30}, "snoozed_by"),
        ("resolve", {}, "resolved_by"),
    ],
)
async def test_staff_action_actor_is_the_caller_not_the_body(
    client, db, rt, world, path, body, field
):
    aid = await new_alert(db, rt, world)
    r = await client.post(
        f"{API}/alerts/{aid}/{path}", json={**body, field: "mallory"}, headers=world.maintenance
    )
    assert r.status_code == 200
    rows = await audit_for(db, "alert", path)
    assert rows[0].actor == "maint" and "mallory" not in str(rows[0].details)
    assert "mallory" not in r.text


async def test_override_actor_is_the_caller_not_the_body(client, db, rt, world):
    aid = await new_alert(db, rt, world)
    r = await client.post(
        f"{API}/alerts/{aid}/override",
        json={"reason": "pool", "overridden_by": "mallory"},
        headers=world.admin,
    )
    assert r.status_code == 200
    assert (await audit_for(db, "alert", "override"))[0].actor == "admin"


async def test_tools_valve_control_issued_by_forgery_is_ignored(client, db, world, publisher):
    r = await client.post(
        f"{API}/tools/valve-control",
        json={"flat_id": str(world.flats[0].id), "action": "close", "issued_by": "the-ceo"},
        headers=world.admin,
    )
    assert r.status_code == 200 and r.json()["issued_by"] == "admin"
    rows = await audit_for(db, "valve", "valve_close_pending")
    assert rows[0].actor == "admin" and "the-ceo" not in str(rows[0].details)
    async with db.session() as s:
        cmd = (await s.execute(select(ValveCommand))).scalar_one()
    assert cmd.issued_by == "admin"


async def test_valve_endpoint_issued_by_forgery_is_ignored(client, db, world, publisher):
    r = await client.post(
        f"{API}/valves/{world.flats[0].id}/close",
        json={"issued_by": "mallory"},
        headers=world.admin,
    )
    assert r.json()["issued_by"] == "admin"
    assert (await audit_for(db, "valve", "valve_close_pending"))[0].actor == "admin"


# ------------------------------------------------------------------ on_behalf_of: service accounts only
async def test_non_service_callers_cannot_assert_on_behalf_of(client, db, rt, world, publisher):
    aid = await new_alert(db, rt, world)
    flat = str(world.flats[0].id)
    attempts = [
        (f"{API}/alerts/{aid}/acknowledge", {"on_behalf_of": "tg:1"}, world.maintenance),
        (
            f"{API}/alerts/{aid}/snooze",
            {"duration_minutes": 15, "on_behalf_of": "tg:1"},
            world.admin,
        ),
        (f"{API}/alerts/{aid}/resolve", {"on_behalf_of": "tg:1"}, world.admin),
        (f"{API}/alerts/{aid}/override", {"reason": "r", "on_behalf_of": "tg:1"}, world.admin),
        (f"{API}/valves/{flat}/close", {"on_behalf_of": "tg:1"}, world.admin),
        (f"{API}/valves/{flat}/open", {"on_behalf_of": "tg:1"}, world.admin),
        (
            f"{API}/tools/valve-control",
            {"flat_id": flat, "action": "close", "on_behalf_of": "tg:1"},
            world.admin,
        ),
    ]
    for url, body, headers in attempts:
        r = await client.post(url, json=body, headers=headers)
        assert r.status_code == 422, url
        assert "service accounts" in r.text
    async with db.session() as s:
        assert (await AlertRepository(s).get(__import__("uuid").UUID(aid))).state == "alert"
        assert (await s.execute(select(ValveCommand))).scalars().all() == []
    assert publisher.sent == []


async def test_service_account_on_behalf_of_is_recorded_as_asserted_not_verified(
    client, db, rt, world
):
    aid = await new_alert(db, rt, world)
    r = await client.post(
        f"{API}/alerts/{aid}/acknowledge",
        json={"on_behalf_of": "telegram:42 (Ravi, flat 101)"},
        headers=world.bot,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["acknowledged_by"] == "bot"  # authenticated actor, always
    assert body["on_behalf_of"] == "telegram:42 (Ravi, flat 101)"
    assert body["on_behalf_of_verified"] is False
    row = (await audit_for(db, "alert", "acknowledge"))[0]
    assert row.actor == "bot"
    assert row.details["on_behalf_of"] == "telegram:42 (Ravi, flat 101)"
    assert row.details["on_behalf_of_verified"] is False


async def test_service_account_without_on_behalf_of_has_none_in_audit(client, db, rt, world):
    aid = await new_alert(db, rt, world)
    r = await client.post(f"{API}/alerts/{aid}/resolve", json={}, headers=world.bot)
    assert r.json()["on_behalf_of"] is None and r.json()["on_behalf_of_verified"] is None
    assert "on_behalf_of" not in (await audit_for(db, "alert", "resolve"))[0].details


async def test_valve_and_tool_on_behalf_of_by_service_account(client, db, world, publisher):
    flat = str(world.flats[0].id)
    r = await client.post(
        f"{API}/tools/valve-control",
        json={"flat_id": flat, "action": "close", "on_behalf_of": "telegram:7", "issued_by": "x"},
        headers=world.bot,
    )
    assert r.status_code == 200
    out = r.json()
    assert out["issued_by"] == "bot" and out["on_behalf_of"] == "telegram:7"
    assert out["on_behalf_of_verified"] is False
    row = (await audit_for(db, "valve", "valve_close_pending"))[0]
    assert row.actor == "bot" and row.details["on_behalf_of"] == "telegram:7"
    r = await client.post(
        f"{API}/valves/{world.flats[1].id}/open",
        json={"on_behalf_of": "telegram:8"},
        headers=world.bot,
    )
    assert r.json()["on_behalf_of"] == "telegram:8" and r.json()["issued_by"] == "bot"


async def test_on_behalf_of_audit_also_reaches_the_open_command_on_override(
    client, db, rt, world, publisher
):
    from tests.alert_helpers import LATE, WARN, escalate

    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    r = await client.post(
        f"{API}/alerts/{aid}/override",
        json={"reason": "owner called", "on_behalf_of": "telegram:9"},
        headers=world.bot,
    )
    assert r.status_code == 200
    rows = await audit_for(db, "valve", "valve_open_pending")
    assert rows[0].actor == "bot" and rows[0].details["on_behalf_of"] == "telegram:9"


async def test_on_behalf_of_validation(client, db, rt, world):
    aid = await new_alert(db, rt, world)
    too_long = await client.post(
        f"{API}/alerts/{aid}/acknowledge", json={"on_behalf_of": "x" * 101}, headers=world.bot
    )
    assert too_long.status_code == 422
    blank = await client.post(
        f"{API}/alerts/{aid}/resolve", json={"on_behalf_of": "   \n\t "}, headers=world.bot
    )
    assert blank.status_code == 200 and blank.json()["on_behalf_of"] is None  # sanitized to nothing


def test_sanitize_strips_control_characters_and_collapses_whitespace():
    assert sanitize_on_behalf_of("tg:1\r\nFAKE: admin\x00\x1b[31m") == "tg:1 FAKE: admin [31m"
    assert sanitize_on_behalf_of("  a \t b" + chr(0x2028) + "c ") == "a b c"
    assert sanitize_on_behalf_of("") is None and sanitize_on_behalf_of(None) is None
    assert len(sanitize_on_behalf_of("y" * 500) or "") == 100


async def test_sanitised_value_is_what_gets_stored(client, db, rt, world):
    aid = await new_alert(db, rt, world)
    r = await client.post(
        f"{API}/alerts/{aid}/acknowledge",
        json={"on_behalf_of": "line1\nline2\x00"},
        headers=world.bot,
    )
    assert r.json()["on_behalf_of"] == "line1 line2"
    assert (await audit_for(db, "alert", "acknowledge"))[0].details["on_behalf_of"] == "line1 line2"

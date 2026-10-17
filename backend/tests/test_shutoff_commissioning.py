"""Per-flat commissioning gate for AUTOMATIC valve closure."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import Flat
from app.repositories import AlertRepository, AuditRepository, FlatRepository, ValveRepository
from app.services import alert_engine as ae
from app.services import valve_control
from tests.conftest import T0, add_society, make_alert

H = timedelta(hours=1)


async def _uncommission(db, fid):
    async with db.session() as s:
        (await FlatRepository(s).get(fid)).auto_shutoff_validated = False


async def _escalate(db, rt, aid, now):
    async with db.session() as s:
        return await ae.escalate_alert(
            s, rt, await AlertRepository(s).get(aid), now, active_use=False
        )


def test_guard_is_last_and_named():
    kw = dict(
        alert=type(
            "A",
            (),
            dict(
                state="ticket",
                severity_score=0.95,
                shutoff_warned_at=None,
                created_at=T0,
                escalated_at=None,
                snoozed_until=None,
            ),
        )(),
        anomaly=type("N", (), dict(anomaly_type="leak", confidence=0.9))(),
    )
    now = T0 + 7 * H
    assert ae.auto_shutoff_block_reason(kw["alert"], kw["anomaly"], now, False) is None
    assert (
        ae.auto_shutoff_block_reason(kw["alert"], kw["anomaly"], now, False, commissioned=False)
        == "auto_shutoff_not_commissioned"
    )
    # any other blocker wins, so the notice only appears when the close would have run
    assert (
        ae.auto_shutoff_block_reason(kw["alert"], kw["anomaly"], now, True, commissioned=False)
        == "active_use"
    )


async def test_flats_default_to_not_commissioned(db):
    _, flats = await add_society(db, "plain", "Plain", ["1"], commissioned=False)
    async with db.session() as s:
        assert (await FlatRepository(s).get(flats[0].id)).auto_shutoff_validated is False
        assert Flat.__table__.c.auto_shutoff_validated.server_default is not None


async def test_uncommissioned_flat_never_auto_closes_and_notice_is_audited_once(
    db, rt, world, publisher
):
    fid = world.flats[0].id
    await _uncommission(db, fid)
    _, aid = await make_alert(db, rt, fid, severity=0.95, confidence=0.9)
    assert await _escalate(db, rt, aid, T0 + 7 * H)  # notice recorded
    assert not await _escalate(db, rt, aid, T0 + 8 * H)  # ... only once
    assert not await _escalate(db, rt, aid, T0 + 30 * H)
    async with db.session() as s:
        a = await AlertRepository(s).get(aid)
        cmds = await ValveRepository(s).recent(fid)
        audit = await AuditRepository(s).find(entity_type="alert")
    assert (a.state, a.tier) == ("ticket", 3) and a.shutoff_warned_at is None
    assert cmds == [] and not publisher.sent
    notices = [r for r in audit if r.action == "shutoff_not_commissioned"]
    assert len(notices) == 1
    assert "auto shut-off not commissioned for this flat" in notices[0].details["notice"]
    assert a.shutoff_uncommissioned_at is not None


async def test_valve_layer_refuses_auto_close_even_if_called_directly(db, rt, world, publisher):
    fid = world.flats[0].id
    await _uncommission(db, fid)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        with pytest.raises(valve_control.AutoShutoffNotCommissioned):
            await valve_control.send_valve_command(
                s, rt, flat, "close", source="auto", issued_by="system"
            )
    assert not publisher.sent


async def test_manual_close_is_unaffected(client, db, world, publisher):
    fid = world.flats[0].id
    await _uncommission(db, fid)
    r = await client.post(f"/api/v1/valves/{fid}/close", json={}, headers=world.admin)
    assert r.status_code in (200, 201, 202), r.text
    assert publisher.sent and publisher.sent[-1][1]["cmd"] == "close"


async def test_revoking_cancels_a_running_warning(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95, confidence=0.9)
    assert await _escalate(db, rt, aid, T0 + 6 * H)  # warning issued
    async with db.session() as s:
        assert (await AlertRepository(s).get(aid)).shutoff_warned_at is not None
    await _uncommission(db, fid)
    assert await _escalate(db, rt, aid, T0 + 6 * H + timedelta(seconds=301))
    async with db.session() as s:
        assert (await AlertRepository(s).get(aid)).shutoff_warned_at is None
    assert not publisher.sent


# --------------------------------------------------------------------------- endpoint
URL = "/api/v1/flats/{}/auto-shutoff-validation"


async def test_endpoint_is_admin_only_requires_note_and_is_audited(client, db, world):
    fid = world.flats[0].id
    body = {"validated": True, "note": "closed/opened 3x on site, flow stopped in 20 s"}
    for hdr in (world.maintenance, world.resident, {}):
        assert (await client.put(URL.format(fid), json=body, headers=hdr)).status_code in (401, 403)
    assert (
        await client.put(URL.format(fid), json={"validated": True}, headers=world.admin)
    ).status_code == 422
    assert (
        await client.put(URL.format(fid), json={"note": "  "}, headers=world.admin)
    ).status_code == 422
    r = await client.put(URL.format(fid), json=body, headers=world.admin)
    assert r.status_code == 200 and r.json()["auto_shutoff_validated"] is True
    d = (await client.get(f"/api/v1/flats/{fid}", headers=world.maintenance)).json()
    assert d["auto_shutoff_validated_by"] == "admin" and "flow stopped" in d["auto_shutoff_notes"]
    res = (await client.get(f"/api/v1/flats/{fid}", headers=world.resident)).json()
    assert res["auto_shutoff_validated"] is True and res["auto_shutoff_notes"] is None
    async with db.session() as s:
        audit = await AuditRepository(s).find(entity_type="flat")
    assert "auto_shutoff_commissioned" in {a.action for a in audit}
    r = await client.put(
        URL.format(fid), json={"validated": False, "note": "valve replaced"}, headers=world.admin
    )
    assert r.json()["auto_shutoff_validated"] is False
    async with db.session() as s:
        actions = {a.action for a in await AuditRepository(s).find(entity_type="flat")}
    assert "auto_shutoff_revoked" in actions


async def test_endpoint_404_and_not_patchable(client, world):
    import uuid

    ok = {"validated": True, "note": "tested"}
    assert (
        await client.put(URL.format(uuid.uuid4()), json=ok, headers=world.admin)
    ).status_code == 404
    r = await client.patch(
        f"/api/v1/flats/{world.flats[0].id}",
        json={"auto_shutoff_validated": True},
        headers=world.admin,
    )
    assert r.status_code == 422

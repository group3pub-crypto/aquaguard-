"""Alert lifecycle on real PostgreSQL (state machine, dedupe, escalation, DB triggers)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.models import Alert, Anomaly
from app.repositories import AlertRepository, FlatRepository, ValveRepository
from app.services import alert_engine
from app.services.alert_engine import InvalidTransition
from tests.conftest import T0, make_alert

pytestmark = pytest.mark.integration


async def _flat_id(db):
    async with db.session() as s:
        return (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id


async def _state(db, alert_id):
    async with db.session() as s:
        a = await s.get(Alert, alert_id)
        return a.state, a.tier


async def test_created_alert_dedupes_and_escalates(rt, pgdb, society):
    fid = await _flat_id(pgdb)
    _, alert_id = await make_alert(pgdb, rt, fid, severity=0.75, now=T0)  # tier 3 ticket
    assert await _state(pgdb, alert_id) == ("ticket", 3)
    a2 = await make_alert(pgdb, rt, fid, severity=0.8, now=T0 + timedelta(minutes=1))
    assert a2[1] == alert_id  # same flat + type: updated, not duplicated
    async with pgdb.session() as s:
        assert len(await AlertRepository(s).find(flat_id=fid)) == 1
    # a lower tier alert of another type escalates after its delay
    _, low = await make_alert(pgdb, rt, fid, severity=0.35, atype="overuse", now=T0)
    assert await _state(pgdb, low) == ("nudge", 1)
    async with pgdb.session() as s:
        assert await alert_engine.run_escalations(s, rt, T0 + timedelta(minutes=10)) == 0
    async with pgdb.session() as s:
        n = await alert_engine.run_escalations(s, rt, T0 + timedelta(minutes=31))
    assert n >= 1 and await _state(pgdb, low) == ("alert", 2)


async def test_manual_transitions_and_invalid_transition(rt, pgdb, society):
    fid = await _flat_id(pgdb)
    _, alert_id = await make_alert(pgdb, rt, fid, severity=0.6, now=T0)
    async with pgdb.session() as s:
        a = await s.get(Alert, alert_id)
        await alert_engine.apply_action(s, rt, a, "acknowledge", "admin", now=T0)
    assert await _state(pgdb, alert_id) == ("acknowledged", 2)
    async with pgdb.session() as s:
        a = await s.get(Alert, alert_id)
        await alert_engine.apply_action(s, rt, a, "resolve", "admin", now=T0)
        anomaly = await s.get(Anomaly, a.anomaly_id)
        assert anomaly.resolved_at is not None
    with pytest.raises(InvalidTransition):
        async with pgdb.session() as s:
            a = await s.get(Alert, alert_id)
            await alert_engine.apply_action(s, rt, a, "acknowledge", "admin", now=T0)
    # DB CHECK still protects against states the engine does not know
    with pytest.raises(sa.exc.IntegrityError, match="ck_alerts_state"):
        async with pgdb.session() as s:
            await s.execute(sa.text("UPDATE alerts SET state='bogus' WHERE id=:i"), {"i": alert_id})


async def test_override_of_auto_shutoff_reopens_valve_and_sets_cooldown(rt, pgdb, society):
    fid = await _flat_id(pgdb)
    _, alert_id = await make_alert(pgdb, rt, fid, severity=0.95, now=T0)
    async with pgdb.session() as s:
        a = await s.get(Alert, alert_id)
        a.state, a.tier = "auto_shutoff", 4
    async with pgdb.session() as s:
        a = await s.get(Alert, alert_id)
        await alert_engine.apply_action(s, rt, a, "override", "admin", now=T0, reason="false alarm")
    async with pgdb.session() as s:
        cmds = await ValveRepository(s).recent(fid)
    assert [(c.command, c.source) for c in cmds] == [("open", "manual_override")]
    assert rt.publisher.sent and rt.publisher.sent[-1][1]["cmd"] == "open"
    # inside the 24h cooldown the same anomaly type does not raise a new alert
    anomaly = Anomaly(
        flat_id=fid,
        detected_at=T0 + timedelta(hours=1),
        anomaly_type="leak",
        confidence=0.9,
        severity=0.95,
    )
    async with pgdb.session() as s:
        s.add(anomaly)
        await s.flush()
        flat = await FlatRepository(s).get(fid)
        assert (
            await alert_engine.process_anomaly(s, rt, flat, anomaly, T0 + timedelta(hours=1))
            is None
        )


async def test_alert_updated_at_is_maintained_by_db_trigger(rt, pgdb, society):
    fid = await _flat_id(pgdb)
    _, alert_id = await make_alert(pgdb, rt, fid, severity=0.6, now=T0)
    async with pgdb.session() as s:
        await s.execute(sa.text("UPDATE alerts SET updated_at = now() - interval '1 hour'"))
    async with pgdb.session() as s:
        before = (await s.execute(sa.text("SELECT updated_at FROM alerts"))).scalar_one()
        # raw SQL update that does not mention updated_at: only the trigger can move it
        await s.execute(sa.text("UPDATE alerts SET message='x' WHERE id=:i"), {"i": alert_id})
    async with pgdb.session() as s:
        after = (await s.execute(sa.text("SELECT updated_at FROM alerts"))).scalar_one()
    assert after > before

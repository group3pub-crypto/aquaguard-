"""Dedupe window, severity-driven re-escalation, and the learning-baseline tier cap."""

from datetime import timedelta

import pytest

from app.models import Anomaly, Flat
from app.repositories import AlertRepository, AuditRepository, FlatRepository
from app.services import alert_engine as ae
from tests.alert_helpers import H, escalate, get
from tests.conftest import T0, make_alert


async def raise_anomaly(db, rt, fid, atype, now, severity, *, window_end=None):
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        an = Anomaly(
            flat_id=fid,
            detected_at=now,
            anomaly_type=atype,
            confidence=0.9,
            severity=severity,
            est_loss_lpm=1.0,
            window_end=window_end or now,
        )
        s.add(an)
        await s.flush()
        alert = await ae.process_anomaly(s, rt, flat, an, now)
        return alert.id if alert else None


async def audit_rows(db, action):
    async with db.session() as s:
        return [
            r
            for r in await AuditRepository(s).find(entity_type="alert", limit=200)
            if r.action == action
        ]


# ------------------------------------------------------------------ 30 minute window
async def test_same_flat_and_type_within_30_minutes_is_one_alert(db, rt, world):
    fid = world.flats[0].id
    _, first = await make_alert(db, rt, fid, severity=0.6)
    for minutes in (1, 15, 29):
        again = await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=minutes), 0.6)
        assert again == first
    async with db.session() as s:
        assert len(await AlertRepository(s).find(flat_id=fid)) == 1


async def test_after_the_window_a_new_anomaly_gets_its_own_alert(db, rt, world):
    fid = world.flats[0].id
    _, first = await make_alert(db, rt, fid, severity=0.6)
    later = await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=31), 0.6)
    assert later not in (None, first)


async def test_ongoing_anomaly_keeps_deduping_beyond_30_minutes(db, rt, world):
    """The detector refreshes the SAME anomaly row every pass: still one alert after hours."""
    fid = world.flats[0].id
    an_id, first = await make_alert(db, rt, fid, severity=0.6)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        an = await s.get(Anomaly, an_id)
        an.window_end = T0 + 3 * H
        again = await ae.process_anomaly(s, rt, flat, an, T0 + 3 * H)
    assert again.id == first


async def test_dedupe_is_per_flat_and_per_type(db, rt, world):
    fid = world.flats[0].id
    _, first = await make_alert(db, rt, fid, severity=0.6)
    other_type = await raise_anomaly(db, rt, fid, "burst", T0 + timedelta(minutes=5), 0.6)
    other_flat = await raise_anomaly(
        db, rt, world.flats[1].id, "leak", T0 + timedelta(minutes=5), 0.6
    )
    assert len({first, other_type, other_flat}) == 3


async def test_resolved_alert_does_not_swallow_a_new_anomaly(db, rt, world):
    fid = world.flats[0].id
    _, first = await make_alert(db, rt, fid, severity=0.6)
    async with db.session() as s:
        await ae.apply_action(s, rt, await AlertRepository(s).get(first), "resolve", "bob", now=T0)
    assert await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=5), 0.6) != first


# ------------------------------------------------------------------ severity increase re-evaluates the tier
async def test_severity_increase_raises_the_tier_immediately(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.55)  # tier 2 'alert'
    assert (await get(db, aid)).tier == 2
    assert await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=10), 0.75) == aid
    a = await get(db, aid)
    assert (a.state, a.tier, a.severity_score) == ("ticket", 3, 0.75)
    assert "ticket" in a.message and a.escalated_at
    rows = await audit_rows(db, "escalated")
    assert (
        rows[0].details["reason"] == "severity_increase"
        and rows[0].details["previous_state"] == "alert"
    )


async def test_severity_increase_never_jumps_straight_to_auto_shutoff(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=10), 0.99)
    assert (await get(db, aid)).tier == 3


async def test_severity_decrease_or_same_tier_does_not_change_the_tier(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.75)  # tier 3
    await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=5), 0.55)
    a = await get(db, aid)
    assert (a.tier, a.severity_score) == (3, 0.55)  # severity tracks reality, tier is not lowered
    assert await audit_rows(db, "escalated") == []
    await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=6), 0.72)  # up, same tier
    assert (await get(db, aid)).tier == 3 and await audit_rows(db, "escalated") == []


async def test_acknowledged_alert_escalates_again_only_if_severity_increases(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    async with db.session() as s:
        await ae.apply_action(
            s, rt, await AlertRepository(s).get(aid), "acknowledge", "bob", now=T0
        )
    # same severity / lower severity / higher but same tier: it stays acknowledged
    for sev in (0.55, 0.5, 0.65):
        await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=5), sev)
        assert (await get(db, aid)).state == "acknowledged"
    # a real increase that reaches a higher tier re-opens it
    await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=10), 0.8)
    a = await get(db, aid)
    assert (a.state, a.tier) == ("ticket", 3)


async def test_snoozed_alert_is_reopened_by_a_severity_increase(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    async with db.session() as s:
        await ae.apply_action(
            s, rt, await AlertRepository(s).get(aid), "snooze", "bob", now=T0, minutes=120
        )
    await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=10), 0.8)
    a = await get(db, aid)
    assert a.state == "ticket" and a.snoozed_until is None


async def test_closed_alerts_are_never_reopened_by_dedupe(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    async with db.session() as s:
        await ae.apply_action(s, rt, await AlertRepository(s).get(aid), "resolve", "bob", now=T0)
    new = await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=2), 0.9)
    assert new != aid and (await get(db, aid)).state == "resolved"


# ------------------------------------------------------------------ learning baseline cap
async def set_status(db, fid, status):
    async with db.session() as s:
        (await s.get(Flat, fid)).baseline_status = status


async def test_new_alert_on_a_learning_flat_is_capped_at_alert_and_says_so(db, rt, world):
    fid = world.flats[0].id
    await set_status(db, fid, "learning")
    _, aid = await make_alert(db, rt, fid, severity=0.99)
    a = await get(db, aid)
    assert (a.state, a.tier) == ("alert", 2)
    assert ae.LEARNING_NOTE in a.message and "still being learned" in a.message
    created = (await audit_rows(db, "created"))[0]
    assert created.details["learning_cap"] is True and created.details["uncapped_tier"] == 3


async def test_below_the_cap_no_note_is_added(db, rt, world):
    fid = world.flats[0].id
    await set_status(db, fid, "learning")
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    a = await get(db, aid)
    assert a.tier == 2 and ae.LEARNING_NOTE not in a.message
    assert "learning_cap" not in (await audit_rows(db, "created"))[0].details


async def test_time_based_escalation_stops_at_alert_while_learning(db, rt, world, publisher):
    fid = world.flats[0].id
    await set_status(db, fid, "learning")
    _, aid = await make_alert(db, rt, fid, severity=0.35)  # nudge
    assert await escalate(db, rt, aid, T0 + timedelta(minutes=31))
    assert (await get(db, aid)).tier == 2
    # the next due escalation is capped: recorded once, then silent, never above 'alert'
    assert await escalate(db, rt, aid, T0 + 3 * H)
    a = await get(db, aid)
    assert (a.state, a.tier) == ("alert", 2) and ae.LEARNING_NOTE in a.message
    assert len(await audit_rows(db, "escalation_capped")) == 1
    for hours in (5, 24, 100):
        assert not await escalate(db, rt, aid, T0 + hours * H)
    assert (await get(db, aid)).tier == 2 and not publisher.sent
    assert len(await audit_rows(db, "escalation_capped")) == 1


async def test_escalation_resumes_once_the_baseline_is_ready(db, rt, world):
    fid = world.flats[0].id
    await set_status(db, fid, "learning")
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    assert await escalate(db, rt, aid, T0 + 3 * H)  # capped note
    assert (await get(db, aid)).tier == 2
    await set_status(db, fid, "ready")
    assert await escalate(db, rt, aid, T0 + 3 * H)
    assert (await get(db, aid)).tier == 3


async def test_learning_flat_can_never_be_shut_off_automatically(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)  # ready flat -> ticket
    await set_status(db, fid, "learning")  # e.g. data gap forced a re-learn
    assert not await escalate(db, rt, aid, T0 + 8 * H, active_use=False)
    assert not publisher.sent and (await get(db, aid)).shutoff_warned_at is None


async def test_severity_increase_on_a_learning_flat_is_capped(db, rt, world):
    fid = world.flats[0].id
    await set_status(db, fid, "learning")
    _, aid = await make_alert(db, rt, fid, severity=0.55)
    await raise_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=5), 0.95)
    assert (await get(db, aid)).tier == 2


def test_block_reason_for_learning_baseline_comes_first():
    from types import SimpleNamespace

    alert = SimpleNamespace(
        state="ticket", severity_score=0.95, created_at=T0, escalated_at=[], snoozed_until=None
    )
    anomaly = SimpleNamespace(anomaly_type="leak", confidence=0.9)
    now = T0 + 7 * H
    assert (
        ae.auto_shutoff_block_reason(alert, anomaly, now, False, "learning") == "baseline_learning"
    )
    assert ae.auto_shutoff_block_reason(alert, anomaly, now, False, "ready") is None
    assert ae.auto_shutoff_block_reason(alert, anomaly, now, False, "stale") is None


@pytest.mark.parametrize("sev", [0.35, 0.6, 0.8, 0.99])
async def test_new_alerts_never_start_above_tier_3(db, rt, world, sev):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=sev)
    assert (await get(db, aid)).tier <= 3

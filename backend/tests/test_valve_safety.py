"""Auto shut-off safety protocol: warning window, commit-then-publish, ack, retry-once, give up."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ValveCommand
from app.repositories import AlertRepository, AuditRepository, ValveRepository
from app.services import alert_engine as ae
from app.services import maintenance, valve_control
from tests.alert_helpers import LATE, WARN, H, ack, add_readings, escalate, get
from tests.conftest import T0, make_alert


@pytest.fixture
def captured(rt, monkeypatch):
    sent = []
    real = rt.ws.broadcast

    async def capture(message, key=None):
        sent.append(message)
        return await real(message, key)

    monkeypatch.setattr(rt.ws, "broadcast", capture)
    return sent


async def cmds(db, flat_id):
    async with db.session() as s:
        return list(reversed(await ValveRepository(s).recent(flat_id, 20)))


async def alert_audit(db):
    async with db.session() as s:
        return [r.action for r in await AuditRepository(s).find(entity_type="alert", limit=200)]


# ------------------------------------------------------------------ 1. failed close is not a shut-off
async def test_publish_failure_keeps_alert_at_ticket_and_never_claims_shutoff(
    db, rt, world, publisher, captured
):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    publisher.fail = True
    assert await escalate(db, rt, aid, LATE, active_use=False)  # warning
    assert await escalate(db, rt, aid, LATE + WARN, active_use=False)  # attempt 1 -> publish fails
    a = await get(db, aid)
    assert (a.state, a.tier) == ("ticket", 3) and "shut off" not in a.message
    c = await cmds(db, fid)
    assert [(x.command, x.status) for x in c] == [("close", "failed")]
    assert "valve_close_failed" in {r.action for r in await _valve_audit(db)}
    assert not any(m.get("event") == "auto_shutoff" for m in captured)
    assert all("shut off" not in (m.get("alert") or {}).get("message", "") for m in captured)


async def _valve_audit(db):
    async with db.session() as s:
        return await AuditRepository(s).find(entity_type="valve", limit=100)


async def test_failed_close_is_retried_exactly_once_then_staff_are_notified(
    db, rt, world, publisher, captured
):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    publisher.fail = True
    t = LATE
    await escalate(db, rt, aid, t, active_use=False)  # warning
    t += WARN
    await escalate(db, rt, aid, t, active_use=False)  # attempt 1 (failed)
    await escalate(db, rt, aid, t + timedelta(seconds=60), active_use=False)  # retry (failed)
    assert len(await cmds(db, fid)) == 2
    assert await escalate(db, rt, aid, t + timedelta(seconds=120), active_use=False)  # give up
    a = await get(db, aid)
    assert a.state == "ticket" and a.shutoff_gave_up_at is not None
    # never a third attempt, no matter how long we wait
    for k in range(3, 8):
        assert not await escalate(db, rt, aid, t + timedelta(seconds=60 * k), active_use=False)
    assert len(await cmds(db, fid)) == 2
    actions = await alert_audit(db)
    assert actions.count("shutoff_attempt") == 2 and actions.count("shutoff_failed") == 1
    failed = [m for m in captured if m.get("event") == "shutoff_failed"]
    assert len(failed) == 1 and failed[0]["type"] == "alert"
    async with db.session() as s:
        row = next(
            r
            for r in await AuditRepository(s).find(entity_type="alert")
            if r.action == "shutoff_failed"
        )
    assert row.details["attempts"] == 2 and "manually" in row.details["action_required"]


async def test_retry_after_failure_can_succeed_and_then_confirms(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    publisher.fail = True
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)  # fails
    publisher.fail = False
    await escalate(db, rt, aid, LATE + WARN + timedelta(seconds=60), active_use=False)  # retry ok
    assert (await get(db, aid)).state == "ticket"  # still waiting for the device
    short = publisher.sent[-1][1]["id"]
    await ack(db, rt, fid, short, "closed", LATE + WARN + timedelta(seconds=70))
    a = await get(db, aid)
    assert (a.state, a.tier) == ("auto_shutoff", 4) and "shut off" in a.message
    assert [c.status for c in await cmds(db, fid)] == ["failed", "confirmed"]


# ------------------------------------------------------------------ ack timeout
async def expire(db, now, timeout_s=60):
    async with db.session() as s:
        return await valve_control.expire_pending(s, now, timeout_s=timeout_s)


async def test_ack_timeout_marks_timeout_retries_once_then_gives_up(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    t1 = LATE + WARN
    await escalate(db, rt, aid, t1, active_use=False)  # attempt 1 published, no ack coming
    assert (await get(db, aid)).state == "ticket"
    assert not await escalate(db, rt, aid, t1 + timedelta(seconds=30), active_use=False)  # waiting
    assert await expire(db, t1 + timedelta(seconds=61)) == 1
    assert [c.status for c in await cmds(db, fid)] == ["timeout"]
    t2 = t1 + timedelta(seconds=62)
    assert await escalate(db, rt, aid, t2, active_use=False)  # the single retry
    assert len(publisher.sent) == 2
    assert await expire(db, t2 + timedelta(seconds=61)) == 1
    assert await escalate(db, rt, aid, t2 + timedelta(seconds=62), active_use=False)  # give up
    a = await get(db, aid)
    assert a.state == "ticket" and a.shutoff_gave_up_at is not None
    assert not await escalate(db, rt, aid, t2 + timedelta(hours=1), active_use=False)
    assert len(publisher.sent) == 2
    assert "valve_close_timeout" in {r.action for r in await _valve_audit(db)}


async def test_late_ack_of_a_timed_out_close_still_advances_the_alert(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    short = publisher.sent[-1][1]["id"]
    await expire(db, LATE + WARN + timedelta(seconds=90))
    match = await ack(db, rt, fid, short, "closed", LATE + WARN + timedelta(seconds=100))
    assert match is not None and match.status == "confirmed"  # the valve DID close
    assert (await get(db, aid)).state == "auto_shutoff"
    async with db.session() as s:
        ev = [
            r
            for r in await AuditRepository(s).find(entity_type="valve")
            if r.action == "valve_state_ack"
        ]
    assert ev[0].details["late"] is True and ev[0].details["confirmed"] is True


async def test_ack_with_the_wrong_state_does_not_confirm_a_close(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    await ack(db, rt, fid, publisher.sent[-1][1]["id"], "open", LATE + WARN)
    assert (await get(db, aid)).state == "ticket"
    assert [c.status for c in await cmds(db, fid)] == ["failed"]


async def test_ack_for_unknown_command_id_changes_nothing(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    assert await ack(db, rt, fid, "deadbeef", "closed", LATE + WARN) is None
    assert (await get(db, aid)).state == "ticket"


async def test_retry_ack_after_confirmation_is_idempotent(db, rt, world, publisher, captured):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    short = publisher.sent[-1][1]["id"]
    await ack(db, rt, fid, short, "closed", LATE + WARN)
    await ack(db, rt, fid, short, "closed", LATE + WARN)  # duplicate delivery
    assert [m["event"] for m in captured if m.get("event") == "auto_shutoff"] == ["auto_shutoff"]


# ------------------------------------------------------------------ 2. warning window
async def test_warning_is_issued_first_and_the_close_waits_for_the_window(
    db, rt, world, publisher, captured
):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    assert await escalate(db, rt, aid, LATE, active_use=False)
    a = await get(db, aid)
    assert a.shutoff_warned_at == LATE and a.state == "ticket" and not publisher.sent
    warn = [m for m in captured if m.get("event") == "shutoff_warning"]
    assert len(warn) == 1 and warn[0]["type"] == "alert"
    async with db.session() as s:
        row = next(
            r
            for r in await AuditRepository(s).find(entity_type="alert")
            if r.action == "shutoff_warning"
        )
    assert row.details["warning_seconds"] == 300 and "executes_at" in row.details
    # nothing happens inside the window ...
    for secs in (1, 120, 299):
        assert not await escalate(db, rt, aid, LATE + timedelta(seconds=secs), active_use=False)
    assert not publisher.sent
    # ... and the close goes out once it elapsed
    assert await escalate(db, rt, aid, LATE + WARN, active_use=False)
    assert len(publisher.sent) == 1 and publisher.sent[0][1]["cmd"] == "close"


async def test_warning_window_length_is_configurable(db, rt, world, publisher):
    rt.settings.shutoff_warning_seconds = 60
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    assert not await escalate(db, rt, aid, LATE + timedelta(seconds=59), active_use=False)
    assert await escalate(db, rt, aid, LATE + timedelta(seconds=60), active_use=False)
    assert publisher.sent


@pytest.mark.parametrize("action", ["acknowledge", "snooze", "override", "resolve"])
async def test_human_action_during_the_window_cancels_the_shutoff(
    db, rt, world, publisher, captured, action
):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    async with db.session() as s:
        await ae.apply_action(
            s,
            rt,
            await AlertRepository(s).get(aid),
            action,
            "bob",
            now=LATE + timedelta(seconds=30),
            minutes=60,
            reason="r",
        )
    a = await get(db, aid)
    assert a.shutoff_warned_at is None
    assert "shutoff_warning_cancelled" in await alert_audit(db)
    assert any(m.get("event") == "shutoff_warning_cancelled" for m in captured)
    assert not await escalate(db, rt, aid, LATE + WARN + timedelta(seconds=1), active_use=False)
    assert not publisher.sent  # no command was ever issued
    assert await cmds(db, fid) == []


async def test_guard_that_stops_holding_cancels_the_warning(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    # somebody starts using water during the window
    assert await escalate(db, rt, aid, LATE + WARN, active_use=True)
    assert (await get(db, aid)).shutoff_warned_at is None and not publisher.sent
    assert "shutoff_warning_cancelled" in await alert_audit(db)
    # when everything is clear again a NEW warning starts (the old one does not carry over)
    assert await escalate(db, rt, aid, LATE + 2 * WARN, active_use=False)
    assert (await get(db, aid)).shutoff_warned_at == LATE + 2 * WARN and not publisher.sent


async def test_anomaly_resolved_during_window_cancels_the_warning(db, rt, world, publisher):
    from app.repositories import AnomalyRepository

    fid = world.flats[0].id
    an, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    async with db.session() as s:
        (await AnomalyRepository(s).get(an)).resolved_at = LATE + timedelta(seconds=10)
    assert await escalate(db, rt, aid, LATE + timedelta(seconds=20), active_use=False)
    assert (await get(db, aid)).shutoff_warned_at is None and not publisher.sent


# ------------------------------------------------------------------ 3. commit-then-publish
async def test_command_row_is_committed_before_the_publish_happens(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    seen = []

    async def spying_publish(topic, payload):
        async with db.session() as s:  # a DIFFERENT transaction must already see the row
            row = (await s.execute(select(ValveCommand))).scalars().all()
            seen.append([(r.status, r.source, r.alert_id) for r in row])
        publisher.sent.append((topic, payload))

    rt.publisher.publish = spying_publish
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    assert seen == [[("pending", "auto", aid)]]


async def test_a_later_rollback_cannot_erase_a_command_that_was_sent(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    with pytest.raises(RuntimeError):
        async with db.session() as s:
            alert = await AlertRepository(s).get(aid)
            await ae.escalate_alert(s, rt, alert, LATE + WARN, active_use=False)
            raise RuntimeError("something else in the same maintenance pass blew up")
    assert publisher.sent and len(await cmds(db, fid)) == 1  # the record of what we sent survives


async def test_manual_valve_command_is_also_persisted_before_publishing(db, rt, world, publisher):
    flat = world.flats[0]
    seen = []

    async def spy(topic, payload):
        async with db.session() as s:
            seen.append(len((await s.execute(select(ValveCommand))).scalars().all()))

    rt.publisher.publish = spy
    async with db.session() as s:
        from app.repositories import FlatRepository

        f = await FlatRepository(s).get(flat.id)
        await valve_control.send_valve_command(
            s, rt, f, "close", source="manual_dashboard", issued_by="a"
        )
    assert seen == [1]


async def test_one_alert_failing_does_not_roll_back_the_others(db, rt, world, monkeypatch):
    ids = [(await make_alert(db, rt, f.id, severity=0.35))[1] for f in world.flats[:3]]
    real = ae.escalate_alert
    calls = []

    async def flaky(session, rt_, alert, now, *a, **kw):
        calls.append(alert.id)
        if len(calls) == 2:
            alert.state = "garbage-never-persisted"
            raise RuntimeError("boom")
        return await real(session, rt_, alert, now, *a, **kw)

    monkeypatch.setattr(ae, "escalate_alert", flaky)
    async with db.session() as s:
        changed = await ae.run_escalations(s, rt, T0 + timedelta(minutes=45))
    assert changed == 2 and len(calls) == 3
    states = {i: (await get(db, i)).state for i in ids}
    assert sorted(states.values()) == ["alert", "alert", "nudge"]  # failed one untouched


async def test_run_escalations_pages_through_every_alert_without_a_cap(db, rt, world):
    ids = []
    for _ in range(5):
        for f in world.flats:
            ids.append((await make_alert(db, rt, f.id, severity=0.35, atype=_type(len(ids))))[1])
    assert len(ids) == 30
    async with db.session() as s:
        assert await ae.run_escalations(s, rt, T0 + timedelta(minutes=45), page=4) == 30
    assert {(await get(db, i)).state for i in ids} == {"alert"}


def _type(n):
    return ("leak", "burst", "overuse", "dripping_tap", "running_toilet")[n // 6]


async def test_candidate_query_ignores_alerts_that_cannot_be_due(db, rt, world):
    for f in world.flats[:3]:
        await make_alert(db, rt, f.id, severity=0.35)
    async with db.session() as s:
        repo = AlertRepository(s)
        assert await repo.escalation_candidate_ids(T0 + timedelta(minutes=10)) == []
        assert len(await repo.escalation_candidate_ids(T0 + timedelta(minutes=31))) == 3
        first = await repo.escalation_candidate_ids(T0 + H, limit=2)
        rest = await repo.escalation_candidate_ids(T0 + H, after=first[-1], limit=2)
        assert len(first) == 2 and len(rest) == 1 and rest[0] > first[-1]


async def test_maintenance_cycle_expires_commands_then_escalates(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await add_readings(db, fid, [1.2] * 18, LATE)
    out = await maintenance.run_cycle(rt, LATE)
    assert out["escalations"] == 1 and (await get(db, aid)).shutoff_warned_at is not None
    out = await maintenance.run_cycle(rt, LATE + WARN)
    assert out["escalations"] == 1 and len(publisher.sent) == 1
    # 70 s later the missing ack times out and the next cycle retries (the readings are stale
    # by then, which would block a fresh shut-off decision: refresh them)
    await add_readings(db, fid, [1.2] * 18, LATE + WARN + timedelta(seconds=75))
    out = await maintenance.run_cycle(rt, LATE + WARN + timedelta(seconds=75))
    assert out["valve_timeouts"] == 1 and len(publisher.sent) == 2


async def test_override_after_a_pending_close_sends_open(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)  # close in flight, no ack yet
    async with db.session() as s:
        await ae.apply_action(
            s,
            rt,
            await AlertRepository(s).get(aid),
            "override",
            "boss",
            now=LATE + WARN + timedelta(seconds=5),
            reason="false alarm",
        )
    assert [p["cmd"] for _, p in publisher.sent] == ["close", "open"]


async def test_close_confirmed_after_alert_was_resolved_is_flagged(
    db, rt, world, publisher, captured
):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    await escalate(db, rt, aid, LATE, active_use=False)
    await escalate(db, rt, aid, LATE + WARN, active_use=False)
    short = publisher.sent[-1][1]["id"]
    async with db.session() as s:
        await ae.apply_action(
            s, rt, await AlertRepository(s).get(aid), "resolve", "bob", now=LATE + WARN
        )
    await ack(db, rt, fid, short, "closed", LATE + WARN + timedelta(seconds=3))
    a = await get(db, aid)
    assert a.state == "resolved"
    assert "shutoff_confirmed_after_close" in await alert_audit(db)

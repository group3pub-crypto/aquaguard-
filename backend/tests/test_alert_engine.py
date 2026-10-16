from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models import Anomaly, FlowReading
from app.repositories import (
    AlertRepository,
    AnomalyRepository,
    AuditRepository,
    FlatRepository,
    ValveRepository,
)
from app.services import alert_engine as ae
from tests.conftest import T0, make_alert

H = timedelta(hours=1)


@pytest.mark.parametrize(
    ("sev", "tier"),
    [(0.0, 0), (0.29, 0), (0.3, 1), (0.49, 1), (0.5, 2), (0.7, 3), (0.89, 3), (0.9, 4), (1.0, 4)],
)
def test_tier_thresholds(sev, tier):
    assert ae.tier_for(sev) == tier


async def get(db, alert_id):
    async with db.session() as s:
        return await AlertRepository(s).get(alert_id)


async def escalate(db, rt, alert_id, now, **kw):
    async with db.session() as s:
        alert = await AlertRepository(s).get(alert_id)
        return await ae.escalate_alert(s, rt, alert, now, **kw)


async def test_initial_tier_is_capped_below_auto_shutoff(db, rt, world):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.99)
    a = await get(db, aid)
    assert (
        (a.state, a.tier) == ("ticket", 3)
        and a.message_source == "template"
        and "Flat 101" in a.message
    )


async def test_below_threshold_creates_no_alert(db, rt, world):
    async with db.session() as s:
        an = Anomaly(
            flat_id=world.flats[0].id,
            detected_at=T0,
            anomaly_type="leak",
            confidence=0.5,
            severity=0.2,
        )
        s.add(an)
        await s.flush()
        flat = await FlatRepository(s).get(world.flats[0].id)
        assert await ae.process_anomaly(s, rt, flat, an, T0) is None


async def test_escalation_timeline_with_time_travel(db, rt, world):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.35)
    assert (await get(db, aid)).state == "nudge"
    assert not await escalate(db, rt, aid, T0 + timedelta(minutes=29))
    assert await escalate(db, rt, aid, T0 + timedelta(minutes=31))
    a = await get(db, aid)
    assert (a.state, a.tier, len(a.escalated_at)) == ("alert", 2, 1)
    t1 = T0 + timedelta(minutes=31)
    assert not await escalate(db, rt, aid, t1 + timedelta(hours=1, minutes=59))
    assert await escalate(db, rt, aid, t1 + 2 * H)
    a = await get(db, aid)
    assert (a.state, a.tier) == ("ticket", 3) and "ticket" in a.message


async def test_acknowledged_resolved_and_overridden_alerts_do_not_escalate(db, rt, world):
    for action, atype in (
        ("acknowledge", "leak"),
        ("resolve", "overuse"),
        ("override", "dripping_tap"),
    ):
        _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.35, atype=atype)
        async with db.session() as s:
            await ae.apply_action(
                s, rt, await AlertRepository(s).get(aid), action, "bob", now=T0, reason="r"
            )
        assert not await escalate(db, rt, aid, T0 + 10 * H)
        assert (await get(db, aid)).state != "nudge"


async def test_escalation_stops_when_anomaly_resolved(db, rt, world):
    anomaly_id, aid = await make_alert(db, rt, world.flats[0].id, severity=0.35)
    async with db.session() as s:
        (await AnomalyRepository(s).get(anomaly_id)).resolved_at = T0 + timedelta(minutes=5)
    assert not await escalate(db, rt, aid, T0 + 3 * H)


async def test_snooze_pauses_then_resumes_and_restarts_the_clock(db, rt, world):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.35)
    async with db.session() as s:
        await ae.apply_action(
            s, rt, await AlertRepository(s).get(aid), "snooze", "bob", now=T0, minutes=60
        )
    assert (await get(db, aid)).state == "snoozed"
    assert not await escalate(db, rt, aid, T0 + timedelta(minutes=59))
    assert await escalate(db, rt, aid, T0 + H)  # snooze expiry -> back to nudge
    assert (await get(db, aid)).state == "nudge"
    assert not await escalate(db, rt, aid, T0 + H + timedelta(minutes=29))  # 30m counts from expiry
    assert await escalate(db, rt, aid, T0 + H + timedelta(minutes=31))


async def test_run_escalations_processes_all_active_alerts(db, rt, world):
    ids = [(await make_alert(db, rt, f.id, severity=0.35))[1] for f in world.flats[:3]]
    async with db.session() as s:
        assert await ae.run_escalations(s, rt, T0 + timedelta(minutes=45)) == 3
    assert {(await get(db, i)).state for i in ids} == {"alert"}


# ---------------------------------------------------------------- auto shut-off guards
def guard_args(**over):
    alert = SimpleNamespace(
        state="ticket", severity_score=0.95, created_at=T0, escalated_at=[], snoozed_until=None
    )
    anomaly = SimpleNamespace(anomaly_type="leak", confidence=0.9)
    now, active = T0 + 7 * H, False
    for k, v in over.items():
        if k in ("state", "severity_score", "created_at", "escalated_at"):
            setattr(alert, k, v)
        elif k in ("anomaly_type", "confidence"):
            setattr(anomaly, k, v)
        elif k == "now":
            now = v
        elif k == "active_use":
            active = v
    return alert, anomaly, now, active


def test_all_guards_satisfied_allows_shutoff():
    assert ae.auto_shutoff_block_reason(*guard_args()) is None
    assert (
        ae.auto_shutoff_block_reason(
            *guard_args(anomaly_type="burst", confidence=0.85, severity_score=0.9)
        )
        is None
    )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"anomaly_type": "overuse"}, "type_not_eligible"),
        ({"anomaly_type": "running_toilet"}, "type_not_eligible"),
        ({"confidence": 0.84}, "low_confidence"),
        ({"severity_score": 0.89}, "low_severity"),
        ({"now": T0 + timedelta(hours=5, minutes=59)}, "not_persisted_6h_at_ticket"),
        ({"state": "alert"}, "not_persisted_6h_at_ticket"),
        ({"escalated_at": [(T0 + 3 * H).isoformat()]}, "not_persisted_6h_at_ticket"),
        ({"active_use": True}, "active_use"),
        ({"active_use": None}, "no_recent_data"),
    ],
)
def test_each_guard_blocks_individually(override, reason):
    assert ae.auto_shutoff_block_reason(*guard_args(**override)) == reason


async def test_auto_shutoff_end_to_end_closes_valve_and_audits(db, rt, world, publisher):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.95, confidence=0.9)
    assert not await escalate(db, rt, aid, T0 + timedelta(hours=5, minutes=59), active_use=False)
    assert await escalate(db, rt, aid, T0 + 6 * H, active_use=False)
    a = await get(db, aid)
    assert (a.state, a.tier) == ("auto_shutoff", 4)
    topic, payload = publisher.sent[-1]
    assert topic == "aquaguard/green-valley/101/valve/cmd"
    assert payload["cmd"] == "close" and payload["src"] == "auto" and payload["v"] == 1
    async with db.session() as s:
        cmds = await ValveRepository(s).recent(world.flats[0].id)
        audit = await AuditRepository(s).find(entity_type="alert")
        valve_audit = await AuditRepository(s).find(entity_type="valve")
    assert [(c.command, c.source, c.status, c.alert_id) for c in cmds] == [
        ("close", "auto", "pending", aid)
    ]
    assert (
        "auto_shutoff" in {r.action for r in audit}
        and valve_audit[0].action == "valve_close_pending"
    )


@pytest.mark.parametrize(
    "kwargs",
    [dict(atype="overuse"), dict(confidence=0.8), dict(severity=0.88)],
    ids=["type", "confidence", "severity"],
)
async def test_auto_shutoff_blocked_end_to_end(db, rt, world, publisher, kwargs):
    _, aid = await make_alert(db, rt, world.flats[0].id, **{"severity": 0.95, **kwargs})
    assert not await escalate(db, rt, aid, T0 + 8 * H, active_use=False)
    assert (await get(db, aid)).state == "ticket" and not publisher.sent


async def add_readings(db, fid, flows, end):
    async with db.session() as s:
        for i, f in enumerate(flows):
            s.add(
                FlowReading(
                    flat_id=fid,
                    timestamp=end - timedelta(seconds=30 * (len(flows) - i)),
                    flow_rate=f,
                    volume=0.1,
                    message_id=f"{i:08x}",
                )
            )


async def test_shutoff_requires_recent_data_and_no_active_use(db, rt, world, publisher):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.95)
    late = T0 + 6 * H
    assert not await escalate(db, rt, aid, late)  # no readings at all -> cannot verify -> block
    await add_readings(db, fid, [1.0, 0.0, 2.5, 0.0, 1.2, 0.0] * 3, late)  # taps opening/closing
    assert not await escalate(db, rt, aid, late)
    assert (await get(db, aid)).state == "ticket"
    async with db.session() as s:
        from sqlalchemy import delete

        await s.execute(delete(FlowReading))
    await add_readings(db, fid, [1.2] * 18, late)  # steady flow, never stops
    assert await escalate(db, rt, aid, late)
    assert (await get(db, aid)).state == "auto_shutoff" and publisher.sent


async def test_shutoff_command_failure_is_recorded_not_raised(db, rt, world, publisher):
    publisher.fail = True
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.95)
    assert await escalate(db, rt, aid, T0 + 6 * H, active_use=False)
    async with db.session() as s:
        cmd = (await ValveRepository(s).recent(world.flats[0].id))[0]
    assert cmd.status == "failed"


# ---------------------------------------------------------------- dedupe, override, transitions
async def new_anomaly(db, rt, fid, atype, now, severity=0.95):
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        an = Anomaly(
            flat_id=fid,
            detected_at=now,
            anomaly_type=atype,
            confidence=0.9,
            severity=severity,
            est_loss_lpm=1.0,
        )
        s.add(an)
        await s.flush()
        alert = await ae.process_anomaly(s, rt, flat, an, now)
        return alert.id if alert else None


async def test_same_flat_and_type_is_deduplicated(db, rt, world):
    fid = world.flats[0].id
    _, first = await make_alert(db, rt, fid, severity=0.6)
    again = await new_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=20), severity=0.8)
    assert again == first
    other_type = await new_anomaly(db, rt, fid, "burst", T0 + timedelta(minutes=20))
    other_flat = await new_anomaly(db, rt, world.flats[1].id, "leak", T0 + timedelta(minutes=20))
    assert len({first, other_type, other_flat}) == 3
    async with db.session() as s:
        assert len(await AlertRepository(s).find(flat_id=fid)) == 2
    assert (await get(db, first)).severity_score == 0.8  # severity refreshed


async def test_new_alert_allowed_after_previous_resolved(db, rt, world):
    fid = world.flats[0].id
    _, first = await make_alert(db, rt, fid)
    async with db.session() as s:
        await ae.apply_action(s, rt, await AlertRepository(s).get(first), "resolve", "bob", now=T0)
    assert await new_anomaly(db, rt, fid, "leak", T0 + timedelta(minutes=5)) not in (None, first)


async def test_override_sets_24h_cooldown_for_that_flat_and_type(db, rt, world):
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid)
    async with db.session() as s:
        await ae.apply_action(
            s,
            rt,
            await AlertRepository(s).get(aid),
            "override",
            "boss",
            now=T0,
            reason="filling pool",
        )
    a = await get(db, aid)
    assert a.state == "overridden" and a.snoozed_until == T0 + 24 * H
    assert await new_anomaly(db, rt, fid, "leak", T0 + 23 * H) is None
    assert await new_anomaly(db, rt, fid, "burst", T0 + 23 * H) is not None  # other type unaffected
    assert await new_anomaly(db, rt, world.flats[1].id, "leak", T0 + 23 * H) is not None
    assert await new_anomaly(db, rt, fid, "leak", T0 + 24 * H + timedelta(minutes=1)) is not None


async def test_override_of_auto_shutoff_reopens_valve(db, rt, world, publisher):
    _, aid = await make_alert(db, rt, world.flats[0].id)
    assert await escalate(db, rt, aid, T0 + 6 * H, active_use=False)
    async with db.session() as s:
        await ae.apply_action(
            s,
            rt,
            await AlertRepository(s).get(aid),
            "override",
            "boss",
            now=T0 + 7 * H,
            reason="false alarm",
        )
    _, payload = publisher.sent[-1]
    assert payload["cmd"] == "open" and payload["src"] == "manual_override"


@pytest.mark.parametrize(
    ("setup", "action"),
    [
        ("resolve", "resolve"),
        ("resolve", "acknowledge"),
        ("resolve", "snooze"),
        ("override", "resolve"),
        ("acknowledge", "acknowledge"),
    ],
)
async def test_invalid_transitions_raise(db, rt, world, setup, action):
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.4)
    async with db.session() as s:
        await ae.apply_action(
            s, rt, await AlertRepository(s).get(aid), setup, "bob", now=T0, reason="r", minutes=15
        )
    with pytest.raises(ae.InvalidTransition):
        async with db.session() as s:
            await ae.apply_action(s, rt, await AlertRepository(s).get(aid), action, "bob", now=T0)


async def test_every_state_change_is_audited_and_broadcast(db, rt, world, monkeypatch):
    sent = []

    async def capture(message, key=None):
        sent.append((message, key))
        return 0

    monkeypatch.setattr(rt.ws, "broadcast", capture)
    fid = world.flats[0].id
    _, aid = await make_alert(db, rt, fid, severity=0.35)
    for action in ("acknowledge", "snooze", "resolve"):
        async with db.session() as s:
            await ae.apply_action(
                s, rt, await AlertRepository(s).get(aid), action, "bob", now=T0, minutes=30
            )
    async with db.session() as s:
        actions = [r.action for r in await AuditRepository(s).find(entity_type="alert")]
    assert sorted(actions) == ["acknowledge", "created", "resolve", "snooze"]
    assert [m["event"] for m, _ in sent] == ["created", "acknowledge", "snooze", "resolve"]
    assert all(m["type"] == "alert" and m["alert"]["id"] == str(aid) for m, _ in sent)
    assert str(fid) in sent[0][1]


async def test_auto_resolve_closes_only_low_tier_alerts(db, rt, world):
    low_an, low = await make_alert(db, rt, world.flats[0].id, severity=0.35)
    hi_an, hi = await make_alert(db, rt, world.flats[1].id, severity=0.95)
    async with db.session() as s:
        for flat_id, an_id in ((world.flats[0].id, low_an), (world.flats[1].id, hi_an)):
            await ae.auto_resolve(
                s,
                rt,
                await FlatRepository(s).get(flat_id),
                await AnomalyRepository(s).get(an_id),
                T0,
            )
    assert (await get(db, low)).state == "resolved" and (await get(db, hi)).state == "ticket"

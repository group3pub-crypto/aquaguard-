"""Alert lifecycle: dedupe, escalation tiers, guarded + warned + confirmed auto shut-off.

Auto shut-off protocol (tier 3 "ticket" -> tier 4 "auto_shutoff"):

1. all guards pass            -> ``shutoff_warned_at`` set, audit + WS ``shutoff_warning``
2. warning window elapsed     -> guards re-evaluated; a human ack/snooze/override/resolve during
   (SHUTOFF_WARNING_SECONDS)     the window, or a guard that stopped holding, CANCELS the warning
3. still all clear            -> close command persisted (committed) and then published
4. device ack ``closed``      -> ONLY NOW alert becomes tier 4 / ``auto_shutoff`` and the resident
                                 message says water was shut off
   publish failed / ack timeout -> alert stays at ``ticket``; retried exactly once; then we give
                                 up (``shutoff_gave_up_at``) and notify staff (audit + WS)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.models import Alert, Anomaly, Flat, ValveCommand
from app.models.alerts import OPEN_STATES
from app.repositories import (
    AlertRepository,
    AnomalyRepository,
    AuditRepository,
    FlatRepository,
    ReadingRepository,
    ValveRepository,
)
from app.runtime import Runtime
from app.schemas.domain import AlertOut
from app.services import detector, valve_control

log = structlog.get_logger(__name__)
_UNSET: Any = object()

TIER_THRESHOLDS = ((0.9, 4), (0.7, 3), (0.5, 2), (0.3, 1))
STATE_BY_TIER = {1: "nudge", 2: "alert", 3: "ticket", 4: "auto_shutoff"}
ESCALATION_DELAY = {1: timedelta(minutes=30), 2: timedelta(hours=2), 3: timedelta(hours=6)}
OVERRIDE_COOLDOWN = timedelta(hours=24)
DEDUPE_WINDOW = timedelta(minutes=30)
INITIAL_MAX_TIER = 3  # tier 4 is only reachable through the guarded, confirmed path
LEARNING_MAX_TIER = 2  # no automatic escalation above "alert" while the baseline is learning
LEARNING_NOTE = (
    "Note: this home's usage baseline is still being learned, so automatic escalation is "
    "limited to the 'alert' level."
)
SHUTOFF_TYPES = {"leak", "burst"}
SHUTOFF_MIN_CONFIDENCE = 0.85
SHUTOFF_MIN_SEVERITY = 0.9
SHUTOFF_PERSIST = timedelta(hours=6)
MAX_SHUTOFF_ATTEMPTS = 2  # the first try + exactly one retry
ACTIVE_USE_ZERO_PCT = 0.10  # flow that regularly stops looks like people using water
ACTIVE_USE_STEP_MIN = 15  # raw_zero_flow_pct looks at 15 minutes; slide it over the window
RECENT_DATA_MAX_AGE = timedelta(minutes=15)

TRANSITIONS: dict[str, set[str]] = {
    "acknowledge": {"nudge", "alert", "ticket", "auto_shutoff", "snoozed"},
    "snooze": {"nudge", "alert", "ticket", "acknowledged", "snoozed"},
    "override": {"nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed"},
    "resolve": {"nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed"},
}


class InvalidTransition(Exception):
    def __init__(self, action: str, state: str) -> None:
        super().__init__(f"cannot {action} an alert in state '{state}'")


def tier_for(severity: float) -> int:
    return next((t for thr, t in TIER_THRESHOLDS if severity >= thr), 0)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def escalation_base(alert: Alert, now: datetime) -> datetime:
    """Moment the current tier started: latest of creation, last escalation, snooze expiry."""
    cands = [alert.created_at]
    if alert.escalated_at:
        cands.append(datetime.fromisoformat(alert.escalated_at[-1]))
    if alert.snoozed_until and alert.snoozed_until <= now:
        cands.append(alert.snoozed_until)
    return max(_aware(c) for c in cands)


def auto_shutoff_block_reason(
    alert: Alert,
    anomaly: Anomaly,
    now: datetime,
    active_use: bool | None,
    baseline_status: str | None = None,
) -> str | None:
    """First guard that forbids an automatic shut-off, or None when it is allowed."""
    if baseline_status == "learning":
        return "baseline_learning"
    if anomaly.anomaly_type not in SHUTOFF_TYPES:
        return "type_not_eligible"
    if anomaly.confidence < SHUTOFF_MIN_CONFIDENCE:
        return "low_confidence"
    if alert.severity_score < SHUTOFF_MIN_SEVERITY:
        return "low_severity"
    if alert.state != "ticket" or now - escalation_base(alert, now) < SHUTOFF_PERSIST:
        return "not_persisted_6h_at_ticket"
    if active_use is None:
        return "no_recent_data"
    if active_use:
        return "active_use"
    return None


def _tier_cap(flat: Flat) -> int:
    return LEARNING_MAX_TIER if flat.baseline_status == "learning" else INITIAL_MAX_TIER


def _message(rt: Runtime, flat: Flat, anomaly: Anomaly, tier: int, capped: bool = False) -> str:
    text = rt.explainer.message(
        {
            "unit": flat.unit_number,
            "anomaly_type": anomaly.anomaly_type,
            "est_loss_lpm": anomaly.est_loss_lpm,
            "tier": tier,
        }
    )
    return f"{text} {LEARNING_NOTE}" if capped else text


async def _emit(
    session: AsyncSession,
    rt: Runtime,
    flat: Flat,
    alert: Alert,
    action: str,
    actor: str,
    details: dict | None = None,
) -> None:
    await AuditRepository(session).record(
        entity_type="alert",
        entity_id=alert.id,
        action=action,
        actor=actor,
        details={"state": alert.state, "tier": alert.tier, **(details or {})},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    await rt.ws.broadcast(
        {
            "type": "alert",
            "event": action,
            "alert": AlertOut.model_validate(alert).model_dump(mode="json"),
        },
        [str(flat.id), str(flat.society_id)],
    )


def _set_tier(alert: Alert, tier: int, now: datetime) -> None:
    alert.tier, alert.state = tier, STATE_BY_TIER[tier]
    alert.escalated_at = [*(alert.escalated_at or []), now.isoformat()]
    metrics.ALERTS_TOTAL.labels(tier=str(tier)).inc()


# ------------------------------------------------------------------------------ creation
async def process_anomaly(
    session: AsyncSession, rt: Runtime, flat: Flat, anomaly: Anomaly, now: datetime
) -> Alert | None:
    """Create (or update) the alert for an anomaly. Returns None if suppressed."""
    raw_tier = min(tier_for(anomaly.severity), INITIAL_MAX_TIER)
    if raw_tier == 0:
        return None
    cap = _tier_cap(flat)
    tier = min(raw_tier, cap)
    capped = raw_tier > tier
    alerts = AlertRepository(session)
    existing = await alerts.find_dedupe(
        flat.id, anomaly.anomaly_type, anomaly.id, now - DEDUPE_WINDOW
    )
    if existing:
        return await _refresh_existing(session, rt, flat, anomaly, existing, tier, capped, now)
    if await alerts.find_overridden(flat.id, anomaly.anomaly_type, now):
        return None  # inside 24h override cooldown
    alert = await alerts.add(
        Alert(
            flat_id=flat.id,
            anomaly_id=anomaly.id,
            state=STATE_BY_TIER[tier],
            tier=tier,
            severity_score=anomaly.severity,
            message=_message(rt, flat, anomaly, tier, capped),
            message_source=rt.explainer.source,
            escalated_at=[],
        )
    )
    alert.created_at = now  # time-travel friendly (and exact for replayed data)
    metrics.ALERTS_TOTAL.labels(tier=str(tier)).inc()
    details: dict[str, Any] = {"anomaly_type": anomaly.anomaly_type}
    if capped:
        details.update(learning_cap=True, uncapped_tier=raw_tier)
    await _emit(session, rt, flat, alert, "created", "system", details)
    return alert


async def _refresh_existing(
    session: AsyncSession,
    rt: Runtime,
    flat: Flat,
    anomaly: Anomaly,
    alert: Alert,
    tier: int,
    capped: bool,
    now: datetime,
) -> Alert:
    """De-duplicated anomaly: refresh severity and re-evaluate the tier when severity rose.

    An acknowledged / snoozed alert is re-opened only by a severity INCREASE that reaches a
    higher tier; otherwise it stays as the human left it.
    """
    increased = anomaly.severity > alert.severity_score
    alert.severity_score = anomaly.severity
    if increased and alert.state in OPEN_STATES and tier > alert.tier:
        previous = alert.state
        _set_tier(alert, tier, now)
        alert.message = _message(rt, flat, anomaly, tier, capped)
        alert.snoozed_until = None
        alert.shutoff_warned_at = None
        await _emit(
            session,
            rt,
            flat,
            alert,
            "escalated",
            "system",
            {"reason": "severity_increase", "previous_state": previous, "learning_cap": capped},
        )
    else:
        await session.flush()
    return alert


# ------------------------------------------------------------------------------ escalation
async def _active_use(
    session: AsyncSession, rt: Runtime, flat: Flat, now: datetime
) -> bool | None:
    """Is somebody using water? None when there is not enough RECENT data to tell.

    Looks back ACTIVE_USE_WINDOW_MIN minutes, sliding the 15-minute raw zero-flow measure
    across it: any 15-minute slice where flow regularly stops counts as active use.
    """
    window = timedelta(minutes=rt.settings.active_use_window_min)
    readings = await ReadingRepository(session).window(flat.id, now - window, now)
    if len(readings) < 3 or now - max(_aware(r.timestamp) for r in readings) > RECENT_DATA_MAX_AGE:
        return None
    steps = max(1, rt.settings.active_use_window_min // ACTIVE_USE_STEP_MIN)
    for k in range(steps):
        end = now - timedelta(minutes=ACTIVE_USE_STEP_MIN * k)
        start = end - timedelta(minutes=ACTIVE_USE_STEP_MIN)
        if not any(start < _aware(r.timestamp) <= end for r in readings):
            continue
        if detector.raw_zero_flow_pct(readings, end) > ACTIVE_USE_ZERO_PCT:
            return True
    return False


async def _cancel_warning(
    session: AsyncSession, rt: Runtime, flat: Flat, alert: Alert, reason: str, actor="system"
) -> None:
    alert.shutoff_warned_at = None
    await _emit(
        session, rt, flat, alert, "shutoff_warning_cancelled", actor, {"reason": reason}
    )


async def escalate_alert(
    session: AsyncSession,
    rt: Runtime,
    alert: Alert,
    now: datetime,
    active_use: bool | None = _UNSET,
) -> bool:
    """Advance one alert if its delay elapsed. Returns True if anything was persisted/changed
    (a tier change, a shut-off warning, a command issued, a give-up...)."""
    flat = await FlatRepository(session).get(alert.flat_id)
    if flat is None:
        return False
    if alert.state == "snoozed":
        if alert.snoozed_until and now >= alert.snoozed_until:
            alert.state = STATE_BY_TIER[alert.tier]
            await _emit(session, rt, flat, alert, "snooze_expired", "system")
            return True
        return False
    if alert.state not in ("nudge", "alert", "ticket"):
        return False
    anomaly = await AnomalyRepository(session).get(alert.anomaly_id)
    if anomaly is None or anomaly.resolved_at is not None:
        if alert.shutoff_warned_at is not None:
            await _cancel_warning(session, rt, flat, alert, "anomaly_resolved")
            return True
        return False
    if now - escalation_base(alert, now) < ESCALATION_DELAY[alert.tier]:
        return False
    new_tier = alert.tier + 1
    if new_tier == 4:
        return await _try_auto_shutoff(session, rt, flat, alert, anomaly, now, active_use)
    if new_tier > _tier_cap(flat):
        if alert.message and LEARNING_NOTE in alert.message:
            return False  # already recorded
        alert.message = f"{alert.message or ''} {LEARNING_NOTE}".strip()
        await _emit(
            session,
            rt,
            flat,
            alert,
            "escalation_capped",
            "system",
            {"reason": "baseline_learning", "wanted_tier": new_tier},
        )
        return True
    _set_tier(alert, new_tier, now)
    alert.message = _message(rt, flat, anomaly, new_tier)
    await _emit(session, rt, flat, alert, "escalated", "system")
    return True


async def _try_auto_shutoff(
    session: AsyncSession,
    rt: Runtime,
    flat: Flat,
    alert: Alert,
    anomaly: Anomaly,
    now: datetime,
    active_use: bool | None,
) -> bool:
    if alert.shutoff_gave_up_at is not None:
        return False
    if active_use is _UNSET:
        active_use = await _active_use(session, rt, flat, now)
    reason = auto_shutoff_block_reason(alert, anomaly, now, active_use, flat.baseline_status)
    if reason:
        log.info("auto_shutoff_blocked", alert=str(alert.id), reason=reason)
        if alert.shutoff_warned_at is not None:  # conditions changed during the warning window
            await _cancel_warning(session, rt, flat, alert, reason)
            return True
        return False

    valves = ValveRepository(session)
    attempts, latest = await valves.auto_close_attempts(alert.id)
    if latest is not None and latest.status == "pending":
        return False  # command in flight: wait for the device ack or the ack timeout
    if latest is not None and latest.status == "confirmed":
        await on_shutoff_confirmed(session, rt, flat, latest, now)
        return True
    if attempts >= MAX_SHUTOFF_ATTEMPTS:
        alert.shutoff_gave_up_at = now
        await _emit(
            session,
            rt,
            flat,
            alert,
            "shutoff_failed",
            "system",
            {
                "attempts": attempts,
                "last_status": latest.status if latest else None,
                "action_required": "close the valve manually",
            },
        )
        return True

    # --- warning window
    if alert.shutoff_warned_at is None:
        alert.shutoff_warned_at = now
        executes = now + timedelta(seconds=rt.settings.shutoff_warning_seconds)
        await _emit(
            session,
            rt,
            flat,
            alert,
            "shutoff_warning",
            "system",
            {
                "executes_at": executes.isoformat(),
                "warning_seconds": rt.settings.shutoff_warning_seconds,
                "cancel_by": "acknowledge, snooze, override or resolve the alert",
            },
        )
        await session.commit()
        return True
    if now - _aware(alert.shutoff_warned_at) < timedelta(seconds=rt.settings.shutoff_warning_seconds):
        return False

    # --- execute (attempt 1) or retry (attempt 2); the alert stays at 'ticket' until confirmed
    cmd = await valve_control.send_valve_command(
        session,
        rt,
        flat,
        "close",
        source="auto",
        issued_by="system",
        alert_id=alert.id,
        reason="auto shut-off" if attempts == 0 else "auto shut-off retry",
        now=now,
        details={"attempt": attempts + 1},
    )
    await _emit(
        session,
        rt,
        flat,
        alert,
        "shutoff_attempt",
        "system",
        {"attempt": attempts + 1, "valve_command": str(cmd.id), "valve_status": cmd.status},
    )
    return True


async def on_shutoff_confirmed(
    session: AsyncSession, rt: Runtime, flat: Flat, cmd: ValveCommand, now: datetime
) -> None:
    """The device confirmed an automatic close: only now does the alert reach tier 4."""
    if cmd.alert_id is None:
        return
    alert = await AlertRepository(session).get(cmd.alert_id)
    if alert is None:
        return
    if alert.state == "auto_shutoff":
        return  # a retry's late ack, nothing new to say
    if alert.state not in OPEN_STATES:  # resolved / overridden meanwhile, yet the valve closed
        await _emit(
            session,
            rt,
            flat,
            alert,
            "shutoff_confirmed_after_close",
            "device",
            {"valve_command": str(cmd.id), "action_required": "valve is closed: reopen it?"},
        )
        return
    anomaly = await AnomalyRepository(session).get(alert.anomaly_id)
    previous = alert.state
    _set_tier(alert, 4, now)
    if anomaly:
        alert.message = _message(rt, flat, anomaly, 4)
    await _emit(
        session,
        rt,
        flat,
        alert,
        "auto_shutoff",
        "system",
        {"valve_command": str(cmd.id), "valve_status": "confirmed", "previous_state": previous},
    )


async def run_escalations(
    session: AsyncSession, rt: Runtime, now: datetime | None = None, page: int = 200
) -> int:
    """Process every alert that may need work. Set-based candidate selection (keyset pages, no
    cap), ONE transaction per alert (`FOR UPDATE SKIP LOCKED`), a failure never rolls back
    the others."""
    now = now or datetime.now(UTC)
    alerts = AlertRepository(session)
    changed = 0
    after: uuid.UUID | None = None
    while True:
        ids = await alerts.escalation_candidate_ids(now, after=after, limit=page)
        if not ids:
            return changed
        after = ids[-1]
        for alert_id in ids:
            try:
                alert = await alerts.lock(alert_id)
                if alert is not None:
                    changed += int(await escalate_alert(session, rt, alert, now))
                await session.commit()
            except Exception:
                log.exception("escalation_failed", alert=str(alert_id))
                await session.rollback()


# ------------------------------------------------------------------------------ resolution
async def auto_resolve(
    session: AsyncSession, rt: Runtime, flat: Flat, anomaly: Anomaly, now: datetime
) -> None:
    """Detector saw normal flow again: close low-tier alerts; tickets/shut-offs need a human."""
    for alert in await AlertRepository(session).for_anomaly(anomaly.id):
        if alert.state in ("nudge", "alert"):
            alert.state, alert.resolved_at = "resolved", now
            await _emit(session, rt, flat, alert, "auto_resolved", "system")


async def apply_action(
    session: AsyncSession,
    rt: Runtime,
    alert: Alert,
    action: str,
    actor: str,
    *,
    now: datetime | None = None,
    minutes: int | None = None,
    reason: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> Alert:
    """Manual transition (acknowledge | snooze | override | resolve) with audit + broadcast.

    `actor` is the AUTHENTICATED username; `extra_details` may carry the asserted
    ``on_behalf_of`` of a service account. Any manual action cancels a pending shut-off warning.
    """
    now = now or datetime.now(UTC)
    if alert.state not in TRANSITIONS[action]:
        raise InvalidTransition(action, alert.state)
    flat = await FlatRepository(session).get(alert.flat_id)
    assert flat is not None
    previous = alert.state
    details: dict = {"previous_state": previous, **(extra_details or {})}
    anomaly = await AnomalyRepository(session).get(alert.anomaly_id)
    if alert.shutoff_warned_at is not None:
        await _cancel_warning(session, rt, flat, alert, f"manual_{action}", actor)
        details["shutoff_warning_cancelled"] = True
    if action == "acknowledge":
        alert.state, alert.acknowledged_by, alert.acknowledged_at = "acknowledged", actor, now
    elif action == "snooze":
        alert.state, alert.snoozed_until = "snoozed", now + timedelta(minutes=minutes or 15)
        details["minutes"] = minutes
    elif action == "override":
        alert.state, alert.snoozed_until = "overridden", now + OVERRIDE_COOLDOWN
        details["reason"] = reason
        if anomaly:
            anomaly.resolved_at = now
        _, latest = await ValveRepository(session).auto_close_attempts(alert.id)
        closed_or_closing = previous == "auto_shutoff" or (
            latest is not None and latest.status in ("pending", "confirmed", "timeout")
        )
        if closed_or_closing:  # operator says the shut-off was wrong: restore water
            cmd = await valve_control.send_valve_command(
                session,
                rt,
                flat,
                "open",
                source="manual_override",
                issued_by=actor,
                alert_id=alert.id,
                reason=reason,
                now=now,
                details=extra_details,
            )
            details["valve_command"] = str(cmd.id)
    else:
        alert.state, alert.resolved_at = "resolved", now
        if anomaly:
            anomaly.resolved_at = now
    await _emit(session, rt, flat, alert, action, actor, details)
    return alert

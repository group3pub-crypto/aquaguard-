"""Alert lifecycle: creation, de-duplication, escalation tiers, auto shut-off, manual transitions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Anomaly, Flat
from app.repositories import (
    AlertRepository, AnomalyRepository, AuditRepository, FlatRepository, ReadingRepository,
)
from app.runtime import Runtime
from app.schemas.domain import AlertOut
from app.services import valve_control
from app.services.detector import WINDOW, extract_features

log = structlog.get_logger(__name__)
_UNSET: Any = object()

TIER_THRESHOLDS = ((0.9, 4), (0.7, 3), (0.5, 2), (0.3, 1))
STATE_BY_TIER = {1: "nudge", 2: "alert", 3: "ticket", 4: "auto_shutoff"}
ESCALATION_DELAY = {1: timedelta(minutes=30), 2: timedelta(hours=2), 3: timedelta(hours=6)}
OVERRIDE_COOLDOWN = timedelta(hours=24)
INITIAL_MAX_TIER = 3  # tier 4 is only reachable through guarded escalation
SHUTOFF_TYPES = {"leak", "burst"}
SHUTOFF_MIN_CONFIDENCE = 0.85
SHUTOFF_MIN_SEVERITY = 0.9
SHUTOFF_PERSIST = timedelta(hours=6)
ACTIVE_USE_ZERO_PCT = 0.10  # flow that regularly stops looks like people using water

TRANSITIONS: dict[str, set[str]] = {
    "acknowledge": {"nudge", "alert", "ticket", "auto_shutoff", "snoozed"},
    "snooze": {"nudge", "alert", "ticket", "acknowledged", "snoozed"},
    "override": {"nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed"},
    "resolve": {"nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed"},
}


class InvalidTransition(Exception):  # noqa: N818
    def __init__(self, action: str, state: str) -> None:
        super().__init__(f"cannot {action} an alert in state '{state}'")


def tier_for(severity: float) -> int:
    return next((t for thr, t in TIER_THRESHOLDS if severity >= thr), 0)


def escalation_base(alert: Alert, now: datetime) -> datetime:
    """Moment the current tier started: latest of creation, last escalation, snooze expiry."""
    cands = [alert.created_at]
    if alert.escalated_at:
        cands.append(datetime.fromisoformat(alert.escalated_at[-1]))
    if alert.snoozed_until and alert.snoozed_until <= now:
        cands.append(alert.snoozed_until)
    return max(c if c.tzinfo else c.replace(tzinfo=UTC) for c in cands)


def auto_shutoff_block_reason(
    alert: Alert, anomaly: Anomaly, now: datetime, active_use: bool | None
) -> str | None:
    """First guard that forbids an automatic shut-off, or None when it is allowed."""
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


async def _emit(session: AsyncSession, rt: Runtime, flat: Flat, alert: Alert, action: str,
                actor: str, details: dict | None = None) -> None:
    await AuditRepository(session).record(
        entity_type="alert", entity_id=alert.id, action=action, actor=actor,
        details={"state": alert.state, "tier": alert.tier, **(details or {})},
        flat_id=flat.id, society_id=flat.society_id,
    )
    await rt.ws.broadcast(
        {"type": "alert", "event": action, "alert": AlertOut.model_validate(alert).model_dump(mode="json")},
        [str(flat.id), str(flat.society_id)],
    )


async def process_anomaly(
    session: AsyncSession, rt: Runtime, flat: Flat, anomaly: Anomaly, now: datetime
) -> Alert | None:
    """Create (or update) the alert for an anomaly. Returns None if suppressed."""
    tier = min(tier_for(anomaly.severity), INITIAL_MAX_TIER)
    if tier == 0:
        return None
    alerts = AlertRepository(session)
    existing = await alerts.find_unresolved(flat.id, anomaly.anomaly_type)
    if existing:  # de-duplicate: same flat + type already has an open alert
        existing.severity_score = anomaly.severity
        await alerts.flush()
        return existing
    if await alerts.find_overridden(flat.id, anomaly.anomaly_type, now):
        return None  # inside 24h override cooldown
    message = rt.explainer.message(
        {"unit": flat.unit_number, "anomaly_type": anomaly.anomaly_type,
         "est_loss_lpm": anomaly.est_loss_lpm, "tier": tier}
    )
    alert = await alerts.add(
        Alert(
            flat_id=flat.id, anomaly_id=anomaly.id, state=STATE_BY_TIER[tier], tier=tier,
            severity_score=anomaly.severity, message=message, message_source=rt.explainer.source,
            escalated_at=[],
        )
    )
    alert.created_at = now  # time-travel friendly (and exact for replayed data)
    await _emit(session, rt, flat, alert, "created", "system", {"anomaly_type": anomaly.anomaly_type})
    return alert


async def _active_use(session: AsyncSession, flat: Flat, now: datetime) -> bool | None:
    readings = await ReadingRepository(session).window(flat.id, now - WINDOW, now)
    if len(readings) < 3:
        return None
    return extract_features(readings, None, now)["zero_flow_pct"] > ACTIVE_USE_ZERO_PCT


async def escalate_alert(
    session: AsyncSession, rt: Runtime, alert: Alert, now: datetime, active_use: bool | None = _UNSET,
) -> bool:
    """Advance one alert if its delay elapsed. Returns True if its state changed."""
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
        return False
    if now - escalation_base(alert, now) < ESCALATION_DELAY[alert.tier]:
        return False
    new_tier = alert.tier + 1
    if new_tier == 4:
        if active_use is _UNSET:
            active_use = await _active_use(session, flat, now)
        reason = auto_shutoff_block_reason(alert, anomaly, now, active_use)
        if reason:
            log.info("auto_shutoff_blocked", alert=str(alert.id), reason=reason)
            return False
        cmd = await valve_control.send_valve_command(
            session, rt, flat, "close", source="auto", issued_by="system", alert_id=alert.id,
            reason="auto shut-off", now=now,
        )
        extra = {"valve_command": str(cmd.id), "valve_status": cmd.status}
    else:
        extra = {}
    alert.tier, alert.state = new_tier, STATE_BY_TIER[new_tier]
    alert.escalated_at = [*(alert.escalated_at or []), now.isoformat()]
    alert.message = rt.explainer.message(
        {"unit": flat.unit_number, "anomaly_type": anomaly.anomaly_type,
         "est_loss_lpm": anomaly.est_loss_lpm, "tier": new_tier}
    )
    await _emit(session, rt, flat, alert, "auto_shutoff" if new_tier == 4 else "escalated", "system", extra)
    return True


async def run_escalations(session: AsyncSession, rt: Runtime, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    n = 0
    for alert in await AlertRepository(session).active():
        n += int(await escalate_alert(session, rt, alert, now))
    return n


async def auto_resolve(session: AsyncSession, rt: Runtime, flat: Flat, anomaly: Anomaly, now: datetime) -> None:
    """Detector saw normal flow again: close low-tier alerts; tickets/shut-offs need a human."""
    for alert in await AlertRepository(session).for_anomaly(anomaly.id):
        if alert.state in ("nudge", "alert"):
            alert.state, alert.resolved_at = "resolved", now
            await _emit(session, rt, flat, alert, "auto_resolved", "system")


async def apply_action(
    session: AsyncSession, rt: Runtime, alert: Alert, action: str, actor: str, *,
    now: datetime | None = None, minutes: int | None = None, reason: str | None = None,
) -> Alert:
    """Manual transition (acknowledge | snooze | override | resolve) with audit + broadcast."""
    now = now or datetime.now(UTC)
    if alert.state not in TRANSITIONS[action]:
        raise InvalidTransition(action, alert.state)
    flat = await FlatRepository(session).get(alert.flat_id)
    assert flat is not None
    previous = alert.state
    details: dict = {"previous_state": previous}
    anomaly = await AnomalyRepository(session).get(alert.anomaly_id)
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
        if previous == "auto_shutoff":  # operator says the shut-off was wrong: restore water
            cmd = await valve_control.send_valve_command(
                session, rt, flat, "open", source="manual_override", issued_by=actor,
                alert_id=alert.id, reason=reason, now=now,
            )
            details["valve_command"] = str(cmd.id)
    else:
        alert.state, alert.resolved_at = "resolved", now
        if anomaly:
            anomaly.resolved_at = now
    await _emit(session, rt, flat, alert, action, actor, details)
    return alert

"""Valve commands: persist (commit) -> publish over MQTT -> confirm on device ack.

Commit-then-publish (outbox style): the ``pending`` row is COMMITTED before anything is sent, so
a rolled-back transaction can never leave a valve closed with no record of why, and a crash
between commit and publish just leaves a pending row that times out and is retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Flat, ValveCommand
from app.mqtt.topics import valve_cmd_topic
from app.repositories import AuditRepository, FlatRepository, ValveRepository
from app.runtime import Runtime
from app.schemas.domain import ValveCommandOut
from app.services.auth import utcnow

log = structlog.get_logger(__name__)
ACK_TIMEOUT = timedelta(seconds=60)  # default; the runtime value is VALVE_ACK_TIMEOUT_S
EXPECTED_STATE = {"close": "closed", "open": "open"}


def _broadcast_payload(cmd: ValveCommand) -> dict[str, Any]:
    return {
        "type": "valve",
        "event": "command",
        "command": ValveCommandOut.model_validate(cmd).model_dump(mode="json"),
    }


async def send_valve_command(
    session: AsyncSession,
    rt: Runtime,
    flat: Flat,
    command: str,
    *,
    source: str,
    issued_by: str,
    alert_id=None,
    reason: str | None = None,
    now: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> ValveCommand:
    """Issue a valve command. NOTE: commits `session` (twice) as part of the outbox protocol;
    callers must not hold other uncommitted work they would not want persisted."""
    now = now or utcnow()
    repo, audit = ValveRepository(session), AuditRepository(session)
    cmd = await repo.create(
        flat_id=flat.id,
        alert_id=alert_id,
        command=command,
        source=source,
        issued_by=issued_by,
        issued_at=now,
        status="pending",
    )
    slug = await FlatRepository(session).society_slug(flat)
    base = {"source": source, "reason": reason, "alert_id": str(alert_id) if alert_id else None}
    await audit.record(
        entity_type="valve",
        entity_id=cmd.id,
        action=f"valve_{command}_pending",
        actor=issued_by,
        details={**base, **(details or {})},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    await session.commit()  # durable BEFORE the device can act on it

    payload = {
        "v": 1,
        "id": cmd.id.hex[:8],
        "cmd": command,
        "src": source,
        "ts": int(now.timestamp()),
    }
    error: str | None = None
    try:
        if slug is None:
            raise LookupError("society not found")
        await rt.publisher.publish(valve_cmd_topic(slug, flat.unit_number), payload)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("valve_publish_failed", flat=str(flat.id), error=error)
    if error is not None:
        cmd.status = "failed"
        await audit.record(
            entity_type="valve",
            entity_id=cmd.id,
            action=f"valve_{command}_failed",
            actor="system",
            details={**base, "error": error},
            flat_id=flat.id,
            society_id=flat.society_id,
        )
        await session.commit()
    await rt.ws.broadcast(_broadcast_payload(cmd), [str(flat.id), str(flat.society_id)])
    return cmd


async def handle_ack(
    session: AsyncSession, rt: Runtime, flat: Flat, short_id: str, state: str, now: datetime
) -> ValveCommand | None:
    """Device reported its valve state. Confirms the matching command only when the reported
    state is the one the command asked for; a late ack of a timed-out command still counts
    (the valve moved)."""
    from app.services import alert_engine  # local: alert_engine imports this module

    flat.valve_state = state
    match = next(
        (c for c in await ValveRepository(session).ackable_for_flat(flat.id) if c.id.hex[:8] == short_id),
        None,
    )
    late = False
    confirmed = False
    if match:
        late = match.status == "timeout"
        if state == EXPECTED_STATE[match.command]:
            match.status, match.confirmed_at, confirmed = "confirmed", now, True
        else:
            match.status = "failed"
    await AuditRepository(session).record(
        entity_type="valve",
        entity_id=match.id if match else flat.id,
        action="valve_state_ack",
        actor="device",
        details={
            "state": state,
            "cmd_id": short_id,
            "matched": bool(match),
            "confirmed": confirmed,
            "late": late,
        },
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    if match and confirmed and match.source == "auto" and match.command == "close":
        await alert_engine.on_shutoff_confirmed(session, rt, flat, match, now)
    await rt.ws.broadcast(
        {"type": "valve", "event": "state", "flat_id": str(flat.id), "state": state},
        [str(flat.id), str(flat.society_id)],
    )
    return match


async def expire_pending(
    session: AsyncSession, now: datetime, timeout_s: float | None = None, limit: int = 500
) -> int:
    """Mark commands without a device ack as timed out (audited). Returns how many."""
    timeout = ACK_TIMEOUT if timeout_s is None else timedelta(seconds=timeout_s)
    expired = await ValveRepository(session).expired_pending(now - timeout, limit)
    audit = AuditRepository(session)
    for c in expired:
        c.status = "timeout"
        await audit.record(
            entity_type="valve",
            entity_id=c.id,
            action=f"valve_{c.command}_timeout",
            actor="system",
            details={"source": c.source, "alert_id": str(c.alert_id) if c.alert_id else None},
            flat_id=c.flat_id,
        )
    return len(expired)

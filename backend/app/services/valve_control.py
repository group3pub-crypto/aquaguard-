"""Valve commands: persist as pending, publish over MQTT, confirm on device ack."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Flat, ValveCommand
from app.mqtt.topics import valve_cmd_topic
from app.repositories import AuditRepository, FlatRepository, ValveRepository
from app.runtime import Runtime
from app.schemas.domain import ValveCommandOut

log = structlog.get_logger(__name__)
ACK_TIMEOUT = timedelta(seconds=60)


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
) -> ValveCommand:
    now = now or datetime.now(UTC)
    repo = ValveRepository(session)
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
    payload = {
        "v": 1,
        "id": cmd.id.hex[:8],
        "cmd": command,
        "src": source,
        "ts": int(now.timestamp()),
    }
    try:
        if slug is None:
            raise LookupError("society not found")
        await rt.publisher.publish(valve_cmd_topic(slug, flat.unit_number), payload)
    except Exception as exc:
        cmd.status = "failed"
        log.warning("valve_publish_failed", flat=str(flat.id), error=str(exc))
    await AuditRepository(session).record(
        entity_type="valve",
        entity_id=cmd.id,
        action=f"valve_{command}_{cmd.status}",
        actor=issued_by,
        details={
            "source": source,
            "reason": reason,
            "alert_id": str(alert_id) if alert_id else None,
        },
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    await rt.ws.broadcast(
        {
            "type": "valve",
            "event": "command",
            "command": ValveCommandOut.model_validate(cmd).model_dump(mode="json"),
        },
        [str(flat.id), str(flat.society_id)],
    )
    return cmd


async def handle_ack(
    session: AsyncSession, rt: Runtime, flat: Flat, short_id: str, state: str, now: datetime
) -> ValveCommand | None:
    """Device reported its valve state. Confirms the matching pending command, if any."""
    flat.valve_state = state
    match = next(
        (
            c
            for c in await ValveRepository(session).pending_for_flat(flat.id)
            if c.id.hex[:8] == short_id
        ),
        None,
    )
    if match:
        match.status, match.confirmed_at = "confirmed", now
    await AuditRepository(session).record(
        entity_type="valve",
        entity_id=match.id if match else flat.id,
        action="valve_state_ack",
        actor="device",
        details={"state": state, "cmd_id": short_id, "matched": bool(match)},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    await rt.ws.broadcast(
        {"type": "valve", "event": "state", "flat_id": str(flat.id), "state": state},
        [str(flat.id), str(flat.society_id)],
    )
    return match


async def expire_pending(session: AsyncSession, now: datetime) -> int:
    expired = await ValveRepository(session).expired_pending(now - ACK_TIMEOUT)
    for c in expired:
        c.status = "timeout"
    return len(expired)

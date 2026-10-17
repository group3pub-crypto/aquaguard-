"""Tool endpoints consumed by the chat bot (JSON in, JSON out). All obey the caller's scope."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.access import flat_in_scope, require_staff_scope, society_in_scope
from app.actor import resolve_actor
from app.dependencies import RT, AdminUser, CallerScope, Session
from app.repositories import AlertRepository
from app.schemas.domain import AlertOut, ValveCommandActionOut
from app.schemas.tools import (
    ActiveAlertsRequest,
    FlatUsageRequest,
    ToolValveRequest,
    TopLossesRequest,
    WeeklyReportRequest,
)
from app.services import report, valve_control

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("/active-alerts")
async def active_alerts(body: ActiveAlertsRequest, session: Session, scope: CallerScope):
    flat_ids = None
    society_id = None
    if body.flat_id:
        await flat_in_scope(session, scope, body.flat_id)
        flat_ids = [body.flat_id]
    elif body.society_id:
        await society_in_scope(session, scope, body.society_id)
        society_id = body.society_id
    alerts = await AlertRepository(session).active(
        flat_ids, scope=scope, society_id=society_id, limit=body.limit
    )
    return {
        "count": len(alerts),
        "alerts": [AlertOut.model_validate(a).model_dump(mode="json") for a in alerts],
    }


@router.post("/flat-usage")
async def flat_usage(body: FlatUsageRequest, session: Session, scope: CallerScope):
    await flat_in_scope(session, scope, body.flat_id)
    return await report.flat_usage(session, body.flat_id, body.hours, datetime.now(UTC))


@router.post("/top-losses")
async def top_losses(body: TopLossesRequest, session: Session, scope: CallerScope):
    await society_in_scope(session, scope, body.society_id)
    require_staff_scope(scope)
    rows = await report.top_losses(
        session, body.society_id, body.days, body.limit, datetime.now(UTC)
    )
    return {"society_id": str(body.society_id), "days": body.days, "losses": rows}


@router.post("/valve-control")
async def valve_control_tool(
    body: ToolValveRequest, admin: AdminUser, session: Session, rt: RT, scope: CallerScope
):
    actor = resolve_actor(admin, body.on_behalf_of)
    flat = await flat_in_scope(session, scope, body.flat_id)
    cmd = await valve_control.send_valve_command(
        session,
        rt,
        flat,
        body.action,
        source="manual_bot",
        issued_by=actor.username,  # body.issued_by is ignored: it used to forge the audit trail
        reason=body.reason,
        details=actor.audit_details(),
    )
    out = ValveCommandActionOut.model_validate(cmd).model_copy(update=actor.echo())
    return out.model_dump(mode="json")


@router.post("/weekly-report")
async def weekly_report(body: WeeklyReportRequest, session: Session, scope: CallerScope):
    society = await society_in_scope(session, scope, body.society_id)
    require_staff_scope(scope)
    return {"society_id": str(society.id), "markdown": await report.weekly_report(session, society)}

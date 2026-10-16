"""Tool endpoints consumed by the chat bot (JSON in, JSON out)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import RT, AdminUser, Session, require_user
from app.repositories import AlertRepository, FlatRepository, SocietyRepository
from app.schemas.domain import AlertOut, ValveCommandOut
from app.schemas.tools import (
    ActiveAlertsRequest,
    FlatUsageRequest,
    ToolValveRequest,
    TopLossesRequest,
    WeeklyReportRequest,
)
from app.services import report, valve_control

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("/active-alerts", dependencies=[Depends(require_user)])
async def active_alerts(body: ActiveAlertsRequest, session: Session):
    flats = FlatRepository(session)
    ids = None
    if body.flat_id:
        ids = [body.flat_id]
    elif body.society_id:
        ids = await flats.ids_for_society(body.society_id)
    alerts = (await AlertRepository(session).active(ids))[: body.limit]
    return {
        "count": len(alerts),
        "alerts": [AlertOut.model_validate(a).model_dump(mode="json") for a in alerts],
    }


@router.post("/flat-usage", dependencies=[Depends(require_user)])
async def flat_usage(body: FlatUsageRequest, session: Session):
    if await FlatRepository(session).get(body.flat_id) is None:
        raise HTTPException(404, "Flat not found")
    return await report.flat_usage(session, body.flat_id, body.hours, datetime.now(UTC))


@router.post("/top-losses", dependencies=[Depends(require_user)])
async def top_losses(body: TopLossesRequest, session: Session):
    rows = await report.top_losses(
        session, body.society_id, body.days, body.limit, datetime.now(UTC)
    )
    return {"society_id": str(body.society_id), "days": body.days, "losses": rows}


@router.post("/valve-control")
async def valve_control_tool(body: ToolValveRequest, admin: AdminUser, session: Session, rt: RT):
    flat = await FlatRepository(session).get(body.flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    cmd = await valve_control.send_valve_command(
        session,
        rt,
        flat,
        body.action,
        source="manual_bot",
        issued_by=body.issued_by or admin.username,
        reason=body.reason,
    )
    return ValveCommandOut.model_validate(cmd).model_dump(mode="json")


@router.post("/weekly-report", dependencies=[Depends(require_user)])
async def weekly_report(body: WeeklyReportRequest, session: Session):
    society = await SocietyRepository(session).get(body.society_id)
    if society is None:
        raise HTTPException(404, "Society not found")
    return {"society_id": str(society.id), "markdown": await report.weekly_report(session, society)}

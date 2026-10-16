from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import RT, AdminUser, Session, StaffUser, require_user
from app.models import Alert
from app.models.alerts import ALERT_STATES
from app.repositories import AlertRepository
from app.schemas.common import Limit, Offset
from app.schemas.domain import (
    AcknowledgeRequest,
    AlertOut,
    OverrideRequest,
    ResolveRequest,
    SnoozeRequest,
)
from app.services import alert_engine

router = APIRouter(prefix="/alerts", tags=["alerts"])


async def _alert_or_404(session, alert_id: uuid.UUID) -> Alert:
    alert = await AlertRepository(session).get(alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return alert


async def _act(session, rt, alert_id, action, actor, **kw) -> Alert:
    alert = await _alert_or_404(session, alert_id)
    try:
        return await alert_engine.apply_action(session, rt, alert, action, actor, **kw)
    except alert_engine.InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("", response_model=list[AlertOut], dependencies=[Depends(require_user)])
async def list_alerts(
    session: Session,
    flat_id: uuid.UUID | None = None,
    state: Annotated[str | None, Query(pattern=f"^({'|'.join(ALERT_STATES)})$")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await AlertRepository(session).find(
        flat_id=flat_id, state=state, limit=limit, offset=offset
    )


@router.get("/{alert_id}", response_model=AlertOut, dependencies=[Depends(require_user)])
async def get_alert(alert_id: uuid.UUID, session: Session):
    return await _alert_or_404(session, alert_id)


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge(
    alert_id: uuid.UUID,
    user: StaffUser,
    session: Session,
    rt: RT,
    body: AcknowledgeRequest | None = None,
):
    return await _act(session, rt, alert_id, "acknowledge", user.username)


@router.post("/{alert_id}/snooze", response_model=AlertOut)
async def snooze(
    alert_id: uuid.UUID, body: SnoozeRequest, user: StaffUser, session: Session, rt: RT
):
    return await _act(session, rt, alert_id, "snooze", user.username, minutes=body.duration_minutes)


@router.post("/{alert_id}/override", response_model=AlertOut)
async def override(
    alert_id: uuid.UUID, body: OverrideRequest, user: AdminUser, session: Session, rt: RT
):
    return await _act(session, rt, alert_id, "override", user.username, reason=body.reason)


@router.post("/{alert_id}/resolve", response_model=AlertOut)
async def resolve(
    alert_id: uuid.UUID,
    user: StaffUser,
    session: Session,
    rt: RT,
    body: ResolveRequest | None = None,
):
    return await _act(session, rt, alert_id, "resolve", user.username)

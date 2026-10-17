from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.actor import resolve_actor
from app.dependencies import RT, AdminUser, CallerScope, Session, StaffUser
from app.models import Alert, User
from app.models.alerts import ALERT_STATES
from app.repositories import AlertRepository
from app.schemas.common import Limit, Offset
from app.schemas.domain import (
    AcknowledgeRequest,
    AlertActionOut,
    AlertOut,
    OverrideRequest,
    ResolveRequest,
    SnoozeRequest,
)
from app.services import alert_engine

router = APIRouter(prefix="/alerts", tags=["alerts"])


async def _alert_or_404(session, scope, alert_id: uuid.UUID, *, for_update=False) -> Alert:
    alert = await AlertRepository(session).get_scoped(alert_id, scope, for_update=for_update)
    if alert is None:
        raise HTTPException(404, "Alert not found")
    return alert


async def _act(
    session, rt, scope, user: User, alert_id, action, on_behalf_of, **kw
) -> AlertActionOut:
    # the actor is ALWAYS the authenticated account; only service accounts may add an asserted
    # `on_behalf_of` (422 for everyone else) - body fields like acknowledged_by are ignored
    actor = resolve_actor(user, on_behalf_of)
    alert = await _alert_or_404(session, scope, alert_id, for_update=True)
    try:
        alert = await alert_engine.apply_action(
            session, rt, alert, action, actor.username, extra_details=actor.audit_details(), **kw
        )
    except alert_engine.InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from None
    return AlertActionOut.model_validate(alert).model_copy(update=actor.echo())


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    session: Session,
    scope: CallerScope,
    flat_id: uuid.UUID | None = None,
    state: Annotated[str | None, Query(pattern=f"^({'|'.join(ALERT_STATES)})$")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await AlertRepository(session).find(
        scope=scope, flat_id=flat_id, state=state, limit=limit, offset=offset
    )


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(alert_id: uuid.UUID, session: Session, scope: CallerScope):
    return await _alert_or_404(session, scope, alert_id)


@router.post("/{alert_id}/acknowledge", response_model=AlertActionOut)
async def acknowledge(
    alert_id: uuid.UUID,
    user: StaffUser,
    session: Session,
    rt: RT,
    scope: CallerScope,
    body: AcknowledgeRequest | None = None,
):
    return await _act(
        session, rt, scope, user, alert_id, "acknowledge", body.on_behalf_of if body else None
    )


@router.post("/{alert_id}/snooze", response_model=AlertActionOut)
async def snooze(
    alert_id: uuid.UUID,
    body: SnoozeRequest,
    user: StaffUser,
    session: Session,
    rt: RT,
    scope: CallerScope,
):
    return await _act(
        session, rt, scope, user, alert_id, "snooze", body.on_behalf_of, minutes=body.duration_minutes
    )


@router.post("/{alert_id}/override", response_model=AlertActionOut)
async def override(
    alert_id: uuid.UUID,
    body: OverrideRequest,
    user: AdminUser,
    session: Session,
    rt: RT,
    scope: CallerScope,
):
    return await _act(
        session, rt, scope, user, alert_id, "override", body.on_behalf_of, reason=body.reason
    )


@router.post("/{alert_id}/resolve", response_model=AlertActionOut)
async def resolve(
    alert_id: uuid.UUID,
    user: StaffUser,
    session: Session,
    rt: RT,
    scope: CallerScope,
    body: ResolveRequest | None = None,
):
    return await _act(
        session, rt, scope, user, alert_id, "resolve", body.on_behalf_of if body else None
    )

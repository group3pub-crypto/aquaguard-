from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter

from app.access import flat_in_scope
from app.actor import resolve_actor
from app.dependencies import RT, AdminUser, CallerScope, Session
from app.repositories import ValveRepository
from app.schemas.domain import ValveCommandActionOut, ValveCommandOut, ValveRequest, ValveStatus
from app.services import valve_control

router = APIRouter(prefix="/valves", tags=["valves"])


async def _command(
    session, rt, scope, flat_id, command: Literal["open", "close"], body: ValveRequest, admin
) -> ValveCommandActionOut:
    actor = resolve_actor(admin, body.on_behalf_of)
    flat = await flat_in_scope(session, scope, flat_id)
    cmd = await valve_control.send_valve_command(
        session,
        rt,
        flat,
        command,
        source=body.source,
        issued_by=actor.username,  # never body.issued_by
        reason=body.reason,
        details=actor.audit_details(),
    )
    return ValveCommandActionOut.model_validate(cmd).model_copy(update=actor.echo())


@router.get("/{flat_id}", response_model=ValveStatus)
async def valve_status(flat_id: uuid.UUID, session: Session, scope: CallerScope):
    flat = await flat_in_scope(session, scope, flat_id)
    repo = ValveRepository(session)
    return ValveStatus(
        flat_id=flat.id,
        valve_state=flat.valve_state,
        pending=[ValveCommandOut.model_validate(c) for c in await repo.pending_for_flat(flat.id)],
        recent=[ValveCommandOut.model_validate(c) for c in await repo.recent(flat.id)],
    )


@router.post("/{flat_id}/open", response_model=ValveCommandActionOut)
async def open_valve(
    flat_id: uuid.UUID,
    body: ValveRequest,
    admin: AdminUser,
    session: Session,
    rt: RT,
    scope: CallerScope,
):
    return await _command(session, rt, scope, flat_id, "open", body, admin)


@router.post("/{flat_id}/close", response_model=ValveCommandActionOut)
async def close_valve(
    flat_id: uuid.UUID,
    body: ValveRequest,
    admin: AdminUser,
    session: Session,
    rt: RT,
    scope: CallerScope,
):
    return await _command(session, rt, scope, flat_id, "close", body, admin)

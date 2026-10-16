from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import RT, AdminUser, Session, require_user
from app.repositories import FlatRepository, ValveRepository
from app.schemas.domain import ValveCommandOut, ValveRequest, ValveStatus
from app.services import valve_control

router = APIRouter(prefix="/valves", tags=["valves"])


async def _flat(session, flat_id: uuid.UUID):
    flat = await FlatRepository(session).get(flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    return flat


async def _command(session, rt, flat_id, command: Literal["open", "close"], body, admin):
    flat = await _flat(session, flat_id)
    return await valve_control.send_valve_command(
        session, rt, flat, command, source=body.source, issued_by=admin.username, reason=body.reason
    )


@router.get("/{flat_id}", response_model=ValveStatus, dependencies=[Depends(require_user)])
async def valve_status(flat_id: uuid.UUID, session: Session):
    flat = await _flat(session, flat_id)
    repo = ValveRepository(session)
    return ValveStatus(
        flat_id=flat.id,
        valve_state=flat.valve_state,
        pending=[ValveCommandOut.model_validate(c) for c in await repo.pending_for_flat(flat.id)],
        recent=[ValveCommandOut.model_validate(c) for c in await repo.recent(flat.id)],
    )


@router.post("/{flat_id}/open", response_model=ValveCommandOut)
async def open_valve(
    flat_id: uuid.UUID, body: ValveRequest, admin: AdminUser, session: Session, rt: RT
):
    return await _command(session, rt, flat_id, "open", body, admin)


@router.post("/{flat_id}/close", response_model=ValveCommandOut)
async def close_valve(
    flat_id: uuid.UUID, body: ValveRequest, admin: AdminUser, session: Session, rt: RT
):
    return await _command(session, rt, flat_id, "close", body, admin)

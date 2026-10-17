from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError

from app.access import require_staff_scope, society_in_scope
from app.dependencies import AdminUser, CallerScope, Session
from app.repositories import AuditRepository, FlatRepository, SocietyRepository
from app.schemas.admin import SocietyCreate, SocietyUpdate
from app.schemas.common import Limit, Offset
from app.schemas.domain import SocietyOut, SocietyStats
from app.services.ingest import OFFLINE_AFTER

router = APIRouter(prefix="/societies", tags=["societies"])


@router.get("", response_model=list[SocietyOut])
async def list_societies(
    session: Session, scope: CallerScope, limit: Limit = 50, offset: Offset = 0
):
    """admin: all; maintenance / resident: only their own society (summary)."""
    return await SocietyRepository(session).find(scope=scope, limit=limit, offset=offset)


@router.post("", response_model=SocietyOut, status_code=status.HTTP_201_CREATED)
async def create_society(body: SocietyCreate, admin: AdminUser, session: Session):
    repo = SocietyRepository(session)
    if await repo.get_by_slug(body.slug):
        raise HTTPException(status.HTTP_409_CONFLICT, "Slug already in use")
    try:
        society = await repo.create(name=body.name, slug=body.slug, address=body.address)
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Slug already in use") from None
    await AuditRepository(session).record(
        entity_type="society",
        entity_id=society.id,
        action="society_created",
        actor=admin.username,
        details={"slug": society.slug, "name": society.name},
        society_id=society.id,
    )
    return society


@router.get("/{society_id}", response_model=SocietyOut)
async def get_society(society_id: uuid.UUID, session: Session, scope: CallerScope):
    return await society_in_scope(session, scope, society_id)


@router.patch("/{society_id}", response_model=SocietyOut)
async def update_society(
    society_id: uuid.UUID, body: SocietyUpdate, admin: AdminUser, session: Session
):
    society = await SocietyRepository(session).get(society_id)
    if society is None:
        raise HTTPException(404, "Society not found")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(422, "name cannot be null")
    for k, v in changes.items():
        setattr(society, k, v)
    await AuditRepository(session).record(
        entity_type="society",
        entity_id=society.id,
        action="society_updated",
        actor=admin.username,
        details={"changes": changes},
        society_id=society.id,
    )
    return society


@router.delete("/{society_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_society(society_id: uuid.UUID, admin: AdminUser, session: Session):
    """Only an empty society can be deleted (remove its flats first: that is destructive and
    deliberately a separate, per-flat action)."""
    repo = SocietyRepository(session)
    society = await repo.get(society_id)
    if society is None:
        raise HTTPException(404, "Society not found")
    if await FlatRepository(session).count_for_society(society_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Society still has flats")
    await AuditRepository(session).record(
        entity_type="society",
        entity_id=society.id,
        action="society_deleted",
        actor=admin.username,
        details={"slug": society.slug},
    )
    await session.delete(society)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{society_id}/stats", response_model=SocietyStats)
async def society_stats(society_id: uuid.UUID, session: Session, scope: CallerScope):
    await society_in_scope(session, scope, society_id)
    require_staff_scope(scope)
    now = datetime.now(UTC)
    row = await SocietyRepository(session).stats(
        society_id,
        now,
        volume_from=now - timedelta(hours=24),
        offline_before=now - OFFLINE_AFTER,
    )
    return SocietyStats(
        society_id=society_id,
        flats=int(row["flats"]),
        active_alerts=int(row["active_alerts"]),
        volume_24h_l=round(float(row["volume_24h_l"]), 1),
        sensors_offline=int(row["sensors_offline"]),
    )

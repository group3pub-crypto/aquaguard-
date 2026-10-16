from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import Session, require_user
from app.repositories import (
    AlertRepository,
    FlatRepository,
    ReadingRepository,
    SensorHealthRepository,
    SocietyRepository,
)
from app.schemas.common import Limit, Offset
from app.schemas.domain import SocietyOut, SocietyStats
from app.services.ingest import sensor_status

router = APIRouter(prefix="/societies", tags=["societies"], dependencies=[Depends(require_user)])


async def _society_or_404(session, society_id: uuid.UUID):
    society = await SocietyRepository(session).get(society_id)
    if society is None:
        raise HTTPException(404, "Society not found")
    return society


@router.get("", response_model=list[SocietyOut])
async def list_societies(session: Session, limit: Limit = 50, offset: Offset = 0):
    return await SocietyRepository(session).find(limit=limit, offset=offset)


@router.get("/{society_id}", response_model=SocietyOut)
async def get_society(society_id: uuid.UUID, session: Session):
    return await _society_or_404(session, society_id)


@router.get("/{society_id}/stats", response_model=SocietyStats)
async def society_stats(society_id: uuid.UUID, session: Session):
    await _society_or_404(session, society_id)
    now = datetime.now(UTC)
    ids = await FlatRepository(session).ids_for_society(society_id)
    volume = await ReadingRepository(session).volume_sum(ids, now - timedelta(hours=24), now)
    health = {h.flat_id: h for h in await SensorHealthRepository(session).all()}
    offline = sum(
        1
        for i in ids
        if sensor_status(health[i].last_seen if i in health else None, now) == "offline"
    )
    return SocietyStats(
        society_id=society_id,
        flats=len(ids),
        active_alerts=await AlertRepository(session).count_active(ids),
        volume_24h_l=round(sum(volume.values()), 1),
        sensors_offline=offline,
    )

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.dependencies import RT, AdminUser, Session, require_user
from app.models import Flat
from app.repositories import (
    BaselineRepository,
    FlatRepository,
    ForecastRepository,
    ReadingRepository,
    SensorHealthRepository,
)
from app.schemas.common import Offset
from app.schemas.domain import (
    BaselineOut,
    FlatDetail,
    FlatOut,
    ForecastOut,
    ForecastRequest,
    ReadingOut,
    SensorStatus,
)
from app.services import baseline as baseline_svc
from app.services import forecaster
from app.services.ingest import sensor_status

router = APIRouter(prefix="/flats", tags=["flats"], dependencies=[Depends(require_user)])


async def flat_or_404(session, flat_id: uuid.UUID) -> Flat:
    flat = await FlatRepository(session).get(flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    return flat


@router.get("", response_model=list[FlatOut])
async def list_flats(
    session: Session,
    society_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Offset = 0,
):
    return await FlatRepository(session).find(society_id=society_id, limit=limit, offset=offset)


@router.get("/{flat_id}", response_model=FlatDetail)
async def get_flat(flat_id: uuid.UUID, session: Session):
    flat = await flat_or_404(session, flat_id)
    latest = await ReadingRepository(session).latest(flat.id)
    health = await SensorHealthRepository(session).get_for_flat(flat.id)
    last_seen = health.last_seen if health else None
    return FlatDetail(
        **FlatOut.model_validate(flat).model_dump(),
        current_flow=latest.flow_rate if latest else None,
        last_reading_at=latest.timestamp if latest else None,
        sensor_status=SensorStatus(
            status=sensor_status(last_seen, datetime.now(UTC)),
            last_seen=last_seen,
            missed_readings=health.missed_readings if health else 0,
        ),
    )


@router.get("/{flat_id}/readings", response_model=list[ReadingOut])
async def list_readings(
    flat_id: uuid.UUID,
    session: Session,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Offset = 0,
):
    await flat_or_404(session, flat_id)
    return await ReadingRepository(session).find(
        flat_id, start=from_, end=to, limit=limit, offset=offset
    )


@router.get("/{flat_id}/baseline", response_model=BaselineOut)
async def get_baseline(flat_id: uuid.UUID, session: Session):
    await flat_or_404(session, flat_id)
    baseline = await BaselineRepository(session).active(flat_id)
    if baseline is None:
        raise HTTPException(404, "No baseline yet")
    return baseline


@router.post("/{flat_id}/baseline/retrain", response_model=BaselineOut)
async def retrain_baseline(flat_id: uuid.UUID, admin: AdminUser, session: Session):
    flat = await flat_or_404(session, flat_id)
    return await baseline_svc.train_baseline(session, flat, actor=admin.username)


@router.get("/{flat_id}/forecast", response_model=ForecastOut)
async def get_forecast(flat_id: uuid.UUID, session: Session):
    await flat_or_404(session, flat_id)
    fc = await ForecastRepository(session).latest(flat_id)
    if fc is None:
        raise HTTPException(404, "No forecast generated yet")
    return fc


@router.post("/{flat_id}/forecast/generate", response_model=ForecastOut)
async def generate_forecast(
    flat_id: uuid.UUID,
    admin: AdminUser,
    session: Session,
    rt: RT,
    body: Annotated[ForecastRequest | None, Body()] = None,
):
    flat = await flat_or_404(session, flat_id)
    return await forecaster.generate_forecast(
        session,
        rt.models,
        flat,
        horizon_hours=(body or ForecastRequest()).horizon_hours,
        actor=admin.username,
    )

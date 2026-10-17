from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError

from app.access import flat_in_scope
from app.dependencies import RT, AdminUser, CallerScope, Session
from app.repositories import (
    AuditRepository,
    BaselineRepository,
    FlatRepository,
    ForecastRepository,
    ReadingRepository,
    SensorHealthRepository,
    SocietyRepository,
)
from app.schemas.admin import FlatCreate, FlatUpdate
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

router = APIRouter(prefix="/flats", tags=["flats"])


@router.get("", response_model=list[FlatOut])
async def list_flats(
    session: Session,
    scope: CallerScope,
    society_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Offset = 0,
):
    """admin: all flats; maintenance: their society's; resident: only their own flat."""
    return await FlatRepository(session).find(
        society_id=society_id, scope=scope, limit=limit, offset=offset
    )


@router.post("", response_model=FlatOut, status_code=status.HTTP_201_CREATED)
async def create_flat(body: FlatCreate, admin: AdminUser, session: Session):
    societies, flats = SocietyRepository(session), FlatRepository(session)
    society = await societies.get(body.society_id)
    if society is None:
        raise HTTPException(404, "Society not found")
    if await flats.get_by_unit(society.id, body.unit_number):
        raise HTTPException(status.HTTP_409_CONFLICT, "Unit already exists in this society")
    # the sensor id doubles as the MQTT device username: "<society-slug>.<unit>"
    sensor_id = body.sensor_id or f"{society.slug}.{body.unit_number}"
    if await flats.get_by_sensor_id(sensor_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "sensor_id already assigned")
    try:
        flat = await flats.create(
            society_id=society.id,
            unit_number=body.unit_number,
            floor=body.floor,
            residents=body.residents,
            sensor_id=sensor_id,
        )
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Unit or sensor_id already exists") from None
    society.total_flats = await flats.count_for_society(society.id)
    await AuditRepository(session).record(
        entity_type="flat",
        entity_id=flat.id,
        action="flat_created",
        actor=admin.username,
        details={"unit": flat.unit_number, "sensor_id": flat.sensor_id},
        flat_id=flat.id,
        society_id=society.id,
    )
    return flat


@router.get("/{flat_id}", response_model=FlatDetail)
async def get_flat(flat_id: uuid.UUID, session: Session, scope: CallerScope):
    flat = await flat_in_scope(session, scope, flat_id)
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


@router.patch("/{flat_id}", response_model=FlatOut)
async def update_flat(flat_id: uuid.UUID, body: FlatUpdate, admin: AdminUser, session: Session):
    flats = FlatRepository(session)
    flat = await flats.get(flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("residents", 0) is None or changes.get("sensor_id", "x") is None:
        raise HTTPException(422, "residents / sensor_id cannot be null")
    sensor_id = changes.get("sensor_id")
    if sensor_id and sensor_id != flat.sensor_id and await flats.get_by_sensor_id(sensor_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "sensor_id already assigned")
    for k, v in changes.items():
        setattr(flat, k, v)
    try:
        await flats.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "sensor_id already assigned") from None
    await AuditRepository(session).record(
        entity_type="flat",
        entity_id=flat.id,
        action="flat_updated",
        actor=admin.username,
        details={"changes": changes},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    return flat


@router.delete("/{flat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flat(flat_id: uuid.UUID, admin: AdminUser, session: Session):
    """Destroys the flat's readings, alerts and baselines. Refused while its valve is closed
    (water would stay off with nobody able to command it)."""
    flats = FlatRepository(session)
    flat = await flats.get(flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    if flat.valve_state == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Valve is closed: open it before deleting")
    society = await SocietyRepository(session).get(flat.society_id)
    await AuditRepository(session).record(
        entity_type="flat",
        entity_id=flat.id,
        action="flat_deleted",
        actor=admin.username,
        details={"unit": flat.unit_number, "sensor_id": flat.sensor_id},
        society_id=flat.society_id,
    )
    await session.delete(flat)
    await session.flush()
    if society is not None:
        society.total_flats = await flats.count_for_society(society.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{flat_id}/readings", response_model=list[ReadingOut])
async def list_readings(
    flat_id: uuid.UUID,
    session: Session,
    scope: CallerScope,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Offset = 0,
):
    await flat_in_scope(session, scope, flat_id)
    return await ReadingRepository(session).find(
        flat_id, start=from_, end=to, limit=limit, offset=offset
    )


@router.get("/{flat_id}/baseline", response_model=BaselineOut)
async def get_baseline(flat_id: uuid.UUID, session: Session, scope: CallerScope):
    await flat_in_scope(session, scope, flat_id)
    baseline = await BaselineRepository(session).active(flat_id)
    if baseline is None:
        raise HTTPException(404, "No baseline yet")
    return baseline


@router.post("/{flat_id}/baseline/retrain", response_model=BaselineOut)
async def retrain_baseline(
    flat_id: uuid.UUID, admin: AdminUser, session: Session, scope: CallerScope
):
    flat = await flat_in_scope(session, scope, flat_id)
    return await baseline_svc.train_baseline(session, flat, actor=admin.username)


@router.get("/{flat_id}/forecast", response_model=ForecastOut)
async def get_forecast(flat_id: uuid.UUID, session: Session, scope: CallerScope):
    await flat_in_scope(session, scope, flat_id)
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
    scope: CallerScope,
    body: Annotated[ForecastRequest | None, Body()] = None,
):
    flat = await flat_in_scope(session, scope, flat_id)
    return await forecaster.generate_forecast(
        session,
        rt.models,
        flat,
        horizon_hours=(body or ForecastRequest()).horizon_hours,
        actor=admin.username,
    )

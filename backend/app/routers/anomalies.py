from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import Session, require_user
from app.models.alerts import ANOMALY_TYPES
from app.repositories import AnomalyRepository
from app.schemas.common import Limit, Offset
from app.schemas.domain import AnomalyOut

router = APIRouter(prefix="/anomalies", tags=["anomalies"], dependencies=[Depends(require_user)])


@router.get("", response_model=list[AnomalyOut])
async def list_anomalies(
    session: Session,
    flat_id: uuid.UUID | None = None,
    type: Annotated[str | None, Query(pattern=f"^({'|'.join(ANOMALY_TYPES)})$")] = None,
    active_only: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await AnomalyRepository(session).find(
        flat_id=flat_id, anomaly_type=type, active_only=active_only, limit=limit, offset=offset
    )


@router.get("/{anomaly_id}", response_model=AnomalyOut)
async def get_anomaly(anomaly_id: uuid.UUID, session: Session):
    anomaly = await AnomalyRepository(session).get(anomaly_id)
    if anomaly is None:
        raise HTTPException(404, "Anomaly not found")
    return anomaly

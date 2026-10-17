from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import CallerScope, Session
from app.models.alerts import ANOMALY_TYPES
from app.repositories import AnomalyRepository
from app.schemas.common import Limit, Offset
from app.schemas.domain import AnomalyOut

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.get("", response_model=list[AnomalyOut])
async def list_anomalies(
    session: Session,
    scope: CallerScope,
    flat_id: uuid.UUID | None = None,
    type: Annotated[str | None, Query(pattern=f"^({'|'.join(ANOMALY_TYPES)})$")] = None,
    active_only: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await AnomalyRepository(session).find(
        scope=scope,
        flat_id=flat_id,
        anomaly_type=type,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )


@router.get("/{anomaly_id}", response_model=AnomalyOut)
async def get_anomaly(anomaly_id: uuid.UUID, session: Session, scope: CallerScope):
    anomaly = await AnomalyRepository(session).get_scoped(anomaly_id, scope)
    if anomaly is None:
        raise HTTPException(404, "Anomaly not found")
    return anomaly

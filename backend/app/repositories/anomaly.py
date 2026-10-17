from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app.models import Anomaly, Flat
from app.repositories.base import BaseRepository
from app.scope import Scope


class AnomalyRepository(BaseRepository[Anomaly]):
    model = Anomaly

    async def find(
        self,
        *,
        scope: Scope | None = None,
        flat_id: uuid.UUID | None = None,
        anomaly_type: str | None = None,
        active_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Anomaly]:
        stmt = select(Anomaly).order_by(Anomaly.detected_at.desc(), Anomaly.id)
        if scope is not None:
            stmt = stmt.where(scope.flat_id_pred(Anomaly.flat_id))
        if flat_id:
            stmt = stmt.where(Anomaly.flat_id == flat_id)
        if anomaly_type:
            stmt = stmt.where(Anomaly.anomaly_type == anomaly_type)
        if active_only:
            stmt = stmt.where(Anomaly.resolved_at.is_(None))
        return await self._all(stmt.limit(limit).offset(offset))

    async def get_scoped(self, anomaly_id: uuid.UUID, scope: Scope) -> Anomaly | None:
        return await self._one(
            select(Anomaly).where(Anomaly.id == anomaly_id, scope.flat_id_pred(Anomaly.flat_id))
        )

    async def open_for_flat(self, flat_id: uuid.UUID) -> list[Anomaly]:
        return await self._all(
            select(Anomaly).where(Anomaly.flat_id == flat_id, Anomaly.resolved_at.is_(None))
        )

    async def find_open(self, flat_id: uuid.UUID, anomaly_type: str) -> Anomaly | None:
        return await self._one(
            select(Anomaly)
            .where(
                Anomaly.flat_id == flat_id,
                Anomaly.anomaly_type == anomaly_type,
                Anomaly.resolved_at.is_(None),
            )
            .order_by(Anomaly.detected_at.desc())
        )

    async def between(
        self, flat_ids: list[uuid.UUID], start: datetime, end: datetime
    ) -> list[Anomaly]:
        if not flat_ids:
            return []
        return await self._all(
            select(Anomaly)
            .where(
                Anomaly.flat_id.in_(flat_ids),
                Anomaly.detected_at >= start,
                Anomaly.detected_at <= end,
            )
            .order_by(Anomaly.detected_at)
        )

    async def between_society(
        self, society_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[Anomaly]:
        return await self._all(
            select(Anomaly)
            .join(Flat, Flat.id == Anomaly.flat_id)
            .where(
                Flat.society_id == society_id,
                Anomaly.detected_at >= start,
                Anomaly.detected_at <= end,
            )
            .order_by(Anomaly.detected_at)
        )

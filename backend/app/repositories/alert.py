from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app.models import Alert, Anomaly
from app.models.alerts import OPEN_STATES
from app.repositories.base import BaseRepository


class AlertRepository(BaseRepository[Alert]):
    model = Alert

    async def find(
        self,
        *,
        flat_id: uuid.UUID | None = None,
        state: str | None = None,
        active_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Alert]:
        stmt = select(Alert).order_by(Alert.created_at.desc())
        if flat_id:
            stmt = stmt.where(Alert.flat_id == flat_id)
        if state:
            stmt = stmt.where(Alert.state == state)
        if active_only:
            stmt = stmt.where(Alert.state.in_(OPEN_STATES))
        return await self._all(stmt.limit(limit).offset(offset))

    async def active(self, flat_ids: list[uuid.UUID] | None = None) -> list[Alert]:
        stmt = select(Alert).where(Alert.state.in_(OPEN_STATES)).order_by(Alert.severity_score.desc())
        if flat_ids is not None:
            stmt = stmt.where(Alert.flat_id.in_(flat_ids))
        return await self._all(stmt)

    async def count_active(self, flat_ids: list[uuid.UUID]) -> int:
        if not flat_ids:
            return 0
        return await self._count(
            select(Alert.id).where(Alert.flat_id.in_(flat_ids), Alert.state.in_(OPEN_STATES))
        )

    async def find_unresolved(self, flat_id: uuid.UUID, anomaly_type: str) -> Alert | None:
        """Latest still-open alert of this type for a flat (used for de-duplication)."""
        stmt = (
            select(Alert)
            .join(Anomaly, Anomaly.id == Alert.anomaly_id)
            .where(
                Alert.flat_id == flat_id,
                Anomaly.anomaly_type == anomaly_type,
                Alert.state.in_(OPEN_STATES),
            )
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        return await self._one(stmt)

    async def find_overridden(
        self, flat_id: uuid.UUID, anomaly_type: str, now: datetime
    ) -> Alert | None:
        """An override whose cooldown (snoozed_until) is still running."""
        stmt = (
            select(Alert)
            .join(Anomaly, Anomaly.id == Alert.anomaly_id)
            .where(
                Alert.flat_id == flat_id,
                Anomaly.anomaly_type == anomaly_type,
                Alert.state == "overridden",
                Alert.snoozed_until > now,
            )
            .limit(1)
        )
        return await self._one(stmt)

    async def for_anomaly(self, anomaly_id: uuid.UUID) -> list[Alert]:
        return await self._all(select(Alert).where(Alert.anomaly_id == anomaly_id))

    async def count_between(self, flat_ids: list[uuid.UUID], start: datetime, end: datetime) -> int:
        if not flat_ids:
            return 0
        return await self._count(
            select(Alert.id).where(
                Alert.flat_id.in_(flat_ids), Alert.created_at >= start, Alert.created_at <= end
            )
        )

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select

from app.models import Alert, Flat, FlowReading, SensorHealth, Society
from app.models.alerts import OPEN_STATES
from app.repositories.base import BaseRepository
from app.scope import Scope


class SocietyRepository(BaseRepository[Society]):
    model = Society

    async def find(
        self, *, scope: Scope | None = None, limit: int = 50, offset: int = 0
    ) -> list[Society]:
        stmt = select(Society).order_by(Society.name, Society.id)
        if scope is not None:
            stmt = stmt.where(scope.society_pred(Society.id))
        return await self._all(stmt.limit(limit).offset(offset))

    async def get_scoped(self, society_id: uuid.UUID, scope: Scope) -> Society | None:
        return await self._one(
            select(Society).where(Society.id == society_id, scope.society_pred(Society.id))
        )

    async def get_by_slug(self, slug: str) -> Society | None:
        return await self._one(select(Society).where(Society.slug == slug))

    async def create(self, **kw) -> Society:
        return await self.add(Society(**kw))

    async def exists(self, id_: uuid.UUID) -> bool:
        return await self.get(id_) is not None

    async def stats(
        self, society_id: uuid.UUID, now: datetime, *, volume_from: datetime, offline_before: datetime
    ) -> dict[str, float | int]:
        """Dashboard numbers for one society as four aggregate queries (no row loading)."""
        ex = self.session.execute
        flats = (
            await ex(select(func.count()).select_from(Flat).where(Flat.society_id == society_id))
        ).scalar_one()
        active = (
            await ex(
                select(func.count())
                .select_from(Alert)
                .join(Flat, Flat.id == Alert.flat_id)
                .where(Flat.society_id == society_id, Alert.state.in_(OPEN_STATES))
            )
        ).scalar_one()
        volume = (
            await ex(
                select(func.coalesce(func.sum(FlowReading.volume), 0.0))
                .select_from(FlowReading)
                .join(Flat, Flat.id == FlowReading.flat_id)
                .where(
                    Flat.society_id == society_id,
                    FlowReading.timestamp >= volume_from,
                    FlowReading.timestamp <= now,
                )
            )
        ).scalar_one()
        offline = (
            await ex(
                select(func.count())
                .select_from(Flat)
                .outerjoin(SensorHealth, SensorHealth.flat_id == Flat.id)
                .where(
                    Flat.society_id == society_id,
                    (SensorHealth.last_seen.is_(None)) | (SensorHealth.last_seen < offline_before),
                )
            )
        ).scalar_one()
        return {
            "flats": int(flats),
            "active_alerts": int(active),
            "volume_24h_l": float(volume),
            "sensors_offline": int(offline),
        }

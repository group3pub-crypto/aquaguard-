from __future__ import annotations

import uuid

from sqlalchemy import func, select, update

from app.models import Baseline
from app.repositories.base import BaseRepository


class BaselineRepository(BaseRepository[Baseline]):
    model = Baseline

    async def active(self, flat_id: uuid.UUID) -> Baseline | None:
        return await self._one(
            select(Baseline).where(Baseline.flat_id == flat_id, Baseline.is_active.is_(True))
        )

    async def next_version(self, flat_id: uuid.UUID) -> int:
        cur = (
            await self.session.execute(
                select(func.max(Baseline.version)).where(Baseline.flat_id == flat_id)
            )
        ).scalar_one_or_none()
        return (cur or 0) + 1

    async def create_active(self, **kw) -> Baseline:
        """Deactivate previous versions and insert a new active one."""
        flat_id = kw["flat_id"]
        await self.session.execute(
            update(Baseline).where(Baseline.flat_id == flat_id).values(is_active=False)
        )
        return await self.add(Baseline(is_active=True, **kw))

    async def history(self, flat_id: uuid.UUID) -> list[Baseline]:
        return await self._all(
            select(Baseline).where(Baseline.flat_id == flat_id).order_by(Baseline.version.desc())
        )

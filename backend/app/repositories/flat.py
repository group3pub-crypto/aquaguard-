from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Flat, Society
from app.repositories.base import BaseRepository


class FlatRepository(BaseRepository[Flat]):
    model = Flat

    async def find(
        self, *, society_id: uuid.UUID | None = None, limit: int = 50, offset: int = 0
    ) -> list[Flat]:
        stmt = select(Flat).order_by(Flat.unit_number)
        if society_id:
            stmt = stmt.where(Flat.society_id == society_id)
        return await self._all(stmt.limit(limit).offset(offset))

    async def ids_for_society(self, society_id: uuid.UUID) -> list[uuid.UUID]:
        res = await self.session.execute(select(Flat.id).where(Flat.society_id == society_id))
        return list(res.scalars().all())

    async def count_for_society(self, society_id: uuid.UUID) -> int:
        return await self._count(select(Flat.id).where(Flat.society_id == society_id))

    async def get_by_slug_unit(self, slug: str, unit: str) -> Flat | None:
        stmt = (
            select(Flat)
            .join(Society, Society.id == Flat.society_id)
            .where(Society.slug == slug, Flat.unit_number == unit)
        )
        return await self._one(stmt)

    async def society_slug(self, flat: Flat) -> str | None:
        return (
            await self.session.execute(select(Society.slug).where(Society.id == flat.society_id))
        ).scalar_one_or_none()

    async def create(self, **kw) -> Flat:
        return await self.add(Flat(**kw))

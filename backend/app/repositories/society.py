from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Society
from app.repositories.base import BaseRepository


class SocietyRepository(BaseRepository[Society]):
    model = Society

    async def find(self, *, limit: int = 50, offset: int = 0) -> list[Society]:
        return await self._all(select(Society).order_by(Society.name).limit(limit).offset(offset))

    async def get_by_slug(self, slug: str) -> Society | None:
        return await self._one(select(Society).where(Society.slug == slug))

    async def create(self, **kw) -> Society:
        return await self.add(Society(**kw))

    async def exists(self, id_: uuid.UUID) -> bool:
        return await self.get(id_) is not None

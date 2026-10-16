from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import SensorHealth
from app.repositories.base import BaseRepository


class SensorHealthRepository(BaseRepository[SensorHealth]):
    model = SensorHealth

    async def get_for_flat(self, flat_id: uuid.UUID) -> SensorHealth | None:
        return await self.get(flat_id)

    async def upsert(self, flat_id: uuid.UUID, **values) -> SensorHealth:
        row = await self.get(flat_id)
        if row is None:
            row = SensorHealth(flat_id=flat_id, **values)
            self.session.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)
        await self.flush()
        return row

    async def all(self) -> list[SensorHealth]:
        return await self._all(select(SensorHealth))

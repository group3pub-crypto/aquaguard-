from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import SensorHealth
from app.repositories.base import BaseRepository


class SensorHealthRepository(BaseRepository[SensorHealth]):
    model = SensorHealth

    async def get_for_flat(self, flat_id: uuid.UUID) -> SensorHealth | None:
        return await self.get(flat_id)

    async def upsert(self, flat_id: uuid.UUID, **values) -> None:
        """Insert-or-update. On PostgreSQL a single atomic ON CONFLICT statement, so concurrent
        first messages of a flat cannot fail on the primary key (and never touch the ORM
        identity map). Other dialects (SQLite tests) use get-then-write."""
        if self.session.bind.dialect.name == "postgresql":
            stmt = pg_insert(SensorHealth).values(flat_id=flat_id, **values)
            await self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SensorHealth.flat_id],
                    set_={**values, "updated_at": func.now()},
                )
            )
            # a row cached in this session (if any) is now stale
            cached = self.session.identity_map.get((SensorHealth, (flat_id,)), None)
            if cached is not None:
                self.session.expire(cached)
            return
        row = await self.get(flat_id)
        if row is None:
            row = SensorHealth(flat_id=flat_id, **values)
            self.session.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)
        await self.flush()

    async def all(self) -> list[SensorHealth]:
        return await self._all(select(SensorHealth))

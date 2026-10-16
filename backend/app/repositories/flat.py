from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from sqlalchemy import func, select

from app.models import Flat, Society
from app.repositories.base import BaseRepository
from app.scope import Scope


class FlatRepository(BaseRepository[Flat]):
    model = Flat

    async def find(
        self,
        *,
        society_id: uuid.UUID | None = None,
        scope: Scope | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Flat]:
        stmt = select(Flat).order_by(Flat.unit_number, Flat.id)
        if scope is not None:
            stmt = stmt.where(scope.flat_pred())
        if society_id:
            stmt = stmt.where(Flat.society_id == society_id)
        return await self._all(stmt.limit(limit).offset(offset))

    async def get_scoped(self, flat_id: uuid.UUID, scope: Scope) -> Flat | None:
        """The flat if (and only if) it is inside `scope`; out-of-scope == missing."""
        return await self._one(select(Flat).where(Flat.id == flat_id, scope.flat_pred()))

    async def ids_for_society(self, society_id: uuid.UUID) -> list[uuid.UUID]:
        res = await self.session.execute(select(Flat.id).where(Flat.society_id == society_id))
        return list(res.scalars().all())

    async def count_for_society(self, society_id: uuid.UUID) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count()).select_from(Flat).where(Flat.society_id == society_id)
                )
            ).scalar_one()
        )

    async def get_by_slug_unit(self, slug: str, unit: str) -> Flat | None:
        stmt = (
            select(Flat)
            .join(Society, Society.id == Flat.society_id)
            .where(Society.slug == slug, Flat.unit_number == unit)
        )
        return await self._one(stmt)

    async def get_by_unit(self, society_id: uuid.UUID, unit: str) -> Flat | None:
        return await self._one(
            select(Flat).where(Flat.society_id == society_id, Flat.unit_number == unit)
        )

    async def get_by_sensor_id(self, sensor_id: str) -> Flat | None:
        return await self._one(select(Flat).where(Flat.sensor_id == sensor_id))

    async def society_slug(self, flat: Flat) -> str | None:
        return (
            await self.session.execute(select(Society.slug).where(Society.id == flat.society_id))
        ).scalar_one_or_none()

    async def create(self, **kw) -> Flat:
        return await self.add(Flat(**kw))

    async def batches(self, size: int = 500) -> AsyncIterator[list[Flat]]:
        """All flats, keyset-paginated by id (no silent cap, stable under concurrent inserts)."""
        last: uuid.UUID | None = None
        while True:
            stmt = select(Flat).order_by(Flat.id).limit(size)
            if last is not None:
                stmt = stmt.where(Flat.id > last)
            rows = await self._all(stmt)
            if not rows:
                return
            yield rows
            last = rows[-1].id

    async def mark_stale_baselines(self, cutoff) -> int:
        """Set-based: ready -> stale where the active baseline was trained before `cutoff`."""
        from sqlalchemy import update

        from app.models import Baseline

        stale = select(Baseline.flat_id).where(
            Baseline.is_active.is_(True), Baseline.trained_to < cutoff
        )
        res = await self.session.execute(
            update(Flat)
            .where(Flat.baseline_status == "ready", Flat.id.in_(stale))
            .values(baseline_status="stale")
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

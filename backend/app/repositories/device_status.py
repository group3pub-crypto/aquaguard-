from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import DeviceStatus, Flat, SensorHealth
from app.repositories.base import BaseRepository


class DeviceStatusRepository(BaseRepository[DeviceStatus]):
    model = DeviceStatus

    async def get_for_flat(self, flat_id: uuid.UUID) -> DeviceStatus | None:
        return await self.get(flat_id)

    async def lock(self, flat_id: uuid.UUID) -> DeviceStatus | None:
        """Row under ``FOR UPDATE`` (serialises concurrent readings of one flat; no-op clause
        on SQLite)."""
        stmt = (
            select(DeviceStatus)
            .where(DeviceStatus.flat_id == flat_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._one(stmt)

    async def lock_or_create(self, flat_id: uuid.UUID) -> DeviceStatus:
        """The flat's status row, locked; created empty first if it does not exist yet."""
        row = await self.lock(flat_id)
        if row is not None:
            return row
        if self.dialect == "postgresql":
            await self.session.execute(
                pg_insert(DeviceStatus)
                .values(flat_id=flat_id, err_codes=[])
                .on_conflict_do_nothing(index_elements=["flat_id"])
            )
        else:
            self.session.add(DeviceStatus(flat_id=flat_id, err_codes=[]))
            await self.session.flush()
        row = await self.lock(flat_id)
        assert row is not None
        return row

    async def for_society(
        self, society_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[tuple[Flat, SensorHealth | None, DeviceStatus | None]]:
        """Every flat of a society with its sensor health and device status (one query)."""
        stmt = (
            select(Flat, SensorHealth, DeviceStatus)
            .outerjoin(SensorHealth, SensorHealth.flat_id == Flat.id)
            .outerjoin(DeviceStatus, DeviceStatus.flat_id == Flat.id)
            .where(Flat.society_id == society_id)
            .order_by(Flat.unit_number, Flat.id)
            .limit(limit)
            .offset(offset)
        )
        return [tuple(r) for r in (await self.session.execute(stmt)).all()]  # type: ignore[misc]

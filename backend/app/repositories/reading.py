from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.models import FlowReading
from app.repositories.base import BaseRepository


class ReadingRepository(BaseRepository[FlowReading]):
    model = FlowReading

    async def exists(self, flat_id: uuid.UUID, message_id: str) -> bool:
        stmt = select(FlowReading.id).where(
            FlowReading.flat_id == flat_id, FlowReading.message_id == message_id
        )
        return (await self.session.execute(stmt)).first() is not None

    async def add_idempotent(self, reading: FlowReading) -> bool:
        """Insert unless (flat_id, message_id) already stored. Returns True if inserted.

        PostgreSQL: one ``INSERT .. ON CONFLICT DO NOTHING RETURNING id`` (atomic, no failed
        statement, no savepoint, one round trip). Other dialects: check + savepoint.
        """
        if self.dialect == "postgresql":
            cols = (
                "flat_id", "timestamp", "flow_rate", "volume", "pressure", "temperature",
                "battery_pct", "rssi", "message_id", "received_at",
            )  # fmt: skip
            values = {c: getattr(reading, c) for c in cols}
            if values["received_at"] is None:
                values.pop("received_at")
            stmt = (
                pg_insert(FlowReading)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["flat_id", "message_id"])
                .returning(FlowReading.id)
            )
            new_id = (await self.session.execute(stmt)).scalar_one_or_none()
            if new_id is None:
                return False
            reading.id = new_id
            return True
        if await self.exists(reading.flat_id, reading.message_id):
            return False
        try:
            async with self.session.begin_nested():
                self.session.add(reading)
                await self.session.flush()
        except IntegrityError:  # lost a race with a concurrent duplicate
            return False
        return True

    async def find(
        self,
        flat_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[FlowReading]:
        stmt = select(FlowReading).where(FlowReading.flat_id == flat_id)
        if start:
            stmt = stmt.where(FlowReading.timestamp >= start)
        if end:
            stmt = stmt.where(FlowReading.timestamp <= end)
        return await self._all(
            stmt.order_by(FlowReading.timestamp.desc()).limit(limit).offset(offset)
        )

    async def window(self, flat_id: uuid.UUID, start: datetime, end: datetime) -> list[FlowReading]:
        stmt = (
            select(FlowReading)
            .where(
                FlowReading.flat_id == flat_id,
                FlowReading.timestamp >= start,
                FlowReading.timestamp <= end,
            )
            .order_by(FlowReading.timestamp)
        )
        return await self._all(stmt)

    async def latest(self, flat_id: uuid.UUID) -> FlowReading | None:
        stmt = (
            select(FlowReading)
            .where(FlowReading.flat_id == flat_id)
            .order_by(FlowReading.timestamp.desc())
            .limit(1)
        )
        return await self._one(stmt)

    async def count(self, flat_id: uuid.UUID) -> int:
        return await self._count(select(FlowReading.id).where(FlowReading.flat_id == flat_id))

    async def volume_sum(
        self, flat_ids: list[uuid.UUID], start: datetime, end: datetime
    ) -> dict[uuid.UUID, float]:
        if not flat_ids:
            return {}
        stmt = (
            select(FlowReading.flat_id, func.coalesce(func.sum(FlowReading.volume), 0.0))
            .where(
                FlowReading.flat_id.in_(flat_ids),
                FlowReading.timestamp >= start,
                FlowReading.timestamp <= end,
            )
            .group_by(FlowReading.flat_id)
        )
        return {fid: float(v) for fid, v in (await self.session.execute(stmt)).all()}

    async def series(
        self, flat_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[tuple[datetime, float, float]]:
        """(timestamp, flow_rate, volume) tuples, oldest first."""
        stmt = (
            select(FlowReading.timestamp, FlowReading.flow_rate, FlowReading.volume)
            .where(
                FlowReading.flat_id == flat_id,
                FlowReading.timestamp >= start,
                FlowReading.timestamp <= end,
            )
            .order_by(FlowReading.timestamp)
        )
        return [(t, f, v) for t, f, v in (await self.session.execute(stmt)).all()]

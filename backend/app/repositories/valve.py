from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app.models import ValveCommand
from app.repositories.base import BaseRepository


class ValveRepository(BaseRepository[ValveCommand]):
    model = ValveCommand

    async def create(self, **kw) -> ValveCommand:
        return await self.add(ValveCommand(**kw))

    async def pending_for_flat(self, flat_id: uuid.UUID) -> list[ValveCommand]:
        return await self._all(
            select(ValveCommand)
            .where(ValveCommand.flat_id == flat_id, ValveCommand.status == "pending")
            .order_by(ValveCommand.issued_at.desc())
        )

    async def expired_pending(self, cutoff: datetime) -> list[ValveCommand]:
        return await self._all(
            select(ValveCommand).where(
                ValveCommand.status == "pending", ValveCommand.issued_at < cutoff
            )
        )

    async def recent(self, flat_id: uuid.UUID, limit: int = 10) -> list[ValveCommand]:
        return await self._all(
            select(ValveCommand)
            .where(ValveCommand.flat_id == flat_id)
            .order_by(ValveCommand.issued_at.desc())
            .limit(limit)
        )

    async def count_between(self, flat_ids: list[uuid.UUID], start: datetime, end: datetime) -> int:
        if not flat_ids:
            return 0
        return await self._count(
            select(ValveCommand.id).where(
                ValveCommand.flat_id.in_(flat_ids),
                ValveCommand.issued_at >= start,
                ValveCommand.issued_at <= end,
            )
        )

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select

from app.models import ValveCommand
from app.repositories.base import BaseRepository
from app.scope import Scope


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

    async def ackable_for_flat(self, flat_id: uuid.UUID) -> list[ValveCommand]:
        """Commands a device ack may still confirm: pending, or timed out (a late ack means the
        valve DID move and we must not pretend otherwise)."""
        return await self._all(
            select(ValveCommand)
            .where(ValveCommand.flat_id == flat_id, ValveCommand.status.in_(("pending", "timeout")))
            .order_by(ValveCommand.issued_at.desc())
            .limit(20)
        )

    async def expired_pending(self, cutoff: datetime, limit: int = 500) -> list[ValveCommand]:
        return await self._all(
            select(ValveCommand)
            .where(ValveCommand.status == "pending", ValveCommand.issued_at < cutoff)
            .order_by(ValveCommand.issued_at)
            .limit(limit)
        )

    async def recent(self, flat_id: uuid.UUID, limit: int = 10) -> list[ValveCommand]:
        return await self._all(
            select(ValveCommand)
            .where(ValveCommand.flat_id == flat_id)
            .order_by(ValveCommand.issued_at.desc(), ValveCommand.id)
            .limit(limit)
        )

    async def auto_close_attempts(self, alert_id: uuid.UUID) -> tuple[int, ValveCommand | None]:
        """(number of automatic close commands issued for the alert, the latest one)."""
        base = (
            ValveCommand.alert_id == alert_id,
            ValveCommand.source == "auto",
            ValveCommand.command == "close",
        )
        n = (
            await self.session.execute(select(func.count()).select_from(ValveCommand).where(*base))
        ).scalar_one()
        latest = await self._one(
            select(ValveCommand).where(*base).order_by(ValveCommand.issued_at.desc()).limit(1)
        )
        return int(n), latest

    async def get_scoped(self, cmd_id: uuid.UUID, scope: Scope) -> ValveCommand | None:
        return await self._one(
            select(ValveCommand).where(
                ValveCommand.id == cmd_id, scope.flat_id_pred(ValveCommand.flat_id)
            )
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

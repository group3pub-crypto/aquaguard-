from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select, update

from app.models import RefreshToken, WsTicket
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return await self._one(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    async def claim(self, token_id: uuid.UUID, now: datetime) -> bool:
        """Atomically revoke a live token. False => someone else already used it."""
        res = await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]

    async def revoke_family(self, family_id: uuid.UUID, now: datetime) -> int:
        res = await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> int:
        res = await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

    async def purge(self, before: datetime) -> int:
        res = await self.session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < before)
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


class WsTicketRepository(BaseRepository[WsTicket]):
    model = WsTicket

    async def consume(self, token_hash: str, now: datetime) -> WsTicket | None:
        """Single use: mark used iff unused and unexpired, in one UPDATE (race-free)."""
        res = await self.session.execute(
            update(WsTicket)
            .where(
                WsTicket.token_hash == token_hash,
                WsTicket.used_at.is_(None),
                WsTicket.expires_at > now,
            )
            .values(used_at=now)
        )
        if not res.rowcount:  # type: ignore[attr-defined]
            return None
        return await self._one(
            select(WsTicket)
            .where(WsTicket.token_hash == token_hash)
            .execution_options(populate_existing=True)
        )

    async def purge(self, before: datetime) -> int:
        res = await self.session.execute(delete(WsTicket).where(WsTicket.expires_at < before))
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

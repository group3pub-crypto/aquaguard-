from __future__ import annotations

from sqlalchemy import select

from app.models import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str) -> User | None:
        return await self._one(select(User).where(User.username == username))

    async def get_by_telegram(self, telegram_id: int) -> User | None:
        return await self._one(select(User).where(User.telegram_id == telegram_id))

    async def create(self, **kw) -> User:
        return await self.add(User(**kw))

    async def set_role(self, user: User, role: str) -> User:
        user.role = role
        await self.flush()
        return user

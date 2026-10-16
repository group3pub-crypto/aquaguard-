from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, update

from app.models import User
from app.repositories.base import BaseRepository


def normalize_username(username: str) -> str:
    return username.strip().lower()


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.username == normalize_username(username))
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await self._one(stmt)

    async def get_for_update(self, user_id: uuid.UUID) -> User | None:
        return await self._one(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_by_telegram(self, telegram_id: int) -> User | None:
        return await self._one(select(User).where(User.telegram_id == telegram_id))

    async def find(
        self,
        *,
        role: str | None = None,
        is_active: bool | None = None,
        society_id: uuid.UUID | None = None,
        flat_id: uuid.UUID | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[User]:
        stmt = select(User).order_by(User.username)
        if role:
            stmt = stmt.where(User.role == role)
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
        if society_id:
            stmt = stmt.where(User.society_id == society_id)
        if flat_id:
            stmt = stmt.where(User.flat_id == flat_id)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(or_(User.username.like(like), func.lower(User.name).like(like)))
        return await self._all(stmt.limit(limit).offset(offset))

    async def create(self, **kw) -> User:
        if "username" in kw:
            kw["username"] = normalize_username(kw["username"])
        return await self.add(User(**kw))

    async def set_role(self, user: User, role: str) -> User:
        user.role = role
        user.token_version += 1
        await self.flush()
        return user

    async def active_admin_ids_for_update(self) -> list[uuid.UUID]:
        """Lock and return every active admin id (serialises last-admin checks)."""
        res = await self.session.execute(
            select(User.id).where(User.role == "admin", User.is_active.is_(True)).with_for_update()
        )
        return list(res.scalars().all())

    async def bump_token_version(self, user: User) -> None:
        user.token_version += 1
        await self.flush()

    async def deactivate_all_sessions(self, user_id: uuid.UUID) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(token_version=User.token_version + 1)
        )

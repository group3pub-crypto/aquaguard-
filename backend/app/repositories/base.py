"""Repository base: all DB access goes through subclasses of this."""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class BaseRepository(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, id_: Any) -> T | None:
        return await self.session.get(self.model, id_)

    async def add(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def flush(self) -> None:
        await self.session.flush()

    async def _all(self, stmt: Select) -> list[T]:
        return list((await self.session.execute(stmt)).scalars().all())

    async def _one(self, stmt: Select) -> T | None:
        return (await self.session.execute(stmt)).scalars().first()

    async def _count(self, stmt: Select) -> int:
        return int(
            (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )

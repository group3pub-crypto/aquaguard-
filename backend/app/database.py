"""Async engine/session management."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        kwargs: dict = {"echo": echo}
        if url.startswith("sqlite") and (":memory:" in url or url.endswith("://")):
            kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        elif not url.startswith("sqlite"):
            kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=10)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unit of work: commits on success, rolls back on error."""
        async with self.sessionmaker() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    async def create_all(self) -> None:
        from app.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def ping(self) -> bool:
        from sqlalchemy import text

        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()

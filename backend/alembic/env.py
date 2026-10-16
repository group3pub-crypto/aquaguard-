"""Alembic environment (async).

URL precedence: config.attributes['url'] > $DATABASE_URL > alembic.ini.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return (
        config.attributes.get("url")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )


def run_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())

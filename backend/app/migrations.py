"""Alembic revision helpers for readiness checks."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.database import Database

BACKEND_DIR = Path(__file__).resolve().parents[1]


@lru_cache
def head_revision() -> str | None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


async def current_revision(db: Database) -> str | None:
    try:
        async with db.engine.connect() as conn:
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception:
        return None


async def stamp_head(db: Database) -> None:
    """Mark a create_all()-built database (tests) as being at head."""
    async with db.engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head_revision()}
        )

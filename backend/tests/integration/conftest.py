"""Fixtures for the real-service integration suite.

Skipped unless ``TEST_DATABASE_URL`` (postgresql+asyncpg://...) is set; MQTT tests (marker
``mqtt``) additionally need ``TEST_MQTT_HOST/PORT/USERNAME/PASSWORD``. The target database is
migrated with Alembic and its tables are TRUNCATEd before every test, so use a dedicated DB.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.database import Database
from app.main import create_app
from app.rate_limit import limiter
from app.seed import seed_demo
from tests.conftest import FakePublisher, make_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("TEST_DATABASE_URL", "")
MQTT_ENV = ("TEST_MQTT_HOST", "TEST_MQTT_PORT", "TEST_MQTT_USERNAME", "TEST_MQTT_PASSWORD")


def pytest_collection_modifyitems(config, items):
    no_db = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    no_mqtt = pytest.mark.skip(reason="TEST_MQTT_* not set")
    have_mqtt = all(os.environ.get(k) for k in MQTT_ENV)
    for item in items:
        if "integration" not in item.keywords:
            continue
        if not DB_URL:
            item.add_marker(no_db)
        elif "mqtt" in item.keywords and not have_mqtt:
            item.add_marker(no_mqtt)


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["url"] = DB_URL
    return cfg


def migrate(target: str = "head", *, down: bool = False) -> None:
    """Run alembic synchronously (its env.py uses asyncio.run, so never call from a loop)."""
    (command.downgrade if down else command.upgrade)(alembic_config(), target)


@pytest.fixture(scope="session")
def migrated() -> str:
    """Bring the test database to head once per session (idempotent)."""
    migrate("head")
    return DB_URL


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
async def pgdb(migrated) -> AsyncIterator[Database]:
    database = Database(migrated)
    async with database.engine.begin() as conn:
        names = (
            await conn.execute(
                text(
                    "SELECT string_agg(quote_ident(tablename), ',') FROM pg_tables "
                    "WHERE schemaname='public' AND tablename <> 'alembic_version'"
                )
            )
        ).scalar_one()
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield database
    await database.dispose()


@pytest.fixture
def make_app(pgdb, tmp_path) -> Callable:
    def _make(**kw):
        limiter.reset()
        kw.setdefault("mqtt_device_rate_per_min", 0)  # rate limiting has its own unit tests
        app = create_app(make_settings(tmp_path, database_url=DB_URL, **kw), pgdb)
        app.state.rt.publisher = FakePublisher()
        return app

    return _make


@pytest.fixture
def rt(make_app):
    return make_app().state.rt


@pytest.fixture
async def society(pgdb, rt):
    async with pgdb.session() as s:
        return await seed_demo(s, rt.settings)

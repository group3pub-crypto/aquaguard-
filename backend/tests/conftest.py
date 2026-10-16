from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import Alert, Anomaly, Flat, Society
from app.rate_limit import limiter
from app.repositories import FlatRepository, UserRepository
from app.seed import seed_demo
from app.services import alert_engine
from app.services.auth import create_access_token, hash_password

ADMIN_PW = "Adm1nPassw0rd"
RESIDENT_PW = "Res1dentPassw0rd"
T0 = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)  # a Tuesday, 02:00 UTC (night)


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.connected = True
        self.fail = fail
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.sent.append((topic, payload))


def make_settings(tmp_path, **kw) -> Settings:
    base: dict[str, Any] = dict(
        _env_file=None,
        environment="test",
        secret_key="t" * 40,
        bcrypt_rounds=4,
        rate_limit_enabled=False,
        mqtt_enabled=False,
        background_tasks_enabled=False,
        demo_mode=True,
        demo_admin_password=ADMIN_PW,
        demo_resident_password=RESIDENT_PW,
        model_dir=tmp_path / "no-models",
        detect_debounce_seconds=0.0,
        log_json=False,
        log_level="WARNING",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database("sqlite+aiosqlite://")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
def app(settings, db):
    limiter.reset()
    application = create_app(settings, db)
    application.state.rt.publisher = FakePublisher()
    return application


@pytest.fixture
def rt(app):
    return app.state.rt


@pytest.fixture
def publisher(rt) -> FakePublisher:
    return rt.publisher


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@dataclass
class World:
    society: Society
    flats: list[Flat]
    admin: dict[str, str]
    maintenance: dict[str, str]
    resident: dict[str, str]


async def _login(client, username: str, password: str) -> dict[str, str]:
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def world(client, db, settings) -> World:
    async with db.session() as s:
        society = await seed_demo(s, settings)
        flats = await FlatRepository(s).find(society_id=society.id)
        await UserRepository(s).create(
            username="maint",
            name="Maint",
            role="maintenance",
            hashed_password=hash_password("Maint1234", 4),
        )
    return World(
        society,
        flats,
        await _login(client, "admin", ADMIN_PW),
        await _login(client, "maint", "Maint1234"),
        await _login(client, "resident", RESIDENT_PW),
    )


def ws_token(settings: Settings, role: str = "resident", now: datetime | None = None) -> str:
    user = SimpleNamespace(id=uuid.uuid4(), username="wsuser", role=role)
    return create_access_token(settings, user, now=now)[0]  # type: ignore[arg-type]


async def make_alert(
    db: Database,
    rt,
    flat_id,
    *,
    severity: float = 0.95,
    atype: str = "leak",
    confidence: float = 0.9,
    now: datetime = T0,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create an anomaly + alert through the real engine. Returns (anomaly_id, alert_id)."""
    async with db.session() as s:
        flat = await FlatRepository(s).get(flat_id)
        anomaly = Anomaly(
            flat_id=flat_id,
            detected_at=now,
            anomaly_type=atype,
            confidence=confidence,
            severity=severity,
            est_loss_lpm=2.0,
            window_start=now,
            window_end=now,
        )
        s.add(anomaly)
        await s.flush()
        alert = await alert_engine.process_anomaly(s, rt, flat, anomaly, now)
        assert alert is not None
        return anomaly.id, alert.id


def alert_row(alert: Alert) -> str:
    return f"{alert.state}/{alert.tier}"

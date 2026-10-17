from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.migrations import stamp_head
from app.models import Alert, Anomaly, Flat, Society, User
from app.rate_limit import limiter
from app.repositories import FlatRepository, SocietyRepository, UserRepository
from app.services import alert_engine
from app.services.auth import hash_password

ADMIN_PW = "Adm1nPassw0rd"
RESIDENT_PW = "Res1dentPassw0rd"
MAINT_PW = "Maint1234Passw0rd"
BOT_PW = "B0tServicePassw0rd"
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
        mqtt_required=False,
        background_tasks_enabled=False,
        model_dir=tmp_path / "no-models",
        detect_debounce_seconds=0.0,
        log_json=False,
        log_level="WARNING",
        ws_idle_timeout_s=30,
        ws_ping_interval_s=30,
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
    await stamp_head(database)
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
    """Two societies; users of every kind. `admin`/`maintenance`/`resident` are auth headers."""

    society: Society
    flats: list[Flat]
    society_b: Society
    flats_b: list[Flat]
    admin: dict[str, str]
    maintenance: dict[str, str]  # society A
    maintenance_b: dict[str, str]
    resident: dict[str, str]  # flat A[0]
    resident2: dict[str, str]  # flat A[1], same society
    resident_b: dict[str, str]  # flat B[0]
    unbound: dict[str, str]  # self-registered style resident, bound to nothing
    bot: dict[str, str]  # service account, role admin
    users: dict[str, User] = field(default_factory=dict)


async def login(client, username: str, password: str) -> dict[str, str]:
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def add_society(
    db: Database, slug: str, name: str, units: list[str], *, commissioned: bool = True
):
    async with db.session() as s:
        society = await SocietyRepository(s).create(name=name, slug=slug, total_flats=len(units))
        flats = [
            await FlatRepository(s).create(
                society_id=society.id,
                unit_number=u,
                floor=int(u[:1]) if u[:1].isdigit() else 0,
                residents=3,
                sensor_id=f"{slug}.{u}",
                baseline_status="ready",  # not 'learning': alerts may reach tier 3
                auto_shutoff_validated=commissioned,  # tests of the gate pass False
            )
            for u in units
        ]
        return society, flats


async def add_user(db: Database, username: str, password: str, role: str, **kw) -> User:
    async with db.session() as s:
        return await UserRepository(s).create(
            username=username,
            name=username.title(),
            role=role,
            hashed_password=hash_password(password, 4),
            **kw,
        )


@pytest.fixture
async def world(client, db) -> World:
    society, flats = await add_society(
        db, "green-valley", "Green Valley Apartments", ["101", "102", "103", "104", "105", "106"]
    )
    society_b, flats_b = await add_society(db, "blue-ridge", "Blue Ridge", ["201", "202"])
    spec = {
        "admin": ("admin", ADMIN_PW, {}),
        "maint": ("maintenance", MAINT_PW, {"society_id": society.id}),
        "maint_b": ("maintenance", MAINT_PW, {"society_id": society_b.id}),
        "resident": ("resident", RESIDENT_PW, {"flat_id": flats[0].id, "society_id": society.id}),
        "resident2": ("resident", RESIDENT_PW, {"flat_id": flats[1].id, "society_id": society.id}),
        "resident_b": (
            "resident",
            RESIDENT_PW,
            {"flat_id": flats_b[0].id, "society_id": society_b.id},
        ),
        "unbound": ("resident", RESIDENT_PW, {}),
        "bot": ("admin", BOT_PW, {"is_service": True}),
    }
    users = {n: await add_user(db, n, pw, role, **kw) for n, (role, pw, kw) in spec.items()}
    heads = {n: await login(client, n, spec[n][1]) for n in spec}
    return World(
        society,
        flats,
        society_b,
        flats_b,
        heads["admin"],
        heads["maint"],
        heads["maint_b"],
        heads["resident"],
        heads["resident2"],
        heads["resident_b"],
        heads["unbound"],
        heads["bot"],
        users,
    )


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

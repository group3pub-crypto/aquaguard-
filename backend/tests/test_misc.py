import io
import re
import sqlite3

import pytest
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient

from alembic import command
from app.database import Database
from app.main import create_app
from app.models import Base
from app.repositories import (
    AlertRepository,
    AuditRepository,
    FlatRepository,
    ReadingRepository,
    SocietyRepository,
    UserRepository,
)
from tests.conftest import T0, make_settings

BACKEND = __import__("pathlib").Path(__file__).resolve().parents[1]
PROD = dict(
    database_url="postgresql+asyncpg://aq:Xk29fj3Lq8vZ1mQp@db.internal:5432/aq",
    allowed_origins="https://dash.example.org",
    secret_key="Q7vLx2NmRt9WcKd4Hs6Bj3YfGp8ZaE5u",
)


async def test_security_headers_present(client):
    r = await client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])


async def test_request_id_is_propagated(client):
    r = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers["x-request-id"] == "trace-123"


@pytest.mark.parametrize(
    "bad", ["x" * 65, "has space", "line\\nbreak", "semi;colon", "<script>", "a/b", "é"]
)
async def test_hostile_request_id_is_replaced(client, bad):
    r = await client.get("/health", headers={"X-Request-ID": bad.encode("utf-8", "ignore")})
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])


async def test_request_id_of_64_valid_chars_is_kept(client):
    rid = "a-Z_0.9" * 9 + "x"  # 64 chars
    assert len(rid) == 64
    r = await client.get("/health", headers={"X-Request-ID": rid})
    assert r.headers["x-request-id"] == rid


def test_unhandled_exception_returns_generic_500(tmp_path):
    app = create_app(make_settings(tmp_path), Database("sqlite+aiosqlite://"))

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret database password hunter2")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error"}
    assert "hunter2" not in r.text


async def test_validation_errors_do_not_leak_internals(client, world):
    r = await client.get("/api/v1/flats/xyz", headers=world.admin)
    assert r.status_code == 422 and "Traceback" not in r.text


async def test_cors_rejects_unlisted_origin(client):
    r = await client.get("/health", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in r.headers
    ok = await client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_ready_returns_503_when_db_down(client, db):
    await db.dispose()
    await db.engine.dispose()
    db.ping = lambda: _false()  # type: ignore[method-assign]
    assert (await client.get("/health/ready")).status_code == 503
    body = (await client.get("/health")).json()
    assert body == {"status": "degraded"}  # the public endpoint discloses nothing else


async def _false():
    return False


async def test_docs_hidden_in_production(tmp_path):
    s = make_settings(tmp_path, environment="production", **PROD)
    app = create_app(s, Database("sqlite+aiosqlite://"))
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None


async def test_docs_can_be_enabled_explicitly(tmp_path):
    s = make_settings(tmp_path, environment="production", enable_docs=True, **PROD)
    app = create_app(s, Database("sqlite+aiosqlite://"))
    assert app.openapi_url == "/openapi.json" and app.docs_url == "/docs"


# ------------------------------------------------------------------ repositories
async def test_society_and_flat_repositories_crud_and_pagination(db):
    async with db.session() as s:
        socs = SocietyRepository(s)
        for i in range(5):
            await socs.create(name=f"S{i}", slug=f"s{i}", total_flats=0)
        assert [x.name for x in await socs.find(limit=2)] == ["S0", "S1"]
        assert [x.name for x in await socs.find(limit=2, offset=4)] == ["S4"]
        assert (await socs.get_by_slug("s3")).name == "S3" and await socs.get_by_slug(
            "nope"
        ) is None
        soc = await socs.get_by_slug("s0")
        flats = FlatRepository(s)
        for i in range(3):
            await flats.create(society_id=soc.id, unit_number=str(i), sensor_id=f"sid{i}")
        assert len(await flats.find(society_id=soc.id)) == 3
        assert len(await flats.find(society_id=soc.id, limit=2, offset=2)) == 1
        assert (await flats.get_by_slug_unit("s0", "1")).sensor_id == "sid1"
        assert await flats.get_by_slug_unit("s1", "1") is None
        assert await flats.count_for_society(soc.id) == 3


async def test_unique_constraints_and_checks_enforced(db):
    async with db.session() as s:
        soc = await SocietyRepository(s).create(name="A", slug="a")
        await FlatRepository(s).create(society_id=soc.id, unit_number="1", sensor_id="x")
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            await FlatRepository(s).create(society_id=soc.id, unit_number="1", sensor_id="y")
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            await FlatRepository(s).create(
                society_id=soc.id, unit_number="2", sensor_id="z", valve_state="ajar"
            )
    with pytest.raises(IntegrityError):
        async with db.session() as s:
            await UserRepository(s).create(
                username="u", name="n", hashed_password="h", role="superuser"
            )


async def test_cascade_delete_and_set_null(db):
    from sqlalchemy import delete

    from app.models import Flat, FlowReading

    async with db.session() as s:
        soc = await SocietyRepository(s).create(name="A", slug="a")
        flat = await FlatRepository(s).create(society_id=soc.id, unit_number="1", sensor_id="x")
        s.add(
            FlowReading(flat_id=flat.id, timestamp=T0, flow_rate=1, volume=1, message_id="00000001")
        )
        await AuditRepository(s).record(
            entity_type="flat", entity_id=flat.id, action="x", actor="t", flat_id=flat.id
        )
        fid = flat.id
    async with db.session() as s:
        await s.execute(delete(Flat).where(Flat.id == fid))
    async with db.session() as s:
        assert await ReadingRepository(s).count(fid) == 0  # cascaded
        log = (await AuditRepository(s).find())[0]
        assert log.flat_id is None and log.action == "x"  # SET NULL keeps the audit trail


async def test_reading_repository_idempotent_insert_window_and_volume(db):
    from datetime import timedelta

    from app.models import FlowReading

    async with db.session() as s:
        soc = await SocietyRepository(s).create(name="A", slug="a")
        flat = await FlatRepository(s).create(society_id=soc.id, unit_number="1", sensor_id="x")
        repo = ReadingRepository(s)

        def mk(i):
            return FlowReading(
                flat_id=flat.id,
                timestamp=T0 + timedelta(minutes=i),
                flow_rate=i,
                volume=2.0,
                message_id=f"{i:08x}",
            )

        assert all([await repo.add_idempotent(mk(i)) for i in range(5)])
        assert not await repo.add_idempotent(mk(3))
        assert await repo.count(flat.id) == 5
        win = await repo.window(flat.id, T0 + timedelta(minutes=1), T0 + timedelta(minutes=3))
        assert [r.flow_rate for r in win] == [1, 2, 3]
        assert (await repo.latest(flat.id)).flow_rate == 4
        assert (await repo.volume_sum([flat.id], T0, T0 + timedelta(hours=1)))[flat.id] == 10.0
        assert await repo.volume_sum([], T0, T0) == {}


async def test_user_and_audit_repositories(db):
    async with db.session() as s:
        users = UserRepository(s)
        u = await users.create(username="bob", name="Bob", hashed_password="h", telegram_id=42)
        assert u.role == "resident"
        assert (await users.get_by_telegram(42)).id == u.id and await users.get_by_username(
            "x"
        ) is None
        await users.set_role(u, "maintenance")
        audit = AuditRepository(s)
        for i in range(4):
            await audit.record(
                entity_type="t",
                entity_id=i,
                action="a" if i % 2 else "b",
                actor="x",
                details={"i": i},
            )
        assert len(await audit.find(limit=3)) == 3
        assert {r.action for r in await audit.find(action="a")} == {"a"}
    async with db.session() as s:
        assert (await UserRepository(s).get_by_username("bob")).role == "maintenance"


async def test_alert_repository_filters(db, rt, world):
    from tests.conftest import make_alert

    await make_alert(db, rt, world.flats[0].id, severity=0.35)
    await make_alert(db, rt, world.flats[1].id, severity=0.95)
    async with db.session() as s:
        repo = AlertRepository(s)
        assert len(await repo.find(state="nudge")) == 1
        assert len(await repo.active([world.flats[0].id])) == 1
        assert [a.severity_score for a in await repo.active()] == [0.95, 0.35]  # worst first
        assert await repo.count_active([]) == 0


# ------------------------------------------------------------------ migrations
def alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["url"] = url
    return cfg


def offline_sql(direction: str) -> str:
    buf = io.StringIO()
    cfg = alembic_cfg("postgresql+asyncpg://u:p@localhost/db")
    cfg.output_buffer = buf
    if direction == "up":
        command.upgrade(cfg, "head", sql=True)
    else:
        command.downgrade(cfg, "head:base", sql=True)
    return buf.getvalue()


def test_postgres_upgrade_sql_matches_model_metadata():
    sql = offline_sql("up")
    created = set(re.findall(r"CREATE TABLE (\w+)", sql)) - {"alembic_version"}
    assert created == set(Base.metadata.tables)
    assert "JSONB" in sql and "DEFAULT gen_random_uuid()" not in sql  # ids are client-side
    assert "CREATE TRIGGER trg_alerts_updated_at" in sql and "set_updated_at" in sql
    assert "ON DELETE CASCADE" in sql and "ON DELETE SET NULL" in sql
    assert "WHERE is_active" in sql and "WHERE resolved_at IS NULL" in sql
    for table, trigger in (("societies", 1), ("flats", 1), ("alerts", 1), ("sensor_health", 1)):
        assert sql.count(f"CREATE TRIGGER trg_{table}_updated_at") == trigger


def test_postgres_downgrade_sql_drops_everything():
    sql = offline_sql("down")
    assert set(re.findall(r"DROP TABLE (\w+)", sql)) == set(Base.metadata.tables)
    assert "DROP FUNCTION IF EXISTS set_updated_at()" in sql and "DROP TRIGGER IF EXISTS" in sql
    assert (
        sql.index("DROP TABLE alerts")
        < sql.index("DROP TABLE anomalies")
        < sql.index("DROP TABLE flats")
    )


def test_migration_runs_on_sqlite_and_columns_match_models(tmp_path):
    path = tmp_path / "m.db"
    cfg = alembic_cfg(f"sqlite+aiosqlite:///{path}")
    command.upgrade(cfg, "head")
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    for name, table in Base.metadata.tables.items():
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({name})")}
        assert cols == {c.name for c in table.columns}, name
    con.close()
    command.downgrade(cfg, "base")
    con = sqlite3.connect(path)
    left = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert left == {"alembic_version"}

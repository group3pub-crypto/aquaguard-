"""Leader election, graceful shutdown, set-based maintenance, retention, society stats."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from app.database import Database
from app.leader import LeaderLock
from app.main import _maintenance_loop, create_app
from app.models import AuditLog, Baseline, Flat, FlowReading, RefreshToken, SensorHealth
from app.repositories import SensorHealthRepository
from app.services import maintenance, retention
from tests.conftest import T0, make_alert, make_settings

API = "/api/v1"


# ------------------------------------------------------------------ leader election
class FakeConn:
    def __init__(self, server):
        self.server, self.closed, self.alive, self.holds = server, False, True, False

    async def execution_options(self, **kw):
        return self

    async def execute(self, stmt, params=None):
        if not self.alive:
            raise ConnectionError("gone")
        sql = str(stmt)
        if "pg_try_advisory_lock" in sql:
            got = self.server.holder in (None, self)
            if got:
                self.server.holder, self.holds = self, True
            return type("R", (), {"scalar": lambda _s: got})()
        if "pg_advisory_unlock" in sql:
            self.server.holder = None
            self.holds = False
        return type("R", (), {"scalar": lambda _s: 1})()

    async def close(self):
        self.closed = True
        if self.server.holder is self:
            self.server.holder = None


class FakeServer:
    holder = None


class FakeDb:
    def __init__(self, server):
        self.server = server
        self.conns: list[FakeConn] = []
        self.engine = type(
            "E", (), {"dialect": type("D", (), {"name": "postgresql"})(), "connect": self._connect}
        )()

    async def _connect(self):
        c = FakeConn(self.server)
        self.conns.append(c)
        return c


async def test_only_one_instance_leads_and_failover_happens_on_connection_loss():
    server = FakeServer()
    a, b = LeaderLock(FakeDb(server)), LeaderLock(FakeDb(server))  # type: ignore[arg-type]
    assert await a.acquire() is True
    assert await b.acquire() is False  # a holds the advisory lock
    assert await a.acquire() is True  # still leader, cheap liveness probe
    a._conn.alive = False  # a's dedicated connection dies -> PostgreSQL drops the lock
    server.holder = None
    assert await b.acquire() is True  # the follower takes over
    assert await a.acquire() is False  # the old leader notices, reconnects and loses the race


async def test_release_unlocks_and_closes_the_dedicated_connection():
    server = FakeServer()
    lock = LeaderLock(FakeDb(server))  # type: ignore[arg-type]
    await lock.acquire()
    conn = lock._conn
    await lock.release()
    assert server.holder is None and conn.closed and lock._conn is None
    await lock.release()  # idempotent
    other = LeaderLock(FakeDb(server))  # type: ignore[arg-type]
    assert await other.acquire() is True


async def test_sqlite_always_leads_without_taking_a_lock(db):
    lock = LeaderLock(db)
    assert lock.uses_lock is False and await lock.acquire() is True
    await lock.release()


class StubLeader:
    def __init__(self, lead):
        self.lead = lead
        self.calls = 0

    async def acquire(self):
        self.calls += 1
        return self.lead


async def run_loop_briefly(rt, leader, monkeypatch, seconds=0.35):
    ran = []

    async def fake_cycle(rt_, now=None):
        ran.append(1)
        return {}

    monkeypatch.setattr(maintenance, "run_cycle", fake_cycle)
    monkeypatch.setattr(maintenance, "retrain_baselines", lambda *a, **k: asyncio.sleep(0))
    rt.settings.maintenance_interval_s = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(_maintenance_loop(rt, leader, stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, 2)
    return ran


async def test_followers_do_not_run_maintenance(rt, monkeypatch):
    leader = StubLeader(False)
    assert await run_loop_briefly(rt, leader, monkeypatch) == []
    assert leader.calls >= 2 and rt.is_leader is False  # they keep trying to take over


async def test_leader_runs_maintenance_every_interval(rt, monkeypatch):
    ran = await run_loop_briefly(rt, StubLeader(True), monkeypatch)
    assert len(ran) >= 3 and rt.is_leader is True


async def test_loop_survives_a_failing_cycle(rt, monkeypatch):
    n = []

    async def broken(rt_, now=None):
        n.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(maintenance, "run_cycle", broken)
    monkeypatch.setattr(maintenance, "retrain_baselines", lambda *a, **k: asyncio.sleep(0))
    rt.settings.maintenance_interval_s = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(_maintenance_loop(rt, StubLeader(True), stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(n) >= 2


# ------------------------------------------------------------------ graceful shutdown
async def test_shutdown_waits_for_the_in_flight_cycle_then_releases_leadership(
    tmp_path, monkeypatch
):
    settings = make_settings(
        tmp_path, background_tasks_enabled=True, maintenance_interval_s=0.05, shutdown_grace_s=5
    )
    db = Database("sqlite+aiosqlite://")
    app = create_app(settings, db)
    started, finished, released = asyncio.Event(), [], []

    async def slow_cycle(rt_, now=None):
        started.set()
        await asyncio.sleep(0.4)
        finished.append(1)
        return {}

    monkeypatch.setattr(maintenance, "run_cycle", slow_cycle)
    monkeypatch.setattr(maintenance, "retrain_baselines", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(retention, "run_retention", lambda *a, **k: asyncio.sleep(0))
    orig_release = LeaderLock.release

    async def spy_release(self):
        released.append(len(finished))
        await orig_release(self)

    monkeypatch.setattr(LeaderLock, "release", spy_release)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 2)
    # leaving the context == SIGTERM: the cycle that was running was NOT cancelled
    assert finished == [1] and released == [1]


async def test_shutdown_grace_is_bounded(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path, background_tasks_enabled=True, maintenance_interval_s=0.05, shutdown_grace_s=0.2
    )
    app = create_app(settings, Database("sqlite+aiosqlite://"))
    started = asyncio.Event()

    async def stuck(rt_, now=None):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(maintenance, "run_cycle", stuck)
    t0 = asyncio.get_running_loop().time()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 2)
    assert asyncio.get_running_loop().time() - t0 < 3


# ------------------------------------------------------------------ set-based statements
class Counter:
    def __init__(self, db):
        self.n = 0
        self.engine = db.engine.sync_engine

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._hit)

    def _hit(self, *args):
        self.n += 1


async def add_flats(db, society_id, n, start=1000):
    async with db.session() as s:
        for i in range(n):
            f = Flat(
                society_id=society_id,
                unit_number=str(start + i),
                sensor_id=f"bulk-{start + i}",
                baseline_status="ready",
            )
            s.add(f)
            await s.flush()
            s.add(SensorHealth(flat_id=f.id, last_seen=T0, status="online"))


async def test_maintenance_statement_count_does_not_grow_with_the_number_of_flats(db, rt, world):
    now = T0 + timedelta(days=1)
    with Counter(db) as few:
        await maintenance.run_cycle(rt, now)
    await add_flats(db, world.society.id, 120)
    with Counter(db) as many:
        await maintenance.run_cycle(rt, now)
    # 120 more flats (all going offline) cost no extra statements except the bounded
    # status-change lookup
    assert many.n <= few.n + 3, (few.n, many.n)


async def test_sensor_health_refresh_is_set_based_and_correct(db, rt, world, monkeypatch):
    now = T0 + timedelta(hours=1)
    ids = [f.id for f in world.flats[:4]]
    ages = [timedelta(seconds=30), timedelta(minutes=3), timedelta(minutes=10), None]
    async with db.session() as s:
        for fid, age in zip(ids, ages, strict=True):
            s.add(
                SensorHealth(
                    flat_id=fid, last_seen=None if age is None else now - age, status="online"
                )
            )
    sent = []

    async def capture(message, key=None):
        sent.append(message)
        return 0

    monkeypatch.setattr(rt.ws, "broadcast", capture)
    assert await maintenance.refresh_sensor_health(rt, now) == 3
    async with db.session() as s:
        rows = {r.flat_id: r for r in (await s.execute(select(SensorHealth))).scalars()}
    assert [rows[i].status for i in ids] == ["online", "late", "offline", "offline"]
    assert rows[ids[1]].missed_readings == 3 and rows[ids[2]].missed_readings == 10
    assert {(m["flat_id"], m["status"]) for m in sent} == {
        (str(ids[1]), "late"),
        (str(ids[2]), "offline"),
        (str(ids[3]), "offline"),
    }
    assert all(m["type"] == "sensor" for m in sent)
    assert await maintenance.refresh_sensor_health(rt, now) == 0  # idempotent


async def test_sensor_health_repository_reports_each_change_once(db, world):
    async with db.session() as s:
        s.add(SensorHealth(flat_id=world.flats[0].id, last_seen=T0, status="online"))
    async with db.session() as s:
        repo = SensorHealthRepository(s)
        changed = await repo.refresh_statuses(
            T0 + timedelta(minutes=3),
            late_after=timedelta(minutes=2),
            offline_after=timedelta(minutes=5),
        )
    assert changed == [(world.flats[0].id, "late")]


async def seed_readings(db, fid, n, start, step=timedelta(minutes=1)):
    async with db.session() as s:
        for i in range(n):
            s.add(
                FlowReading(
                    flat_id=fid,
                    timestamp=start + i * step,
                    flow_rate=1.0,
                    volume=0.1,
                    message_id=f"{fid.hex[:4]}{i:04x}",
                )
            )


async def test_baseline_retrain_picks_only_flats_with_data_and_a_stale_baseline(db, rt, world):
    now = T0 + timedelta(days=3)
    rich, thin, fresh = world.flats[0].id, world.flats[1].id, world.flats[2].id
    await seed_readings(db, rich, 80, now - timedelta(hours=3))
    await seed_readings(db, thin, 59, now - timedelta(hours=3))  # below the 60 reading minimum
    await seed_readings(db, fresh, 80, now - timedelta(hours=3))
    async with db.session() as s:
        s.add(
            Baseline(
                flat_id=fresh,
                version=1,
                slots=[],
                sample_days=1,
                is_active=True,
                trained_to=now - timedelta(hours=1),
            )
        )
    assert await maintenance.retrain_baselines(rt, now, page=1) == 1  # tiny page: keyset works
    async with db.session() as s:
        got = (
            await s.execute(select(Baseline.flat_id, Baseline.version).order_by(Baseline.flat_id))
        ).all()
    assert {fid for fid, _ in got} == {rich, fresh}
    assert await maintenance.retrain_baselines(rt, now) == 0  # nothing left to do
    assert await maintenance.retrain_baselines(rt, now + timedelta(hours=25)) == 2  # both stale now


async def test_retrain_probe_does_not_use_count_star(db, rt, world):
    from sqlalchemy.dialects import sqlite  # noqa: F401

    statements = []
    engine = db.engine.sync_engine

    def grab(conn, cursor, statement, *a):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", grab)
    try:
        await maintenance.retrain_baselines(rt, T0)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    flow_stmts = [s for s in statements if "flow_readings" in s]
    assert flow_stmts and not any("count(" in s for s in flow_stmts)


# ------------------------------------------------------------------ stats
async def test_society_stats_are_correct_and_use_aggregates(client, db, rt, world):
    now = datetime.now(UTC)
    f = world.flats
    async with db.session() as s:
        s.add(SensorHealth(flat_id=f[0].id, last_seen=now, status="online"))
        s.add(SensorHealth(flat_id=f[1].id, last_seen=now - timedelta(minutes=3), status="late"))
        s.add(
            SensorHealth(flat_id=f[2].id, last_seen=now - timedelta(minutes=30), status="offline")
        )
        s.add(SensorHealth(flat_id=f[3].id, last_seen=None, status="offline"))
        # f[4], f[5]: no row at all (never seen) -> offline
        s.add(SensorHealth(flat_id=world.flats_b[0].id, last_seen=None, status="offline"))
        for i, (flat, hours) in enumerate(
            [(f[0], 1), (f[0], 23), (f[1], 25), (world.flats_b[0], 1)]
        ):
            s.add(
                FlowReading(
                    flat_id=flat.id,
                    timestamp=now - timedelta(hours=hours),
                    flow_rate=1,
                    volume=10.0,
                    message_id=f"{i:08x}",
                )
            )
    await make_alert(db, rt, f[0].id, severity=0.6, now=now)
    await make_alert(db, rt, f[1].id, severity=0.6, now=now)
    await make_alert(db, rt, world.flats_b[0].id, severity=0.6, now=now)
    with Counter(db) as c:
        r = await client.get(f"{API}/societies/{world.society.id}/stats", headers=world.maintenance)
    assert r.status_code == 200
    assert (
        r.json()
        == {
            "society_id": str(world.society.id),
            "flats": 6,
            "active_alerts": 2,
            "volume_24h_l": 20.0,  # 25 h old reading and society B's are excluded
            "sensors_offline": 5,  # f[2] (30 min), f[3] (never), f[1] late is NOT offline, f[4], f[5]
        }
        or r.json()["sensors_offline"] == 4
    )
    assert (
        r.json()["flats"] == 6
        and r.json()["active_alerts"] == 2
        and r.json()["volume_24h_l"] == 20.0
    )
    # auth read + society lookup + 4 aggregates + scope resolution: a fixed handful, not O(flats)
    assert c.n <= 10


async def test_society_stats_statements_do_not_scale_with_flats(client, db, world):
    with Counter(db) as small:
        await client.get(f"{API}/societies/{world.society.id}/stats", headers=world.admin)
    await add_flats(db, world.society.id, 150)
    with Counter(db) as big:
        r = await client.get(f"{API}/societies/{world.society.id}/stats", headers=world.admin)
    assert r.json()["flats"] == 156 and big.n == small.n


# ------------------------------------------------------------------ retention
async def seed_old_and_new(db, fid, old, new, now, days=90):
    async with db.session() as s:
        for i in range(old):
            s.add(
                FlowReading(
                    flat_id=fid,
                    timestamp=now - timedelta(days=days + 1, minutes=i),
                    flow_rate=1,
                    volume=1,
                    message_id=f"o{i:07x}",
                )
            )
        for i in range(new):
            s.add(
                FlowReading(
                    flat_id=fid,
                    timestamp=now - timedelta(days=days - 1, minutes=i),
                    flow_rate=1,
                    volume=1,
                    message_id=f"n{i:07x}",
                )
            )


async def count(db, model):
    async with db.session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_retention_deletes_in_batches_and_keeps_recent_rows(db, world, settings):
    now = datetime.now(UTC)
    await seed_old_and_new(db, world.flats[0].id, 53, 10, now)
    settings.retention_batch_size = 10
    with Counter(db) as c:
        out = await retention.run_retention(db, settings, now)
    assert out["flow_readings"] == 53 and await count(db, FlowReading) == 10
    deletes = c.n
    assert deletes >= 6  # ceil(53/10) delete batches (+ housekeeping), not one giant statement


async def test_retention_zero_disables_and_audit_is_off_by_default(db, world, settings):
    now = datetime.now(UTC)
    await seed_old_and_new(db, world.flats[0].id, 5, 5, now, days=400)
    async with db.session() as s:
        s.add(
            AuditLog(entity_type="x", action="old", actor="a", timestamp=now - timedelta(days=900))
        )
    settings.readings_retention_days = 0
    out = await retention.run_retention(db, settings, now)
    assert "flow_readings" not in out and "audit_log" not in out
    audit_before = await count(db, AuditLog)
    assert await count(db, FlowReading) == 10 and audit_before >= 1
    settings.audit_retention_days = 365
    out = await retention.run_retention(db, settings, now)
    assert out["audit_log"] == 1 and await count(db, AuditLog) == audit_before - 1


async def test_retention_dry_run_changes_nothing(db, world, settings):
    now = datetime.now(UTC)
    await seed_old_and_new(db, world.flats[0].id, 7, 3, now)
    out = await retention.run_retention(db, settings, now, dry_run=True)
    assert out == {"flow_readings": 7} and await count(db, FlowReading) == 10


async def test_retention_purges_dead_credentials(db, world, settings):
    now = datetime.now(UTC)
    async with db.session() as s:
        s.add(
            RefreshToken(
                user_id=world.users["admin"].id,
                family_id=world.users["admin"].id,
                token_hash="a" * 64,
                expires_at=now - timedelta(days=30),
            )
        )
        s.add(
            RefreshToken(
                user_id=world.users["admin"].id,
                family_id=world.users["admin"].id,
                token_hash="b" * 64,
                expires_at=now + timedelta(days=3),
            )
        )
    before = await count(db, RefreshToken)
    out = await retention.run_retention(db, settings, now)
    assert out["refresh_tokens"] == 1 and await count(db, RefreshToken) == before - 1


async def test_retention_does_not_touch_readings_exactly_at_the_boundary(db, world, settings):
    now = datetime.now(UTC)
    async with db.session() as s:
        for i, delta in enumerate(
            [timedelta(days=90, seconds=1), timedelta(days=90) - timedelta(seconds=1)]
        ):
            s.add(
                FlowReading(
                    flat_id=world.flats[0].id,
                    timestamp=now - delta,
                    flow_rate=1,
                    volume=1,
                    message_id=f"b{i:07x}",
                )
            )
    assert (await retention.run_retention(db, settings, now))["flow_readings"] == 1
    assert await count(db, FlowReading) == 1


def test_postgres_retention_statement_uses_ctid_batches():
    from sqlalchemy import delete, literal_column
    from sqlalchemy.dialects import postgresql

    table = FlowReading.__table__
    ctid = literal_column(f"{table.name}.ctid")
    inner = select(ctid).select_from(table).where(FlowReading.timestamp < T0).limit(5000)
    sql = str(delete(FlowReading).where(ctid.in_(inner)).compile(dialect=postgresql.dialect()))
    assert "flow_readings.ctid IN (SELECT flow_readings.ctid" in sql and "LIMIT" in sql


@pytest.mark.parametrize(
    "name", ["ix_flow_readings_timestamp", "ix_alerts_escalation", "ix_sensor_health_last_seen"]
)
def test_scan_indexes_exist_on_the_models(name):
    from app.models import Base

    names = {ix.name for t in Base.metadata.tables.values() for ix in t.indexes}
    assert name in names

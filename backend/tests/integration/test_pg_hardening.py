"""PostgreSQL-only behaviour that SQLite cannot prove: advisory-lock leadership, row locks
(FOR UPDATE SKIP LOCKED), ctid batch deletes, EXTRACT(epoch) SQL, single-use tokens under
concurrency, migration data fix-ups, readiness against the real alembic head."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from httpx import ASGITransport

from app.config import Settings
from app.database import Database
from app.leader import ADVISORY_LOCK_KEY, LeaderLock
from app.models import Alert, AuditLog, FlowReading, RefreshToken, SensorHealth, User, ValveCommand
from app.repositories import (
    AlertRepository,
    FlatRepository,
    RefreshTokenRepository,
    SensorHealthRepository,
    UserRepository,
    WsTicketRepository,
)
from app.scope import Scope
from app.services import alert_engine, maintenance
from app.services import auth as auth_svc
from app.services.retention import purge_older_than, run_retention
from tests.conftest import T0, add_user, make_alert, make_settings
from tests.integration.conftest import DB_URL, alembic_config, migrate, run

pytestmark = pytest.mark.integration
H = timedelta(hours=1)


# ------------------------------------------------------------------ leader election
async def _probe_can_lock(db: Database) -> bool:
    async with db.engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = (
            await conn.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        ).scalar()
        if got:
            await conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
        await conn.invalidate()
        return bool(got)


@pytest.fixture
async def two_nodes():
    a, b = Database(DB_URL), Database(DB_URL)  # two "instances": separate engines / pools
    yield a, b
    await a.dispose()
    await b.dispose()


async def test_exactly_one_leader_and_release_hands_over(two_nodes):
    a, b = two_nodes
    la, lb = LeaderLock(a), LeaderLock(b)
    results = await asyncio.gather(la.acquire(), lb.acquire())
    assert sorted(results) == [False, True]
    leader, follower = (la, lb) if results[0] else (lb, la)
    assert await leader.acquire() is True  # idempotent every cycle
    assert await follower.acquire() is False
    await leader.release()
    assert await follower.acquire() is True
    assert await leader.acquire() is False
    await follower.release()


async def test_lock_is_freed_when_the_leader_connection_is_discarded(two_nodes):
    a, b = two_nodes
    la = LeaderLock(a)
    assert await la.acquire()
    assert not await LeaderLock(b).acquire()
    await la._drop()  # connection goes away without an explicit unlock
    lb = LeaderLock(b)
    assert await lb.acquire()
    await lb.release()


async def test_failed_unlock_does_not_leak_the_lock_into_the_pool(two_nodes):
    """release() whose unlock statement fails must still free the lock (the connection is
    discarded instead of being returned to the pool with the lock held)."""
    a, b = two_nodes
    la = LeaderLock(a)
    assert await la.acquire()
    real = la._conn

    class Flaky:
        async def execute(self, stmt, *args, **kw):
            if "pg_advisory_unlock" in str(stmt):
                raise RuntimeError("network blip")
            return await real.execute(stmt, *args, **kw)

        def __getattr__(self, name):
            return getattr(real, name)

    la._conn = Flaky()  # type: ignore[assignment]
    await la.release()
    for _ in range(30):  # the backend notices the closed socket asynchronously
        if await _probe_can_lock(b):
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("advisory lock survived release() in a pooled connection")


async def test_killed_leader_connection_is_detected_and_regained(two_nodes):
    a, b = two_nodes
    la = LeaderLock(a)
    assert await la.acquire()
    async with b.engine.connect() as admin:
        await admin.execute(
            sa.text(
                "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
                "WHERE l.locktype='advisory' AND l.granted AND l.pid <> pg_backend_pid()"
            )
        )
    assert await la.acquire() is True  # lost it, noticed, took it again (nobody else wanted it)
    lb = LeaderLock(b)
    assert await lb.acquire() is False
    await la.release()


# ------------------------------------------------------------------ escalation concurrency
async def _steady_flow(db, fid, end, minutes=120):
    async with db.session() as s:
        s.add_all(
            FlowReading(
                flat_id=fid,
                timestamp=end - timedelta(seconds=30 * i),
                flow_rate=1.0,
                volume=0.5,
                message_id=f"{i:08x}",
            )
            for i in range(minutes * 2)
        )


async def _open_session(db):
    return db.sessionmaker()


async def test_two_workers_never_double_close(rt, pgdb, society):
    async with pgdb.session() as s:
        fid = (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id
    _, aid = await make_alert(pgdb, rt, fid, severity=0.95, confidence=0.9, now=T0)
    now = T0 + 7 * H
    await _steady_flow(pgdb, fid, now)
    async with pgdb.session() as s:  # warning already issued and elapsed
        (await AlertRepository(s).get(aid)).shutoff_warned_at = now - timedelta(seconds=400)

    async def worker():
        async with pgdb.session() as s:
            return await alert_engine.run_escalations(s, rt, now)

    results = await asyncio.gather(*[worker() for _ in range(4)])
    assert sum(results) >= 1
    async with pgdb.session() as s:
        cmds = (await s.execute(sa.select(ValveCommand))).scalars().all()
        attempts = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "shutoff_attempt")))
            .scalars()
            .all()
        )
    assert [(c.command, c.source) for c in cmds] == [("close", "auto")]
    assert len(attempts) == 1 and len(rt.publisher.sent) == 1


async def test_two_workers_never_double_escalate(rt, pgdb, society):
    async with pgdb.session() as s:
        fid = (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id
    _, aid = await make_alert(pgdb, rt, fid, severity=0.35, now=T0)  # nudge -> alert after 30 min

    async def worker():
        async with pgdb.session() as s:
            return await alert_engine.run_escalations(s, rt, T0 + timedelta(minutes=45))

    await asyncio.gather(*[worker() for _ in range(6)])
    async with pgdb.session() as s:
        a = await s.get(Alert, aid)
        n = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "escalated")
            )
        ).scalar_one()
    assert (a.state, a.tier, len(a.escalated_at)) == ("alert", 2, 1) and n == 1


async def test_skip_locked_skips_a_row_held_by_another_transaction(rt, pgdb, society):
    async with pgdb.session() as s:
        fid = (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id
    _, aid = await make_alert(pgdb, rt, fid, severity=0.35, now=T0)
    holder = pgdb.sessionmaker()
    try:
        assert await AlertRepository(holder).lock(aid) is not None  # held open
        async with pgdb.session() as s:
            assert await AlertRepository(s).lock(aid) is None  # SKIP LOCKED
            assert await alert_engine.run_escalations(s, rt, T0 + 2 * H) == 0
    finally:
        await holder.rollback()
        await holder.close()
    async with pgdb.session() as s:
        assert await AlertRepository(s).lock(aid) is not None


# ------------------------------------------------------------------ retention (ctid batches)
async def _deletes(db) -> list[str]:
    seen: list[str] = []

    @sa.event.listens_for(db.engine.sync_engine, "before_cursor_execute")
    def _spy(conn, cur, stmt, params, ctx, many):
        if stmt.lstrip().upper().startswith("DELETE"):
            seen.append(stmt)

    return seen


async def _add_readings(db, fid, start, n, step=timedelta(minutes=1), prefix=0):
    async with db.session() as s:
        s.add_all(
            FlowReading(
                flat_id=fid,
                timestamp=start + step * i,
                flow_rate=1.0,
                volume=0.1,
                message_id=f"{prefix + i:08x}",
            )
            for i in range(n)
        )


@pytest.mark.parametrize(
    ("n_old", "batch", "deletes"), [(23, 5, 5), (20, 5, 5), (3, 5, 1), (0, 5, 1)]
)
async def test_ctid_batches_delete_exactly_the_old_rows(pgdb, society, n_old, batch, deletes):
    async with pgdb.session() as s:
        fid = (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    await _add_readings(pgdb, fid, cutoff - timedelta(minutes=n_old), n_old)  # all < cutoff
    await _add_readings(pgdb, fid, cutoff, 4, prefix=1000)  # == cutoff and newer: must stay
    seen = await _deletes(pgdb)
    assert await purge_older_than(pgdb, FlowReading, FlowReading.timestamp, cutoff, batch) == n_old
    assert len(seen) == deletes and all("ctid" in d.lower() for d in seen)
    async with pgdb.session() as s:
        left = (await s.execute(sa.select(FlowReading.timestamp))).scalars().all()
    assert len(left) == 4 and min(left) == cutoff  # boundary row (ts == cutoff) is kept


async def test_run_retention_batches_audit_and_dead_credentials(pgdb, society, tmp_path):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    async with pgdb.session() as s:
        s.add_all(
            AuditLog(entity_type="x", action="a", actor="t", timestamp=now - timedelta(days=d))
            for d in (1, 5, 40, 50, 60)
        )
    st = make_settings(
        tmp_path, database_url=DB_URL, audit_retention_days=30, readings_retention_days=0
    )
    assert (await run_retention(pgdb, st, now, dry_run=True))["audit_log"] == 3
    out = await run_retention(pgdb, st, now)
    assert out["audit_log"] == 3 and "flow_readings" not in out
    async with pgdb.session() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(AuditLog))).scalar_one() == 2


# ------------------------------------------------------------------ sensor health (EXTRACT epoch)
async def test_sensor_health_epoch_sql_matches_python_thresholds(pgdb, society):
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    ages = {"101": 30, "102": 150, "201": 4 * 60, "202": 7 * 60 + 30, "301": 3600 * 3}
    async with pgdb.session() as s:
        flats = {u: await FlatRepository(s).get_by_slug_unit("green-valley", u) for u in ages}
        for u, age in ages.items():
            await SensorHealthRepository(s).upsert(
                flats[u].id, last_seen=now - timedelta(seconds=age), status="online"
            )
    from app.services.ingest import sensor_status

    async with pgdb.session() as s:
        changed = await SensorHealthRepository(s).refresh_statuses(
            now, late_after=timedelta(minutes=2), offline_after=timedelta(minutes=5)
        )
    assert len(changed) == 4  # 102 + 201 -> late, 202 + 301 -> offline
    async with pgdb.session() as s:
        rows = {u: await SensorHealthRepository(s).get_for_flat(flats[u].id) for u in ages}
    for u, age in ages.items():
        expect = sensor_status(now - timedelta(seconds=age), now)
        assert rows[u].status == expect, u
    assert [rows[u].missed_readings for u in ("101", "102", "201", "202", "301")] == [
        0,
        2,
        4,
        7,
        180,
    ]
    async with pgdb.session() as s:  # idempotent: nothing moves, nothing written
        again = await SensorHealthRepository(s).refresh_statuses(
            now, late_after=timedelta(minutes=2), offline_after=timedelta(minutes=5)
        )
    assert again == []


async def test_maintenance_cycle_runs_on_postgres(rt, pgdb, society):
    now = datetime.now(UTC)
    async with pgdb.session() as s:
        f = await FlatRepository(s).get_by_slug_unit("green-valley", "101")
        await SensorHealthRepository(s).upsert(
            f.id, last_seen=now - timedelta(minutes=10), status="online"
        )
        fid = f.id
    summary = await maintenance.run_cycle(rt, now)
    assert None not in summary.values() and summary["sensor_health"] == 1, summary
    async with pgdb.session() as s:
        assert (await s.get(SensorHealth, fid)).status == "offline"


# ------------------------------------------------------------------ auth tokens under concurrency
async def test_ws_ticket_is_single_use_under_concurrent_redemption(pgdb, society, tmp_path):
    st = make_settings(tmp_path, database_url=DB_URL)
    user = await add_user(pgdb, "ticketeer", "Tick3tPassw0rd!", "admin")
    async with pgdb.session() as s:
        raw, _ = await auth_svc.issue_ws_ticket(s, st, user, Scope("all"))

    async def redeem():
        async with pgdb.session() as s:
            return await auth_svc.redeem_ws_ticket(s, raw)

    got = await asyncio.gather(*[redeem() for _ in range(12)])
    assert sum(t is not None for t in got) == 1
    assert await redeem() is None
    async with pgdb.session() as s:  # an expired ticket is refused too
        raw2, _ = await auth_svc.issue_ws_ticket(s, st, user, Scope("all"), now=T0)
    async with pgdb.session() as s:
        assert await auth_svc.redeem_ws_ticket(s, raw2, now=T0 + timedelta(hours=1)) is None
        assert await WsTicketRepository(s).purge(T0 + timedelta(hours=2)) >= 1


async def test_refresh_rotation_has_one_winner_and_reuse_revokes_the_family(
    pgdb, society, tmp_path
):
    st = make_settings(tmp_path, database_url=DB_URL)
    user = await add_user(pgdb, "rotator", "R0tat0rPassw0rd!", "admin")
    async with pgdb.session() as s:
        pair, row = await auth_svc.issue_tokens(s, st, user)
        family = row.family_id

    async def rotate():
        async with pgdb.session() as s:
            out = await auth_svc.rotate_refresh_token(s, st, pair.refresh_token)
            return out

    outs = await asyncio.gather(*[rotate() for _ in range(8)])
    kinds = sorted(o.kind for o in outs)
    assert kinds.count("ok") <= 1, kinds  # never two live successors
    assert set(kinds) <= {"ok", "reuse"}
    # a concurrent double-use is treated as theft: the whole family ends up revoked
    async with pgdb.session() as s:
        live = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.family_id == family, RefreshToken.revoked_at.is_(None))
            )
        ).scalar_one()
    assert live == (1 if kinds.count("ok") == 1 and "reuse" not in kinds else 0)
    async with pgdb.session() as s:
        again = await auth_svc.rotate_refresh_token(s, st, pair.refresh_token)
        assert again.kind == "reuse"


async def test_sequential_rotation_then_reuse_of_old_token(pgdb, society, tmp_path):
    st = make_settings(tmp_path, database_url=DB_URL)
    user = await add_user(pgdb, "seqrot", "S3qRotPassw0rd!", "admin")
    async with pgdb.session() as s:
        pair, _ = await auth_svc.issue_tokens(s, st, user)
    async with pgdb.session() as s:
        first = await auth_svc.rotate_refresh_token(s, st, pair.refresh_token)
    assert first.kind == "ok"
    async with pgdb.session() as s:
        assert (await auth_svc.rotate_refresh_token(s, st, pair.refresh_token)).kind == "reuse"
    async with pgdb.session() as s:  # the successor died with the family
        assert (
            await auth_svc.rotate_refresh_token(s, st, first.pair.refresh_token)
        ).kind == "reuse"
        assert await RefreshTokenRepository(s).purge(datetime.now(UTC) + timedelta(days=30)) >= 2


async def test_concurrent_logins_do_not_lose_failed_login_counts(pgdb, society, tmp_path):
    """authenticate() locks the user row: N concurrent bad passwords count exactly N."""
    st = make_settings(tmp_path, database_url=DB_URL, login_max_failures=100)
    await add_user(pgdb, "counted", "C0untedPassw0rd!", "admin")

    async def bad():
        async with pgdb.session() as s:
            return await auth_svc.authenticate(UserRepository(s), st, "counted", "wrong-password-1")

    await asyncio.gather(*[bad() for _ in range(10)])
    async with pgdb.session() as s:
        u = (await s.execute(sa.select(User).where(User.username == "counted"))).scalar_one()
    assert u.failed_logins == 10


# ------------------------------------------------------------------ migration data fix-ups
def _sql(sql, **p):
    async def go():
        from sqlalchemy.ext.asyncio import create_async_engine

        e = create_async_engine(DB_URL)
        try:
            async with e.begin() as c:
                r = await c.execute(sa.text(sql), p)
                return r.all() if r.returns_rows else []
        finally:
            await e.dispose()

    return run(go())


def test_migration_lowercases_existing_mixed_case_usernames_and_keeps_data():
    _sql("DELETE FROM users")
    migrate("0001", down=True)
    try:
        for name in ("Alice", "BOB", "carol"):
            _sql(
                "INSERT INTO users (id, username, hashed_password, name, role) "
                "VALUES (gen_random_uuid(), :n, 'x', :n, 'admin')",
                n=name,
            )
        migrate("0002")
        names = sorted(r[0] for r in _sql("SELECT username FROM users"))
        assert names == ["alice", "bob", "carol"]
        with pytest.raises(sa.exc.IntegrityError, match="ck_users_username_lower"):
            _sql(
                "INSERT INTO users (id, username, hashed_password, name) "
                "VALUES (gen_random_uuid(), 'MiXed', 'x', 'x')"
            )
    finally:
        _sql("DELETE FROM users")
        migrate("head")


def test_migration_fails_loudly_and_atomically_on_a_case_collision():
    _sql("DELETE FROM users")
    migrate("0001", down=True)
    try:
        for name in ("Dave", "dave"):
            _sql(
                "INSERT INTO users (id, username, hashed_password, name) "
                "VALUES (gen_random_uuid(), :n, 'x', 'x')",
                n=name,
            )
        with pytest.raises(Exception, match=r"(?i)unique|duplicate"):
            migrate("0002")
        # transactional DDL: nothing half-applied, still at 0001, data untouched
        assert _sql("SELECT version_num FROM alembic_version") == [("0001",)]
        assert sorted(r[0] for r in _sql("SELECT username FROM users")) == ["Dave", "dave"]
        assert not _sql("SELECT 1 FROM pg_tables WHERE tablename='refresh_tokens'")
    finally:
        _sql("DELETE FROM users")
        migrate("head")


def test_downgrade_upgrade_preserves_0004_columns_and_defaults():
    migrate("0003", down=True)
    assert not _sql("SELECT 1 FROM pg_tables WHERE tablename='device_status'")
    sid, fid = uuid.uuid4(), uuid.uuid4()
    _sql("INSERT INTO societies (id, name, slug) VALUES (:s,'S','s-m4')", s=sid)
    _sql(
        "INSERT INTO flats (id, society_id, unit_number, sensor_id) VALUES (:f,:s,'1','m4')",
        f=fid,
        s=sid,
    )
    migrate("head")
    row = _sql("SELECT auto_shutoff_validated FROM flats WHERE id=:f", f=fid)
    assert row == [(False,)]  # existing flats are NOT commissioned by an upgrade
    _sql("DELETE FROM societies WHERE id=:s", s=sid)


# ------------------------------------------------------------------ readiness on the real head
async def test_health_ready_passes_on_a_really_migrated_database(make_app):
    app = make_app()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/health/ready")).status_code == 200
        head = _head()
        await asyncio.to_thread(_sql, "UPDATE alembic_version SET version_num='0001'")
        try:
            assert (await c.get("/health/ready")).status_code == 503
        finally:
            await asyncio.to_thread(_sql, "UPDATE alembic_version SET version_num=:h", h=head)


def _head() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()  # type: ignore[return-value]


def test_settings_type():
    assert Settings.model_fields["detection_policy"].default == "hybrid"

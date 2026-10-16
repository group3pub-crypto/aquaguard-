"""Alembic against real PostgreSQL: up/down/up, no model drift, constraints, triggers, cascades."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.models import Base
from tests.integration.conftest import DB_URL, alembic_config, migrate, run

pytestmark = pytest.mark.integration

APP_TABLES = {t for t in Base.metadata.tables}


async def _scalar(sql: str, **params):
    engine = create_async_engine(DB_URL)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(sa.text(sql), params)
            return res.all() if res.returns_rows else []
    finally:
        await engine.dispose()


def _rows(sql: str, **params):
    return run(_scalar(sql, **params))


@pytest.fixture
def clean_head():
    migrate("head")
    yield
    migrate("head")  # whatever the test did, leave the DB at head


def test_upgrade_downgrade_upgrade_roundtrip(clean_head):
    migrate("base", down=True)
    left = {r[0] for r in _rows("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert left <= {"alembic_version"}, f"downgrade left tables behind: {left}"
    assert not _rows("SELECT 1 FROM pg_proc WHERE proname='set_updated_at'")
    migrate("head")
    tables = {r[0] for r in _rows("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert APP_TABLES <= tables
    assert _rows("SELECT version_num FROM alembic_version")


def test_no_model_drift_alembic_check(clean_head):
    # raises CommandError listing the operations if models and migrations disagree
    command.check(alembic_config())


def test_constraints_indexes_and_fks_match_models(clean_head):
    """`alembic check` ignores CHECK constraints, partial-index predicates and ON DELETE;
    compare those explicitly."""
    db_checks = {
        r[0]: r[1]
        for r in _rows(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype='c' AND connamespace='public'::regnamespace"
        )
    }
    model_checks = {
        str(c.name)
        for t in Base.metadata.tables.values()
        for c in t.constraints
        if isinstance(c, sa.CheckConstraint)
    }
    assert set(db_checks) == model_checks

    db_idx = {r[0]: r[1] for r in _rows("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'")}
    for table in Base.metadata.tables.values():
        for ix in table.indexes:
            assert ix.name in db_idx, f"missing index {ix.name}"
            where = ix.dialect_options["postgresql"]["where"]
            assert ("WHERE" in db_idx[ix.name]) == (where is not None), ix.name
            assert ("UNIQUE" in db_idx[ix.name]) == bool(ix.unique), ix.name
    assert "WHERE is_active" in db_idx["uq_baselines_one_active"]
    assert "resolved_at IS NULL" in db_idx["ix_anomalies_unresolved"]
    assert "status" in db_idx["ix_valve_commands_pending"] and "pending" in db_idx["ix_valve_commands_pending"]

    fks = _rows(
        "SELECT conname, confdeltype::text FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace"
    )
    expected = {}
    for table in Base.metadata.tables.values():
        for fk in table.foreign_key_constraints:
            expected[str(fk.name)] = {"CASCADE": "c", "SET NULL": "n"}[fk.ondelete]
    assert dict(fks) == expected


def test_updated_at_trigger_fires_on_update(clean_head):
    sid = uuid.uuid4()
    _rows("INSERT INTO societies (id, name, slug) VALUES (:i, 'S', 's-trg')", i=sid)
    _rows("UPDATE societies SET created_at = now() - interval '1 day', updated_at = now() - interval '1 day' WHERE id=:i", i=sid)
    before = _rows("SELECT updated_at FROM societies WHERE id=:i", i=sid)[0][0]
    _rows("UPDATE societies SET name='S2' WHERE id=:i", i=sid)
    after = _rows("SELECT updated_at, created_at FROM societies WHERE id=:i", i=sid)[0]
    assert after[0] > before, "BEFORE UPDATE trigger did not refresh updated_at"
    assert after[1] < after[0]
    for table in ("societies", "flats", "alerts", "sensor_health"):
        assert _rows(
            "SELECT 1 FROM pg_trigger WHERE tgname=:n AND NOT tgisinternal", n=f"trg_{table}_updated_at"
        )


def test_cascade_deletes_and_set_null(clean_head):
    sid, fid, aid, alert, uid = (uuid.uuid4() for _ in range(5))
    _rows("INSERT INTO societies (id, name, slug) VALUES (:s,'S','s-casc')", s=sid)
    _rows("INSERT INTO flats (id, society_id, unit_number, sensor_id) VALUES (:f,:s,'1','sens-casc')", f=fid, s=sid)
    _rows("INSERT INTO users (id, username, hashed_password, name, flat_id) VALUES (:u,'u-casc','x','U',:f)", u=uid, f=fid)
    _rows("INSERT INTO flow_readings (flat_id, timestamp, flow_rate, volume, message_id) VALUES (:f, now(), 1, 1, 'aaaaaaaa')", f=fid)
    _rows("INSERT INTO sensor_health (flat_id, status) VALUES (:f,'online')", f=fid)
    _rows("INSERT INTO anomalies (id, flat_id, detected_at, anomaly_type, confidence, severity) VALUES (:a,:f,now(),'leak',0.9,0.9)", a=aid, f=fid)
    _rows("INSERT INTO alerts (id, flat_id, anomaly_id, state, tier, severity_score) VALUES (:l,:f,:a,'alert',2,0.5)", l=alert, f=fid, a=aid)
    _rows("INSERT INTO valve_commands (id, flat_id, alert_id, command, source, issued_at) VALUES (gen_random_uuid(),:f,:l,'close','auto',now())", f=fid, l=alert)
    _rows("INSERT INTO audit_log (entity_type, action, actor, flat_id) VALUES ('alert','x','sys',:f)", f=fid)
    _rows("DELETE FROM societies WHERE id=:s", s=sid)
    for table in ("flats", "flow_readings", "sensor_health", "anomalies", "alerts", "valve_commands"):
        assert _rows(f"SELECT count(*) FROM {table}")[0][0] == 0, f"{table} not cascaded"
    # SET NULL: user and audit row survive without references
    assert _rows("SELECT flat_id FROM users WHERE id=:u", u=uid) == [(None,)]
    assert _rows("SELECT flat_id FROM audit_log") == [(None,)]


def test_check_constraints_reject_bad_values(clean_head):
    sid, fid = uuid.uuid4(), uuid.uuid4()
    _rows("INSERT INTO societies (id, name, slug) VALUES (:s,'S','s-chk')", s=sid)
    with pytest.raises(sa.exc.IntegrityError, match="ck_flats_valve_state"):
        _rows("INSERT INTO flats (id, society_id, unit_number, sensor_id, valve_state) VALUES (:f,:s,'1','sens-chk','ajar')", f=fid, s=sid)
    with pytest.raises(sa.exc.IntegrityError, match="ck_users_role"):
        _rows("INSERT INTO users (id, username, hashed_password, name, role) VALUES (gen_random_uuid(),'x','x','x','root')")

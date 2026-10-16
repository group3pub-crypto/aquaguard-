"""Repositories on real PostgreSQL: JSONB, unique constraints, pagination, partial indexes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from app.models import Anomaly, Baseline, Flat, FlowReading, Society
from app.repositories import (
    AnomalyRepository,
    BaselineRepository,
    FlatRepository,
    ReadingRepository,
    SocietyRepository,
)

pytestmark = pytest.mark.integration
T0 = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)


async def _flat(db, slug="rs", unit="1", sensor=None):
    async with db.session() as s:
        soc = await SocietyRepository(s).get_by_slug(slug) or await SocietyRepository(s).create(
            name=slug, slug=slug
        )
        return (
            await FlatRepository(s).create(
                society_id=soc.id, unit_number=unit, sensor_id=sensor or f"{slug}-{unit}"
            )
        ).id


def _reading(fid, i, **kw):
    return FlowReading(
        flat_id=fid,
        timestamp=T0 + timedelta(minutes=i),
        flow_rate=float(i),
        volume=1.0,
        message_id=f"{i:08x}",
        **kw,
    )


async def test_jsonb_roundtrip_and_operators(pgdb):
    fid = await _flat(pgdb)
    slots = [{"slot": i, "median": 0.5, "tags": ["a", "é"], "n": None} for i in range(168)]
    async with pgdb.session() as s:
        await BaselineRepository(s).create_active(
            flat_id=fid, version=1, slots=slots, sample_days=3
        )
        s.add(
            Anomaly(
                flat_id=fid,
                detected_at=T0,
                anomaly_type="leak",
                confidence=0.9,
                severity=0.8,
                features={"mean_flow": 1.5, "nested": {"x": [1, 2]}},
            )
        )
    async with pgdb.session() as s:
        b = await BaselineRepository(s).active(fid)
        assert b.slots == slots
        col = (
            await s.execute(sa.text("SELECT pg_typeof(slots)::text FROM baselines"))
        ).scalar_one()
        assert col == "jsonb"
        hit = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Anomaly)
                .where(sa.cast(Anomaly.features, JSONB).contains({"nested": {"x": [1]}}))
            )
        ).scalar_one()
        assert hit == 1


async def test_unique_constraints(pgdb):
    fid = await _flat(pgdb)
    async with pgdb.session() as s:
        await ReadingRepository(s).add(_reading(fid, 1))
    with pytest.raises(IntegrityError, match="uq_flow_readings_flat_message"):
        async with pgdb.session() as s:
            await ReadingRepository(s).add(_reading(fid, 1))
    with pytest.raises(IntegrityError, match="uq_societies_slug"):
        async with pgdb.session() as s:
            await SocietyRepository(s).create(name="dup", slug="rs")
    with pytest.raises(IntegrityError, match="uq_flats_sensor_id"):
        await _flat(pgdb, slug="other", unit="9", sensor="rs-1")
    with pytest.raises(IntegrityError, match="uq_flats_society_unit"):
        async with pgdb.session() as s:
            soc = await SocietyRepository(s).get_by_slug("rs")
            await FlatRepository(s).create(society_id=soc.id, unit_number="1", sensor_id="zzz")


async def test_same_message_id_allowed_for_different_flats(pgdb):
    f1, f2 = await _flat(pgdb, unit="1"), await _flat(pgdb, unit="2")
    async with pgdb.session() as s:
        repo = ReadingRepository(s)
        assert await repo.add_idempotent(_reading(f1, 1))
        assert await repo.add_idempotent(_reading(f2, 1))
        assert not await repo.add_idempotent(_reading(f1, 1))


async def test_only_one_active_baseline_per_flat(pgdb):
    fid = await _flat(pgdb)
    async with pgdb.session() as s:
        repo = BaselineRepository(s)
        for v in (1, 2, 3):
            await repo.create_active(flat_id=fid, version=v, slots=[], sample_days=v)
    async with pgdb.session() as s:
        active = await BaselineRepository(s).active(fid)
        assert active.version == 3
        assert len(await BaselineRepository(s).history(fid)) == 3
    with pytest.raises(IntegrityError, match="uq_baselines_one_active"):
        async with pgdb.session() as s:
            s.add(Baseline(flat_id=fid, version=9, slots=[], sample_days=1, is_active=True))


async def test_reading_pagination_is_stable_and_complete(pgdb):
    fid = await _flat(pgdb)
    async with pgdb.session() as s:
        s.add_all([_reading(fid, i) for i in range(95)])
    seen = []
    async with pgdb.session() as s:
        repo = ReadingRepository(s)
        for off in range(0, 100, 20):
            page = await repo.find(fid, limit=20, offset=off)
            assert len(page) == (20 if off < 80 else 15 if off == 80 else 0)
            seen += [r.message_id for r in page]
        assert len(seen) == len(set(seen)) == 95
        assert seen == sorted(seen, reverse=True)  # newest first
        ranged = await repo.find(
            fid, start=T0 + timedelta(minutes=10), end=T0 + timedelta(minutes=19)
        )
        assert len(ranged) == 10
        assert await repo.count(fid) == 95
        assert (await repo.latest(fid)).message_id == f"{94:08x}"
        series = await repo.series(fid, T0, T0 + timedelta(minutes=4))
        assert [round(f) for _, f, _ in series] == [0, 1, 2, 3, 4]


async def test_anomaly_filters_and_open_lookup(pgdb):
    fid = await _flat(pgdb)
    async with pgdb.session() as s:
        for i, (t, res) in enumerate([("leak", None), ("leak", T0), ("burst", None)]):
            s.add(
                Anomaly(
                    flat_id=fid,
                    detected_at=T0 + timedelta(minutes=i),
                    anomaly_type=t,
                    confidence=0.9,
                    severity=0.9,
                    resolved_at=res,
                )
            )
    async with pgdb.session() as s:
        repo = AnomalyRepository(s)
        assert len(await repo.find(flat_id=fid)) == 3
        assert len(await repo.find(flat_id=fid, active_only=True)) == 2
        assert len(await repo.find(flat_id=fid, anomaly_type="leak", limit=1, offset=1)) == 1
        assert (await repo.find_open(fid, "leak")).resolved_at is None


async def test_flat_slug_unit_lookup_and_society_pagination(pgdb):
    for i in range(7):
        await _flat(pgdb, slug=f"soc{i}", unit="101")
    async with pgdb.session() as s:
        assert (await FlatRepository(s).get_by_slug_unit("soc3", "101")) is not None
        assert (await FlatRepository(s).get_by_slug_unit("soc3", "nope")) is None
        names = []
        for off in (0, 3, 6):
            names += [x.name for x in await SocietyRepository(s).find(limit=3, offset=off)]
        assert names == sorted(f"soc{i}" for i in range(7))


async def test_deleting_society_cascades_through_orm_data(pgdb):
    fid = await _flat(pgdb)
    async with pgdb.session() as s:
        s.add_all([_reading(fid, i) for i in range(3)])
    async with pgdb.session() as s:
        await s.execute(sa.delete(Society))
    async with pgdb.session() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(Flat))).scalar_one() == 0
        assert (
            await s.execute(sa.select(sa.func.count()).select_from(FlowReading))
        ).scalar_one() == 0

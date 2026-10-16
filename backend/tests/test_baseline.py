import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.models import FlowReading
from app.repositories import BaselineRepository, FlatRepository
from app.services import baseline as bl

MON = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday: slot 0 == Monday 00:00-01:00


def test_empty_slots_shape():
    slots = bl.empty_slots()
    assert len(slots) == 168 and slots[167]["slot"] == 167 and slots[5]["sample_count"] == 0


def test_hour_of_week_mapping():
    assert bl.hour_of_week(MON) == 0
    assert bl.hour_of_week(MON + timedelta(days=2, hours=5)) == 2 * 24 + 5
    assert bl.hour_of_week(MON + timedelta(days=6, hours=23)) == 167


def test_slot_statistics_are_raw_per_minute_per_hour_of_week():
    samples = [
        (MON + timedelta(weeks=w, minutes=5), v) for w, v in enumerate([1.0, 2.0, 3.0, 10.0])
    ]
    slots, days = bl.compute_slots(samples)
    s = slots[0]
    assert days == 4 and s["sample_count"] == 4
    assert s["median"] == 2.5
    assert s["mad"] == 1.0  # median(|[1,2,3,10]-2.5|) = median([1.5,.5,.5,7.5])
    assert s["p25"] == pytest.approx(float(np.percentile([1, 2, 3, 10], 25)), abs=1e-12)
    assert s["p95"] == pytest.approx(float(np.percentile([1, 2, 3, 10], 95)), abs=1e-12)
    assert slots[1]["sample_count"] == 0  # other slots untouched


def test_baseline_uses_every_minute_not_hourly_means():
    # one hour of per-minute flow: 50 quiet minutes and 10 minutes at 6 L/min
    samples = [(MON + timedelta(minutes=m), 6.0 if m >= 50 else 0.0) for m in range(60)]
    slots, _ = bl.compute_slots(samples)
    s = slots[0]
    assert s["sample_count"] == 60 and s["median"] == 0.0 and s["p95"] == 6.0
    assert s["mean"] == pytest.approx(1.0)  # hourly-mean level for the forecaster fallback


def test_readings_within_one_minute_are_averaged_first():
    samples = [(MON + timedelta(seconds=s_), v) for s_, v in [(10, 1.0), (50, 3.0), (70, 5.0)]]
    slots, _ = bl.compute_slots(samples)
    assert (
        slots[0]["sample_count"] == 2 and slots[0]["median"] == 3.5
    )  # minute bins: mean(1,3)=2.0 and 5.0


def test_slots_roundtrip_through_shared_baseline_exactly():
    from aquaguard_features import SlotBaseline

    slots, _ = bl.compute_slots([(MON + timedelta(minutes=m), (m * 0.37) % 4) for m in range(600)])
    b = SlotBaseline.from_slots(json.loads(json.dumps(slots)))
    assert b is not None and b.count[0] == 60 and b.median[0] == slots[0]["median"]


def test_hour_of_week_follows_configured_timezone():
    bl.configure_timezone("Asia/Kolkata")  # UTC+5:30
    try:
        assert bl.hour_of_week(MON) == 5  # Monday 00:00 UTC = 05:30 local
        assert bl.hour_of_week(MON + timedelta(hours=19)) == 24  # Tuesday 00:30 local
    finally:
        bl.configure_timezone("UTC")


def test_learning_to_ready_at_14_days():
    def days(n):
        return bl.compute_slots([(MON + timedelta(days=d), 1.0) for d in range(n)])[1]

    assert bl.status_for(days(13)) == "learning"
    assert bl.status_for(days(14)) == "ready"


def test_slot_stats_ignores_unobserved_or_malformed_slots():
    slots, _ = bl.compute_slots([(MON, 2.0)])
    assert bl.slot_stats(slots, MON)["median"] == 2.0
    assert bl.slot_stats(slots, MON + timedelta(hours=3)) is None
    assert bl.slot_stats(None, MON) is None and bl.slot_stats([], MON) is None


async def _flat_with_history(db, days: int):
    async with db.session() as s:
        from app.models import Society

        soc = Society(name="S", slug="s", total_flats=1)
        s.add(soc)
        await s.flush()
        flat = await FlatRepository(s).create(society_id=soc.id, unit_number="1", sensor_id="x")
        end = datetime(2026, 3, 20, tzinfo=UTC)
        for h in range(days * 24):
            s.add(
                FlowReading(
                    flat_id=flat.id,
                    timestamp=end - timedelta(hours=h),
                    flow_rate=1.0 + (h % 24) / 10,
                    volume=0.1,
                    message_id=f"{h:08x}",
                )
            )
        await s.flush()
        return flat.id, end


async def test_train_baseline_versions_and_status(db):
    fid, end = await _flat_with_history(db, 15)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        b1 = await bl.train_baseline(s, flat, now=end, actor="tester")
        assert b1.version == 1 and b1.sample_days >= 14 and flat.baseline_status == "ready"
        assert len(b1.slots) == 168
        b2 = await bl.train_baseline(s, flat, now=end)
        assert b2.version == 2
        hist = await BaselineRepository(s).history(fid)
        assert [b.is_active for b in hist] == [True, False]
        assert (await BaselineRepository(s).active(fid)).version == 2


async def test_short_history_stays_learning_and_goes_stale(db):
    fid, end = await _flat_with_history(db, 3)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        b = await bl.train_baseline(s, flat, now=end)
        assert flat.baseline_status == "learning" and b.sample_days < 14
        flat.baseline_status = "ready"
        assert await bl.refresh_status(s, flat, end + timedelta(days=8)) == "stale"
        assert (
            await bl.refresh_status(s, flat, end + timedelta(days=1)) == "stale"
        )  # sticky until retrain

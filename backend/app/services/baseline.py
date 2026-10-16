"""Hour-of-week baselines: 168 slots of robust statistics, versioned per flat."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Baseline, Flat
from app.repositories import AuditRepository, BaselineRepository, ReadingRepository

SLOTS = 168
READY_DAYS = 14
STALE_AFTER = timedelta(days=7)
TRAINING_WINDOW_DAYS = 28
MAD_SCALE = 1.4826

_tz: tzinfo = UTC


def configure_timezone(name: str) -> None:
    global _tz
    _tz = ZoneInfo(name) if name.upper() != "UTC" else UTC


def local(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(_tz)


def hour_of_week(dt: datetime) -> int:
    d = local(dt)
    return d.weekday() * 24 + d.hour


def empty_slots() -> list[dict]:
    return [
        {"slot": i, "median": 0.0, "mad": 0.0, "p25": 0.0, "p75": 0.0, "p95": 0.0, "sample_count": 0}
        for i in range(SLOTS)
    ]


def compute_slots(samples: list[tuple[datetime, float]]) -> tuple[list[dict], int]:
    """Per-slot stats over hourly mean flows (one sample per calendar hour).

    Returns (slots, distinct_days_observed).
    """
    hourly: dict[tuple, list[float]] = defaultdict(list)
    for ts, flow in samples:
        d = local(ts)
        hourly[(d.date(), d.hour)].append(flow)
    per_slot: dict[int, list[float]] = defaultdict(list)
    days = set()
    for (date, hour), vals in hourly.items():
        days.add(date)
        per_slot[datetime(date.year, date.month, date.day).weekday() * 24 + hour].append(
            float(np.mean(vals))
        )
    slots = empty_slots()
    for slot, vals in per_slot.items():
        arr = np.asarray(vals)
        med = float(np.median(arr))
        slots[slot] = {
            "slot": slot,
            "median": round(med, 4),
            "mad": round(float(np.median(np.abs(arr - med))), 4),
            "p25": round(float(np.percentile(arr, 25)), 4),
            "p75": round(float(np.percentile(arr, 75)), 4),
            "p95": round(float(np.percentile(arr, 95)), 4),
            "sample_count": len(vals),
        }
    return slots, len(days)


def status_for(sample_days: int) -> str:
    return "ready" if sample_days >= READY_DAYS else "learning"


async def train_baseline(
    session: AsyncSession, flat: Flat, *, now: datetime | None = None, actor: str = "system"
) -> Baseline:
    now = now or datetime.now(UTC)
    start = now - timedelta(days=TRAINING_WINDOW_DAYS)
    rows = await ReadingRepository(session).series(flat.id, start, now)
    slots, days = compute_slots([(t, f) for t, f, _ in rows])
    repo = BaselineRepository(session)
    baseline = await repo.create_active(
        flat_id=flat.id,
        version=await repo.next_version(flat.id),
        slots=slots,
        trained_from=rows[0][0] if rows else start,
        trained_to=rows[-1][0] if rows else now,
        sample_days=days,
    )
    flat.baseline_status = status_for(days)
    await AuditRepository(session).record(
        entity_type="baseline", entity_id=baseline.id, action="baseline_trained", actor=actor,
        details={"version": baseline.version, "sample_days": days, "readings": len(rows)},
        flat_id=flat.id, society_id=flat.society_id,
    )
    return baseline


def is_stale(baseline: Baseline | None, now: datetime) -> bool:
    return baseline is not None and baseline.trained_to is not None and now - baseline.trained_to > STALE_AFTER


async def refresh_status(session: AsyncSession, flat: Flat, now: datetime) -> str:
    """Flip a ready baseline to 'stale' when it has not been retrained for a week."""
    baseline = await BaselineRepository(session).active(flat.id)
    if flat.baseline_status == "ready" and is_stale(baseline, now):
        flat.baseline_status = "stale"
    return flat.baseline_status


def slot_stats(slots: list[dict] | None, when: datetime) -> dict | None:
    if not slots or len(slots) != SLOTS:
        return None
    s = slots[hour_of_week(when)]
    return s if s.get("sample_count", 0) > 0 else None

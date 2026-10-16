"""Hour-of-week baselines: 168 slots of robust statistics, versioned per flat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import numpy as np
from aquaguard_features import MAD_SCALE as MAD_SCALE
from aquaguard_features import SLOTS as SLOTS
from aquaguard_features import fit_slot_baseline
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Baseline, Flat
from app.repositories import AuditRepository, BaselineRepository, ReadingRepository

READY_DAYS = 14
STALE_AFTER = timedelta(days=7)
TRAINING_WINDOW_DAYS = 28

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
        {
            "slot": i,
            "median": 0.0,
            "mad": 0.0,
            "p25": 0.0,
            "p75": 0.0,
            "p95": 0.0,
            "mean": 0.0,
            "hourly_std": 0.0,
            "sample_count": 0,
        }
        for i in range(SLOTS)
    ]


def minute_bins(samples: list[tuple[datetime, float]]) -> tuple[np.ndarray, np.ndarray]:
    """Collapse readings to per-minute values: bin ``(t-1min, t]`` -> mean flow, keyed by the bin
    END epoch seconds (same binning as serving's ``bin_to_minutes``)."""
    if not samples:
        return np.empty(0), np.empty(0)
    ts = np.asarray(
        [(t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp() for t, _ in samples], dtype=float
    )
    fl = np.asarray([f for _, f in samples], dtype=float)
    ok = np.isfinite(fl)
    end = np.ceil(ts[ok] / 60.0 - 1e-9) * 60.0
    uniq, inv = np.unique(end, return_inverse=True)
    sums = np.bincount(inv, weights=fl[ok])
    return uniq, sums / np.bincount(inv)


def compute_slots(samples: list[tuple[datetime, float]]) -> tuple[list[dict], int]:
    """Per-slot stats over RAW per-minute flows (same semantics as ml training).

    Returns (slots, distinct_local_days_observed). Slots keep the 168-entry JSON schema; extra
    keys ``mean``/``hourly_std`` (hourly-mean level, used by the forecaster fallback) are additive.
    """
    ends, flows = minute_bins(samples)
    if ends.size == 0:
        return empty_slots(), 0
    locs = [local(datetime.fromtimestamp(float(e), UTC)) for e in ends]
    slot = np.asarray([d.weekday() * 24 + d.hour for d in locs])
    hour_id = np.asarray([d.toordinal() * 24 + d.hour for d in locs])
    days = {d.date() for d in locs}
    return fit_slot_baseline(slot, flows, hour_id).to_slots(), len(days)


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
        entity_type="baseline",
        entity_id=baseline.id,
        action="baseline_trained",
        actor=actor,
        details={"version": baseline.version, "sample_days": days, "readings": len(rows)},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    return baseline


def is_stale(baseline: Baseline | None, now: datetime) -> bool:
    return (
        baseline is not None
        and baseline.trained_to is not None
        and now - baseline.trained_to > STALE_AFTER
    )


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

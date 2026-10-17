"""Usage / loss summaries and the weekly markdown report."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Flat, Society
from app.repositories import (
    AlertRepository,
    AnomalyRepository,
    BaselineRepository,
    FlatRepository,
    ReadingRepository,
    ValveRepository,
)
from app.services.baseline import slot_stats


async def flat_usage(session: AsyncSession, flat_id: uuid.UUID, hours: int, now: datetime) -> dict:
    start = now - timedelta(hours=hours)
    vol = (await ReadingRepository(session).volume_sum([flat_id], start, now)).get(flat_id, 0.0)
    baseline = await BaselineRepository(session).active(flat_id)
    expected = None
    if baseline:
        expected = sum(
            (s["median"] * 60)
            if (s := slot_stats(baseline.slots, start + timedelta(hours=h)))
            else 0.0
            for h in range(hours)
        )
    return {
        "flat_id": str(flat_id),
        "hours": hours,
        "volume_l": round(vol, 1),
        "avg_lpm": round(vol / (hours * 60), 3),
        "expected_l": None if expected is None else round(expected, 1),
    }


async def top_losses(
    session: AsyncSession, society_id: uuid.UUID, days: int, limit: int, now: datetime
) -> list[dict]:
    """Estimated litres lost per flat; anomalies are read by society (one query), only the
    top `limit` flats are then looked up for their unit number."""
    anomalies = await AnomalyRepository(session).between_society(
        society_id, now - timedelta(days=days), now
    )
    totals: dict[uuid.UUID, float] = {}
    for a in anomalies:
        end = a.resolved_at or a.window_end or now
        minutes = max((end - a.detected_at).total_seconds() / 60, 15.0)
        totals[a.flat_id] = totals.get(a.flat_id, 0.0) + (a.est_loss_lpm or 0.0) * minutes
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    units = dict(
        (
            await session.execute(
                select(Flat.id, Flat.unit_number).where(Flat.id.in_([f for f, _ in ranked]))
            )
        )
        .tuples()
        .all()
    )
    return [
        {"flat_id": str(fid), "unit": units.get(fid, "?"), "estimated_loss_l": round(v, 1)}
        for fid, v in ranked
    ]


async def weekly_report(
    session: AsyncSession, society: Society, now: datetime | None = None
) -> str:
    now = now or datetime.now(UTC)
    week_start, prev_start = now - timedelta(days=7), now - timedelta(days=14)
    flats = await FlatRepository(session).count_for_society(society.id)
    readings = ReadingRepository(session)
    this_week = await readings.volume_sum_society(society.id, week_start, now)
    last_week = await readings.volume_sum_society(society.id, prev_start, week_start)
    anomalies = await AnomalyRepository(session).between_society(society.id, week_start, now)
    by_type = Counter(a.anomaly_type for a in anomalies)
    alerts = await AlertRepository(session).count_between_society(society.id, week_start, now)
    valves = await ValveRepository(session).count_between_society(society.id, week_start, now)
    losses = await top_losses(session, society.id, 7, 5, now)
    change = "n/a" if last_week <= 0 else f"{(this_week - last_week) / last_week * 100:+.1f}%"
    lines = [
        f"# Weekly water report: {society.name}",
        f"_{week_start:%Y-%m-%d} to {now:%Y-%m-%d}_",
        "",
        "## Summary",
        f"- Flats monitored: {flats}",
        f"- Total consumption: {this_week:,.0f} L ({change} vs previous week)",
        f"- Anomalies detected: {len(anomalies)}",
        f"- Alerts raised: {alerts}",
        f"- Valve commands issued: {valves}",
        "",
        "## Anomalies by type",
        *([f"- {t}: {n}" for t, n in by_type.most_common()] or ["- none"]),
        "",
        "## Top estimated losses",
        *(
            [
                f"{i}. Flat {r['unit']}: about {r['estimated_loss_l']:,.0f} L"
                for i, r in enumerate(losses, 1)
            ]
            or ["- none"]
        ),
    ]
    return "\n".join(lines) + "\n"

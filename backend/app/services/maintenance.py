"""Periodic housekeeping, run only on the elected leader (see app.leader).

Every step is set-based (aggregate UPDATE/SELECT or keyset-paginated batches, no silent caps,
no per-flat queries) and isolated: one failing step never stops the others, and escalations
use one transaction per alert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select

from app.models import Baseline, Flat, FlowReading
from app.repositories import FlatRepository, SensorHealthRepository
from app.runtime import Runtime
from app.services import alert_engine, baseline, valve_control

log = structlog.get_logger(__name__)
LATE_AFTER = timedelta(minutes=2)
OFFLINE_AFTER = timedelta(minutes=5)
MIN_READINGS_FOR_BASELINE = 60
RETRAIN_MAX_AGE = timedelta(hours=24)


async def expire_valve_commands(rt: Runtime, now: datetime) -> int:
    async with rt.db.session() as session:
        return await valve_control.expire_pending(
            session, now, timeout_s=rt.settings.valve_ack_timeout_s
        )


async def run_escalations(rt: Runtime, now: datetime) -> int:
    async with rt.db.session() as session:
        return await alert_engine.run_escalations(session, rt, now)


async def refresh_baseline_status(rt: Runtime, now: datetime) -> int:
    async with rt.db.session() as session:
        return await FlatRepository(session).mark_stale_baselines(now - baseline.STALE_AFTER)


async def refresh_sensor_health(rt: Runtime, now: datetime) -> int:
    """online/late/offline for every sensor in two statements; broadcasts only the changes."""
    async with rt.db.session() as session:
        changed = await SensorHealthRepository(session).refresh_statuses(
            now, late_after=LATE_AFTER, offline_after=OFFLINE_AFTER
        )
        society_of: dict[Any, Any] = {}
        ids = [fid for fid, _ in changed]
        for i in range(0, len(ids), 500):
            rows = await session.execute(
                select(Flat.id, Flat.society_id).where(Flat.id.in_(ids[i : i + 500]))
            )
            society_of.update(dict(rows.tuples().all()))
    for fid, status in changed:
        sid = society_of.get(fid)
        if sid is not None:
            await rt.ws.broadcast(
                {"type": "sensor", "flat_id": str(fid), "status": status}, [str(fid), str(sid)]
            )
    return len(changed)


async def retrain_baselines(
    rt: Runtime, now: datetime, max_age: timedelta = RETRAIN_MAX_AGE, page: int = 100
) -> int:
    """Retrain baselines that are missing / older than `max_age` for flats with enough data.

    Candidate flats come from ONE query per page (anti-join on the active baseline + a bounded
    existence probe instead of COUNT(*)); each flat trains in its own transaction.
    """
    active = (
        select(Baseline.flat_id.label("flat_id"), Baseline.trained_to.label("trained_to"))
        .where(Baseline.is_active.is_(True))
        .subquery()
    )
    enough = (
        select(FlowReading.id)
        .where(FlowReading.flat_id == Flat.id)
        .offset(MIN_READINGS_FOR_BASELINE - 1)
        .limit(1)
        .exists()
    )
    trained = 0
    after = None
    while True:
        stmt = (
            select(Flat.id)
            .outerjoin(active, active.c.flat_id == Flat.id)
            .where(
                or_(
                    active.c.flat_id.is_(None),
                    active.c.trained_to.is_(None),
                    active.c.trained_to < now - max_age,
                ),
                enough,
            )
            .order_by(Flat.id)
            .limit(page)
        )
        if after is not None:
            stmt = stmt.where(Flat.id > after)
        async with rt.db.session() as session:
            ids = list((await session.execute(stmt)).scalars().all())
        if not ids:
            return trained
        after = ids[-1]
        for flat_id in ids:
            try:
                async with rt.db.session() as session:
                    flat = await FlatRepository(session).get(flat_id)
                    if flat is not None:
                        await baseline.train_baseline(session, flat, now=now)
                        trained += 1
            except Exception:
                log.exception("baseline_retrain_failed", flat=str(flat_id))


async def run_cycle(rt: Runtime, now: datetime | None = None) -> dict[str, int | None]:
    """One maintenance pass. Never raises; returns per-step results (None = step failed)."""
    now = now or datetime.now(UTC)
    summary: dict[str, int | None] = {}
    steps = (
        ("valve_timeouts", expire_valve_commands),
        ("escalations", run_escalations),
        ("baseline_stale", refresh_baseline_status),
        ("sensor_health", refresh_sensor_health),
    )
    for name, step in steps:
        try:
            summary[name] = await step(rt, now)
        except Exception:
            log.exception("maintenance_step_failed", step=name)
            summary[name] = None
    return summary




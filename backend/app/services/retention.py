"""Batched data retention (READINGS_RETENTION_DAYS, AUDIT_RETENTION_DAYS; 0 = keep forever).

Deletes in small committed batches so a big backlog never holds a long lock or bloats a single
transaction. PostgreSQL uses the physical row id (``ctid``) of a LIMITed subselect; other
dialects use the primary key.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, literal_column, select

from app.config import Settings
from app.database import Database
from app.models import AuditLog, FlowReading
from app.repositories import RefreshTokenRepository, WsTicketRepository

log = structlog.get_logger(__name__)


async def purge_older_than(
    db: Database, model: Any, ts_col: Any, cutoff: datetime, batch_size: int
) -> int:
    """Delete rows with ts < cutoff in batches of `batch_size`. Returns rows deleted."""
    total = 0
    pg = db.engine.dialect.name == "postgresql"
    table = model.__table__
    while True:
        if pg:
            ctid = literal_column(f"{table.name}.ctid")
            inner = select(ctid).select_from(table).where(ts_col < cutoff).limit(batch_size)
            stmt = delete(model).where(ctid.in_(inner))
        else:
            pk = list(table.primary_key.columns)[0]
            inner = select(pk).where(ts_col < cutoff).limit(batch_size)
            stmt = delete(model).where(pk.in_(inner))
        async with db.session() as session:
            res = await session.execute(stmt)
        n = int(res.rowcount or 0)  # type: ignore[attr-defined]
        total += n
        if n < batch_size:
            return total
        await asyncio.sleep(0)  # let the event loop breathe between batches


async def run_retention(
    db: Database, settings: Settings, now: datetime | None = None, *, dry_run: bool = False
) -> dict[str, int]:
    """Apply every retention rule once. Returns rows deleted per table."""
    now = now or datetime.now(UTC)
    out: dict[str, int] = {}
    if settings.readings_retention_days > 0:
        cutoff = now - timedelta(days=settings.readings_retention_days)
        out["flow_readings"] = (
            await _count(db, FlowReading, FlowReading.timestamp, cutoff)
            if dry_run
            else await purge_older_than(
                db, FlowReading, FlowReading.timestamp, cutoff, settings.retention_batch_size
            )
        )
    if settings.audit_retention_days > 0:
        cutoff = now - timedelta(days=settings.audit_retention_days)
        out["audit_log"] = (
            await _count(db, AuditLog, AuditLog.timestamp, cutoff)
            if dry_run
            else await purge_older_than(
                db, AuditLog, AuditLog.timestamp, cutoff, settings.retention_batch_size
            )
        )
    if not dry_run:  # housekeeping that is always safe: dead credentials
        async with db.session() as session:
            out["refresh_tokens"] = await RefreshTokenRepository(session).purge(
                now - timedelta(days=7)
            )
            out["ws_tickets"] = await WsTicketRepository(session).purge(now - timedelta(hours=1))
    log.info("retention_run", dry_run=dry_run, **out)
    return out


async def _count(db: Database, model: Any, ts_col: Any, cutoff: datetime) -> int:
    from sqlalchemy import func

    async with db.session() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(ts_col < cutoff)
                )
            ).scalar_one()
        )

"""Micro-batching of flow readings: one transaction and one multi-row INSERT per batch.

Per-message transactions cost ~5 database round trips and an ORM unit of work each; under load
that, not PostgreSQL, was the bottleneck. Callers `await batcher.add(values)` and get back the
reading id (new row) or None (duplicate); the future resolves only AFTER the batch committed, so
the MQTT ack that follows is still "stored" semantics (at-least-once + idempotent insert).
There is no permanent task: a drain task is started when the queue turns non-empty and exits
when it is empty again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import FlowReading, SensorHealth
from app.repositories import ReadingRepository, SensorHealthRepository
from app.runtime import Runtime

log = structlog.get_logger(__name__)
MAX_BATCH = 500
MAX_WAIT_S = 0.02

READING_COLUMNS = (
    "flat_id", "timestamp", "flow_rate", "volume", "pressure", "temperature",
    "battery_pct", "rssi", "message_id", "received_at",
)  # fmt: skip


@dataclass
class _Item:
    values: dict[str, Any]
    fut: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class ReadingBatcher:
    def __init__(self, rt: Runtime, max_batch: int = MAX_BATCH, max_wait: float = MAX_WAIT_S):
        self.rt, self.max_batch, self.max_wait = rt, max_batch, max_wait
        self._queue: list[_Item] = []
        self._drain_task: asyncio.Task | None = None
        self.batches = 0
        self.rows = 0

    async def add(self, values: dict[str, Any]) -> int | None:
        """Store one reading. Returns its new id, or None when (flat, message_id) already exists.
        Raises whatever the database raised for this specific reading."""
        item = _Item(values)
        self._queue.append(item)
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())
        return await item.fut

    async def _drain(self) -> None:
        await asyncio.sleep(self.max_wait)  # let concurrent workers join the batch
        while self._queue:
            batch, self._queue = self._queue[: self.max_batch], self._queue[self.max_batch :]
            try:
                await self._flush(batch)
            except Exception:  # never leave callers hanging
                log.exception("batch_flush_failed")
                for it in batch:
                    if not it.fut.done():
                        it.fut.set_exception(RuntimeError("batch flush failed"))
            if self._queue:
                await asyncio.sleep(0)

    async def _flush(self, batch: list[_Item]) -> None:
        self.batches += 1
        self.rows += len(batch)
        try:
            ids = await self._write(batch)
        except Exception as exc:
            if len(batch) == 1:
                batch[0].fut.set_exception(exc)
                return
            # one poisonous row (e.g. a deleted flat -> FK violation) must not sink its
            # neighbours: retry row by row so every caller gets its own outcome
            for it in batch:
                try:
                    it.fut.set_result((await self._write([it]))[0])
                except Exception as one:
                    it.fut.set_exception(one)
            return
        for it, rid in zip(batch, ids, strict=True):
            it.fut.set_result(rid)

    async def _write(self, batch: list[_Item]) -> list[int | None]:
        """Insert in one transaction; returns per item the new id or None for duplicates."""
        async with self.rt.db.session() as session:
            if session.bind.dialect.name != "postgresql":
                repo, out = ReadingRepository(session), []
                for it in batch:
                    reading = FlowReading(**it.values)
                    ok = await repo.add_idempotent(reading)
                    out.append(reading.id if ok else None)
                    if ok:
                        await SensorHealthRepository(session).upsert(
                            it.values["flat_id"],
                            last_seen=it.values["received_at"],
                            status="online",
                            missed_readings=0,
                        )
                return out
            stmt = (
                pg_insert(FlowReading)
                .values([{c: it.values.get(c) for c in READING_COLUMNS} for it in batch])
                .on_conflict_do_nothing(index_elements=["flat_id", "message_id"])
                .returning(FlowReading.id, FlowReading.flat_id, FlowReading.message_id)
            )
            new = {(r.flat_id, r.message_id): r.id for r in (await session.execute(stmt)).all()}
            out = []
            for it in batch:  # the first item of an in-batch duplicate pair owns the new row
                out.append(new.pop((it.values["flat_id"], it.values["message_id"]), None))
            seen: dict[Any, datetime] = {}
            for it, rid in zip(batch, out, strict=True):
                if rid is not None:
                    fid = it.values["flat_id"]
                    seen[fid] = max(
                        seen.get(fid, it.values["received_at"]), it.values["received_at"]
                    )
            if seen:
                hs = pg_insert(SensorHealth).values(
                    [
                        {"flat_id": f, "last_seen": t, "status": "online", "missed_readings": 0}
                        for f, t in seen.items()
                    ]
                )
                await session.execute(
                    hs.on_conflict_do_update(
                        index_elements=[SensorHealth.flat_id],
                        set_={
                            "last_seen": hs.excluded.last_seen,
                            "status": "online",
                            "missed_readings": 0,
                            "updated_at": func.now(),
                        },
                    )
                )
            return out


def now_utc() -> datetime:
    return datetime.now(UTC)

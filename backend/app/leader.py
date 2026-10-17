"""Single-leader election for the maintenance loop via a PostgreSQL session advisory lock.

The lock lives on a DEDICATED connection that is held open for as long as we lead; if that
connection dies PostgreSQL releases the lock and another instance takes over. On SQLite (tests,
single-process dev) there is nobody to compete with, so the caller always leads.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.database import Database

log = structlog.get_logger(__name__)
ADVISORY_LOCK_KEY = 0x41515541_52445001  # "AQUARD" + 1, fits bigint


class LeaderLock:
    def __init__(self, db: Database, key: int = ADVISORY_LOCK_KEY) -> None:
        self.db = db
        self.key = key
        self._conn: AsyncConnection | None = None

    @property
    def uses_lock(self) -> bool:
        return self.db.engine.dialect.name == "postgresql"

    async def acquire(self) -> bool:
        """True when this process is (still) the leader. Safe to call every cycle."""
        if not self.uses_lock:
            return True
        if self._conn is not None:
            try:  # the lock is bound to the connection: alive connection == still the leader
                await self._conn.execute(text("SELECT 1"))
                return True
            except Exception:
                log.warning("leader_connection_lost")
                await self._drop()
        conn: AsyncConnection | None = None
        try:
            conn = await self.db.engine.connect()
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})
            ).scalar()
        except Exception:
            log.exception("leader_acquire_failed")
            if conn is not None:
                self._conn = conn  # may hold the lock if the failure came after acquiring it
                await self._drop()
            return False
        if got:
            self._conn = conn
            log.info("maintenance_leader_acquired")
            return True
        await conn.close()  # lock not held on this connection: safe to pool
        return False

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
        except Exception:
            log.warning("leader_unlock_failed")
        await self._drop()

    async def _drop(self) -> None:
        """Really close the lock connection. A plain ``close()`` would hand it back to the pool
        with the session-level advisory lock STILL HELD (pool reset only rolls back), so the
        lock could outlive our leadership; ``invalidate()`` discards the DBAPI connection."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.invalidate()
            except Exception:
                log.debug("leader_connection_invalidate_failed")
            try:
                await conn.close()
            except Exception:
                log.debug("leader_connection_close_failed")

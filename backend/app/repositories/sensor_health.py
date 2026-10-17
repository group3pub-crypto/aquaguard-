from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Integer, func, literal, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import SensorHealth
from app.models.base import UTCDateTime
from app.repositories.base import BaseRepository

EXPECTED_INTERVAL_S = 60.0


class SensorHealthRepository(BaseRepository[SensorHealth]):
    model = SensorHealth

    async def get_for_flat(self, flat_id: uuid.UUID) -> SensorHealth | None:
        return await self.get(flat_id)

    async def upsert(self, flat_id: uuid.UUID, **values) -> None:
        """Insert-or-update. On PostgreSQL a single atomic ON CONFLICT statement, so concurrent
        first messages of a flat cannot fail on the primary key (and never touch the ORM
        identity map). Other dialects (SQLite tests) use get-then-write."""
        if self.dialect == "postgresql":
            stmt = pg_insert(SensorHealth).values(flat_id=flat_id, **values)
            await self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SensorHealth.flat_id],
                    set_={**values, "updated_at": func.now()},
                )
            )
            # a row cached in this session (if any) is now stale
            cached = self.session.identity_map.get(
                sa_inspect(SensorHealth).identity_key_from_primary_key((flat_id,))
            )
            if cached is not None:
                self.session.expire(cached)
            return
        row = await self.get(flat_id)
        if row is None:
            row = SensorHealth(flat_id=flat_id, **values)
            self.session.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)
        await self.flush()

    async def all(self) -> list[SensorHealth]:
        return await self._all(select(SensorHealth))

    async def refresh_statuses(
        self, now: datetime, *, late_after: timedelta, offline_after: timedelta
    ) -> list[tuple[uuid.UUID, str]]:
        """Recompute online/late/offline for every sensor with set-based UPDATEs.

        Returns (flat_id, new_status) for each row whose status changed. Also refreshes
        ``missed_readings`` for sensors that are not online (one statement, no row loading).
        """
        online_cut, late_cut = now - late_after, now - offline_after
        buckets = (
            ("online", SensorHealth.last_seen >= online_cut),
            ("late", (SensorHealth.last_seen < online_cut) & (SensorHealth.last_seen >= late_cut)),
            ("offline", SensorHealth.last_seen.is_(None) | (SensorHealth.last_seen < late_cut)),
        )
        changed: list[tuple[uuid.UUID, str]] = []
        for status, cond in buckets:
            res = await self.session.execute(
                update(SensorHealth)
                .where(cond, SensorHealth.status != status)
                .values(status=status)
                .returning(SensorHealth.flat_id)
            )
            changed.extend((fid, status) for fid in res.scalars().all())
        age_s: ColumnElement[Any]
        if self.dialect == "postgresql":
            age_s = func.extract("epoch", literal(now, UTCDateTime) - SensorHealth.last_seen)
        else:
            age_s = (
                func.julianday(literal(now, UTCDateTime)) - func.julianday(SensorHealth.last_seen)
            ) * 86400
        await self.session.execute(
            update(SensorHealth)
            .where(SensorHealth.last_seen.is_not(None), SensorHealth.status != "online")
            .values(missed_readings=func.cast(func.floor(age_s / EXPECTED_INTERVAL_S), Integer))
        )
        return changed

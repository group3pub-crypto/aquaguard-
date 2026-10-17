from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select

from app.models import Alert, Anomaly, Flat
from app.models.alerts import OPEN_STATES
from app.repositories.base import BaseRepository
from app.scope import Scope

# first moment (relative to creation) at which a tier may escalate; used as a SQL pre-filter
# (the exact base also depends on the last escalation / snooze expiry and is re-checked in code)
TIER_DELAYS = {1: timedelta(minutes=30), 2: timedelta(hours=2), 3: timedelta(hours=6)}


class AlertRepository(BaseRepository[Alert]):
    model = Alert

    async def find(
        self,
        *,
        scope: Scope | None = None,
        flat_id: uuid.UUID | None = None,
        state: str | None = None,
        active_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Alert]:
        stmt = select(Alert).order_by(Alert.created_at.desc(), Alert.id)
        if scope is not None:
            stmt = stmt.where(scope.flat_id_pred(Alert.flat_id))
        if flat_id:
            stmt = stmt.where(Alert.flat_id == flat_id)
        if state:
            stmt = stmt.where(Alert.state == state)
        if active_only:
            stmt = stmt.where(Alert.state.in_(OPEN_STATES))
        return await self._all(stmt.limit(limit).offset(offset))

    async def get_scoped(
        self, alert_id: uuid.UUID, scope: Scope, *, for_update: bool = False
    ) -> Alert | None:
        stmt = select(Alert).where(Alert.id == alert_id, scope.flat_id_pred(Alert.flat_id))
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await self._one(stmt)

    async def lock(self, alert_id: uuid.UUID, *, skip_locked: bool = True) -> Alert | None:
        """Re-read one alert under ``FOR UPDATE [SKIP LOCKED]`` (no-op clause on SQLite)."""
        stmt = (
            select(Alert)
            .where(Alert.id == alert_id)
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
        return await self._one(stmt)

    async def active(
        self,
        flat_ids: list[uuid.UUID] | None = None,
        *,
        scope: Scope | None = None,
        society_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        stmt = (
            select(Alert)
            .where(Alert.state.in_(OPEN_STATES))
            .order_by(Alert.severity_score.desc(), Alert.id)
        )
        if flat_ids is not None:
            stmt = stmt.where(Alert.flat_id.in_(flat_ids))
        if society_id is not None:
            stmt = stmt.where(
                Alert.flat_id.in_(select(Flat.id).where(Flat.society_id == society_id))
            )
        if scope is not None:
            stmt = stmt.where(scope.flat_id_pred(Alert.flat_id))
        if limit is not None:
            stmt = stmt.limit(limit)
        return await self._all(stmt)

    async def count_active(self, flat_ids: list[uuid.UUID]) -> int:
        if not flat_ids:
            return 0
        return await self._count(
            select(Alert.id).where(Alert.flat_id.in_(flat_ids), Alert.state.in_(OPEN_STATES))
        )

    async def escalation_candidate_ids(
        self, now: datetime, *, after: uuid.UUID | None = None, limit: int = 200
    ) -> list[uuid.UUID]:
        """Ids of alerts that MAY need work now, keyset-paginated by id.

        Open alerts whose tier delay (counted from creation, a lower bound of the real base)
        has elapsed, plus snoozed alerts whose snooze ended. Everything else is skipped in SQL.
        """
        due = or_(
            *(
                and_(Alert.tier == tier, Alert.created_at <= now - delay)
                for tier, delay in TIER_DELAYS.items()
            )
        )
        stmt = select(Alert.id).where(
            or_(
                and_(Alert.state.in_(("nudge", "alert", "ticket")), due),
                and_(Alert.state == "snoozed", Alert.snoozed_until <= now),
            )
        )
        if after is not None:
            stmt = stmt.where(Alert.id > after)
        res = await self.session.execute(stmt.order_by(Alert.id).limit(limit))
        return list(res.scalars().all())

    async def find_dedupe(
        self, flat_id: uuid.UUID, anomaly_type: str, anomaly_id: uuid.UUID, since: datetime
    ) -> Alert | None:
        """Open alert for the same flat + anomaly type that is the same ongoing problem.

        Same problem = linked to this very anomaly, or created / last confirmed by the detector
        (anomaly window end) within the dedupe window `since`..now.
        """
        stmt = (
            select(Alert)
            .join(Anomaly, Anomaly.id == Alert.anomaly_id)
            .where(
                Alert.flat_id == flat_id,
                Anomaly.anomaly_type == anomaly_type,
                Alert.state.in_(OPEN_STATES),
                or_(
                    Alert.anomaly_id == anomaly_id,
                    Alert.created_at >= since,
                    func.coalesce(Anomaly.window_end, Anomaly.detected_at) >= since,
                ),
            )
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        return await self._one(stmt)

    async def find_unresolved(self, flat_id: uuid.UUID, anomaly_type: str) -> Alert | None:
        """Latest still-open alert of this type for a flat (any age)."""
        stmt = (
            select(Alert)
            .join(Anomaly, Anomaly.id == Alert.anomaly_id)
            .where(
                Alert.flat_id == flat_id,
                Anomaly.anomaly_type == anomaly_type,
                Alert.state.in_(OPEN_STATES),
            )
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        return await self._one(stmt)

    async def find_overridden(
        self, flat_id: uuid.UUID, anomaly_type: str, now: datetime
    ) -> Alert | None:
        """An override whose cooldown (snoozed_until) is still running."""
        stmt = (
            select(Alert)
            .join(Anomaly, Anomaly.id == Alert.anomaly_id)
            .where(
                Alert.flat_id == flat_id,
                Anomaly.anomaly_type == anomaly_type,
                Alert.state == "overridden",
                Alert.snoozed_until > now,
            )
            .limit(1)
        )
        return await self._one(stmt)

    async def for_anomaly(self, anomaly_id: uuid.UUID) -> list[Alert]:
        return await self._all(select(Alert).where(Alert.anomaly_id == anomaly_id))

    async def society_of(self, alert: Alert) -> uuid.UUID | None:
        return (
            await self.session.execute(select(Flat.society_id).where(Flat.id == alert.flat_id))
        ).scalar_one_or_none()

    async def count_between(self, flat_ids: list[uuid.UUID], start: datetime, end: datetime) -> int:
        if not flat_ids:
            return 0
        return await self._count(
            select(Alert.id).where(
                Alert.flat_id.in_(flat_ids), Alert.created_at >= start, Alert.created_at <= end
            )
        )

    async def count_between_society(
        self, society_id: uuid.UUID, start: datetime, end: datetime
    ) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Alert)
                    .join(Flat, Flat.id == Alert.flat_id)
                    .where(
                        Flat.society_id == society_id,
                        Alert.created_at >= start,
                        Alert.created_at <= end,
                    )
                )
            ).scalar_one()
        )

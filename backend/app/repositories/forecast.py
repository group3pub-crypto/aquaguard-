from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Forecast
from app.repositories.base import BaseRepository


class ForecastRepository(BaseRepository[Forecast]):
    model = Forecast

    async def latest(self, flat_id: uuid.UUID) -> Forecast | None:
        return await self._one(
            select(Forecast)
            .where(Forecast.flat_id == flat_id)
            .order_by(Forecast.generated_at.desc())
            .limit(1)
        )

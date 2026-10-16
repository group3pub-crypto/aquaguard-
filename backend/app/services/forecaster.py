"""24h demand forecast (mean L/min per hour): ML regressor, else baseline median."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Flat, Forecast
from app.repositories import (
    AuditRepository,
    BaselineRepository,
    ForecastRepository,
    ReadingRepository,
)
from app.services.baseline import MAD_SCALE, local, slot_stats
from app.services.detector import ModelStore

HISTORY_HOURS = 24 * 8
Z95 = 1.96
FORECAST_FEATURES = [
    "lag_1",
    "lag_2",
    "lag_24",
    "lag_168",
    "rolling_24h",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend",
]


def _floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def forecast_features(series: dict[datetime, float], target: datetime, fallback) -> list[float]:
    """Feature row for hour `target`; unknown past hours use `fallback(hour)` (baseline L/min)."""

    def val(h: int) -> float:
        t = target - timedelta(hours=h)
        return series[t] if t in series else fallback(t)

    d = local(target)
    return [
        val(1),
        val(2),
        val(24),
        val(168),
        float(np.mean([val(h) for h in range(1, 25)])),
        math.sin(2 * math.pi * d.hour / 24),
        math.cos(2 * math.pi * d.hour / 24),
        math.sin(2 * math.pi * d.weekday() / 7),
        math.cos(2 * math.pi * d.weekday() / 7),
        float(d.weekday() >= 5),
    ]


async def generate_forecast(
    session: AsyncSession,
    store: ModelStore,
    flat: Flat,
    *,
    now: datetime | None = None,
    horizon_hours: int = 24,
    actor: str = "system",
) -> Forecast:
    now = now or datetime.now(UTC)
    await store.ensure_loaded()
    baseline = await BaselineRepository(session).active(flat.id)
    slots = baseline.slots if baseline else None

    def baseline_lpm(hour: datetime) -> float:
        s = slot_stats(slots, hour)
        return s["median"] if s else 0.0

    start = _floor_hour(now) - timedelta(hours=HISTORY_HOURS)
    sums: dict[datetime, tuple[float, int]] = {}
    for ts, flow, _vol in await ReadingRepository(session).series(flat.id, start, now):
        h = _floor_hour(ts)
        total, n = sums.get(h, (0.0, 0))
        sums[h] = (total + flow, n + 1)
    series: dict[datetime, float] = {h: total / n for h, (total, n) in sums.items()}
    series.pop(_floor_hour(now), None)  # current hour is incomplete

    meta = store.metadata.get("forecaster", {})
    resid = meta.get("residual_std") or meta.get("rmse")
    use_model = store.forecaster is not None
    first = _floor_hour(now) + timedelta(hours=1)
    preds = []
    for i in range(horizon_hours):
        target = first + timedelta(hours=i)
        s = slot_stats(slots, target)
        if use_model:
            try:
                x = np.asarray([forecast_features(series, target, baseline_lpm)])
                yhat = float(store.forecaster.predict(x)[0])
                if not math.isfinite(yhat):
                    raise ValueError("non-finite prediction")
            except Exception:
                yhat, use_model = baseline_lpm(target), False
        else:
            yhat = baseline_lpm(target)
        yhat = max(yhat, 0.0)
        series[target] = yhat  # feed back for multi-step lags
        if use_model and resid:
            half: float = Z95 * float(resid)
        elif s:
            half = Z95 * MAD_SCALE * s["mad"]
        else:
            half = 0.0
        preds.append(
            {
                "hour": target.isoformat(),
                "predicted_lpm": round(yhat, 3),
                "lower": round(max(yhat - half, 0.0), 3),
                "upper": round(yhat + half, 3),
            }
        )
    fc = await ForecastRepository(session).add(
        Forecast(
            flat_id=flat.id,
            generated_at=now,
            horizon_hours=horizon_hours,
            predictions=preds,
            model_version=store.forecaster_version if use_model else "baseline-median",
            mape=meta.get("mape") if use_model else None,
        )
    )
    await AuditRepository(session).record(
        entity_type="forecast",
        entity_id=fc.id,
        action="forecast_generated",
        actor=actor,
        details={"horizon_hours": horizon_hours, "model_version": fc.model_version},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    return fc

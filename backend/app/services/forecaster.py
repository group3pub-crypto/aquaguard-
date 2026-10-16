"""24h demand forecast (mean L/min per hour): recursive ML regressor, else baseline."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import structlog
from aquaguard_features import forecast_feature_matrix
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

log = structlog.get_logger(__name__)

HISTORY_HOURS = 24 * 8
Z95 = 1.96


def _floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def forecast_features(series: dict[datetime, float], target: datetime, fallback) -> list[float]:
    """Feature row for hour `target`; unknown past hours use `fallback(hour)` (baseline L/min).

    Same definition as training (shared ``forecast_feature_matrix``): lags are hourly mean L/min,
    calendar features use the TARGET hour in local time.
    """

    def val(h: int) -> float:
        t = target - timedelta(hours=h)
        return series[t] if t in series else fallback(t)

    d = local(target)
    row = forecast_feature_matrix(
        np.array([val(1)]),
        np.array([val(2)]),
        np.array([val(24)]),
        np.array([val(168)]),
        np.array([np.mean([val(h) for h in range(1, 25)])]),
        np.array([d.hour]),
        np.array([d.weekday()]),
    )
    return [float(v) for v in row[0]]


def std_for_step(meta: dict, step: int) -> float | None:
    """Residual std (L/min) for recursive step ``step`` (1 = hour after the last complete one)."""
    by_h = meta.get("residual_std_by_horizon")
    if by_h:
        return float(by_h[min(step, len(by_h)) - 1])
    one = meta.get("residual_std") or meta.get("rmse")
    return float(one) if one else None


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
        """Expected hourly-mean flow (L/min) of the slot; falls back to the median if absent."""
        s = slot_stats(slots, hour)
        return float(s.get("mean", s["median"])) if s else 0.0

    start = _floor_hour(now) - timedelta(hours=HISTORY_HOURS)
    sums: dict[datetime, tuple[float, int]] = {}
    for ts, flow, _vol in await ReadingRepository(session).series(flat.id, start, now):
        h = _floor_hour(ts)
        total, n = sums.get(h, (0.0, 0))
        sums[h] = (total + flow, n + 1)
    series: dict[datetime, float] = {h: total / n for h, (total, n) in sums.items()}
    series.pop(_floor_hour(now), None)  # current hour is incomplete

    meta = store.metadata.get("forecaster", {})
    use_model = store.forecaster is not None
    if not use_model:
        log.warning("forecast_using_baseline_fallback", reason=store.model_status.get("forecaster"))
    current = _floor_hour(now)
    preds = []
    # Recursive rollout exactly as evaluated in training: step 1 is the (incomplete) current
    # hour, whose prediction is fed back but not returned; returned hours are steps 2..H+1.
    for step in range(1, horizon_hours + 2):
        target = current + timedelta(hours=step - 1)
        s = slot_stats(slots, target)
        if use_model:
            try:
                x = np.asarray([forecast_features(series, target, baseline_lpm)])
                yhat = float(store.forecaster.predict(x)[0])
                if not math.isfinite(yhat):
                    raise ValueError("non-finite prediction")
            except Exception as exc:
                log.warning("forecast_model_failed_using_baseline", error=str(exc))
                yhat, use_model = baseline_lpm(target), False
        else:
            yhat = baseline_lpm(target)
        yhat = max(yhat, 0.0)
        series[target] = yhat  # feed back for multi-step lags
        if step == 1:
            continue
        sd = std_for_step(meta, step) if use_model else None
        if sd:
            half: float = Z95 * sd
        elif s:
            half = Z95 * (s.get("hourly_std") or MAD_SCALE * s["mad"])
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
            mape=(meta.get("holdout") or {}).get("mape") if use_model else None,
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

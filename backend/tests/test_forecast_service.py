"""Forecast service: recursive rollout, per-horizon bands, honest model status."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from app.models import FlowReading
from app.repositories import FlatRepository
from app.services import detector as d
from app.services import forecaster as fc
from app.services.forecaster import std_for_step
from tests.test_detector import write_manifest

NOW = datetime(2026, 3, 10, 14, 20, tzinfo=UTC)
STD = [0.1 * (i + 1) for i in range(48)]


def _train_forecaster(path):
    rng = np.random.default_rng(0)
    X = rng.uniform(0, 3, size=(300, 10))
    y = X[:, 0] * 0.5 + 0.2
    joblib.dump(HistGradientBoostingRegressor(max_iter=20).fit(X, y), path / "forecaster.joblib")
    (path / "forecaster_metadata.json").write_text(
        json.dumps(
            {
                "trained_at": "2026-02-02T00:00:00+00:00",
                "data_hash": "feedface00112233",
                "feature_names": fc.FORECAST_FEATURES,
                "holdout": {"mape": 0.31, "mae": 0.1},
                "residual_std": 0.1,
                "residual_std_by_horizon": STD,
            }
        )
    )
    write_manifest(path)


async def _history(db, flat_id):
    async with db.session() as s:
        for h in range(24 * 9):
            t = NOW.replace(minute=0) - timedelta(hours=h + 1)
            s.add(
                FlowReading(
                    flat_id=flat_id,
                    timestamp=t,
                    flow_rate=1.0 + (t.hour % 5) / 10,
                    volume=0.1,
                    message_id=f"{h:08x}",
                )
            )


def test_std_for_step_uses_per_horizon_and_clamps():
    assert std_for_step({"residual_std_by_horizon": STD}, 1) == STD[0]
    assert std_for_step({"residual_std_by_horizon": STD}, 25) == STD[24]
    assert std_for_step({"residual_std_by_horizon": STD}, 500) == STD[-1]
    assert std_for_step({"residual_std": 0.2}, 7) == 0.2 and std_for_step({}, 1) is None


async def test_forecast_uses_horizon_band_mape_and_version(db, world, tmp_path):
    _train_forecaster(tmp_path)
    store = d.ModelStore(tmp_path, manifest_required=True)
    fid = world.flats[0].id
    await _history(db, fid)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        out = await fc.generate_forecast(s, store, flat, now=NOW)
    p = out.predictions
    assert len(p) == 24 and p[0]["hour"].startswith("2026-03-10T15:00")
    # first returned hour is recursive step 2 (step 1 = the incomplete current hour)
    half0 = (p[0]["upper"] - p[0]["lower"]) / 2
    assert half0 <= 1.96 * STD[1] + 1e-3
    widths = [x["upper"] - x["lower"] for x in p]
    assert widths[-1] > widths[0]  # band widens with the horizon
    assert out.mape == 0.31  # nested under holdout
    assert out.model_version.startswith("2026-02-02T00:00:00+00:00+feedface+")


async def test_forecast_without_model_is_labelled_and_status_exposed(db, world, tmp_path):
    store = d.ModelStore(tmp_path)
    fid = world.flats[0].id
    await _history(db, fid)
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        out = await fc.generate_forecast(s, store, flat, now=NOW)
    assert out.model_version == "baseline-median" and out.mape is None
    assert store.model_status["forecaster"].startswith("baseline_fallback")


def test_first_step_lag_handling_matches_training_rollout():
    """Backend feature row for a rollout step equals the shared training definition."""
    series = {NOW.replace(minute=0) - timedelta(hours=h): 1.0 + h * 0.01 for h in range(1, 200)}
    target = NOW.replace(minute=0)  # step 1: lag_1 = last complete hour (real, not imputed)
    row = fc.forecast_features(series, target, lambda _t: -999.0)
    assert row[0] == series[target - timedelta(hours=1)] and -999.0 not in row

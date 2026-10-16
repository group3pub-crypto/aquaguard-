"""Train/serve parity: the ml pipeline and the backend must produce IDENTICAL features."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from aquaguard_features import FEATURE_NAMES, FORECAST_FEATURES, bin_to_minutes
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestClassifier
from sklearn.metrics import f1_score

pytest.importorskip("app.services.detector")  # backend installed (pip install -e backend)
from app.services import detector as backend  # noqa: E402
from app.services import forecaster as backend_fc  # noqa: E402

from ml.features.engineering import (  # noqa: E402
    WARMUP,
    build_baselines,
    extract_features,
    featurize_dataset,
)
from ml.scripts import train_forecaster  # noqa: E402
from ml.scripts.generate_data import START, generate_dataset  # noqa: E402

TOL = 1e-9


@pytest.fixture(scope="module")
def world():
    df = generate_dataset(4, 14, seed=21)
    split = START + pd.Timedelta(days=10)
    base = build_baselines(df, split)
    return df, split, base


def _backend_features(g: pd.DataFrame, i: int, slots: list[dict], dup: bool = False) -> dict:
    """Serve-side features for the window ending at row i, from raw readings (dropouts absent)."""
    lo = max(0, i - 200)
    ts, fl = g["ts"].iloc[lo : i + 1], g["flow"].iloc[lo : i + 1]
    readings = []
    for t, f in zip(ts, fl, strict=True):
        if np.isnan(f):
            continue
        readings.append(SimpleNamespace(timestamp=t.to_pydatetime(), flow_rate=float(f)))
        if dup:  # a second reading 30 s earlier with the same value: same 1-minute bin
            readings.append(
                SimpleNamespace(
                    timestamp=t.to_pydatetime() - timedelta(seconds=30), flow_rate=float(f)
                )
            )
    return backend.extract_features(readings, slots, g["ts"].iloc[i].to_pydatetime())


@pytest.mark.parametrize("dup", [False, True])
def test_feature_vectors_identical_between_ml_and_backend(world, dup):
    df, split, base = world
    rng = np.random.default_rng(0)
    for fid in ("flat-01", "flat-02"):
        g = df[df["flat_id"] == fid].sort_values("ts").reset_index(drop=True)
        ml = extract_features(pd.DatetimeIndex(g["ts"]), g["flow"].to_numpy(float), base[fid], 1)
        slots = json.loads(json.dumps(base[fid].to_slots()))  # DB round trip
        rows = np.r_[
            rng.integers(WARMUP, len(g), 25),
            # hour boundaries, night/day edges and the first test window
            [i for i in range(WARMUP, len(g)) if g["ts"].iloc[i].minute in (0, 1, 14, 15)][::97],
        ]
        for i in rows:
            want = ml.iloc[int(i) - WARMUP][FEATURE_NAMES].to_numpy(float)
            got = _backend_features(g, int(i), slots, dup)
            np.testing.assert_allclose(
                [got[k] for k in FEATURE_NAMES], want, rtol=0, atol=TOL, err_msg=f"{fid} row {i}"
            )


def test_night_definition_is_identical_on_both_sides(world):
    df, _, base = world
    w = featurize_dataset(df, base, stride=5)
    hours = w["hour_of_day"].astype(int)
    assert set(hours) == set(range(24))
    for h in range(24):
        assert bool(backend.is_night_hour(h)) == (0 <= h < 5)
    assert (w["is_night"] == hours.map(lambda h: float(0 <= h < 5))).all()


def test_saved_classifier_f1_is_identical_on_backend_features(world):
    """Regression for the audit finding (macro-F1 0.934 -> 0.833 when serving features differed)."""
    df, split, base = world
    w = featurize_dataset(df, base, stride=5)
    train, test = w[w["ts"] < split], w[w["ts"] >= split]
    clf = RandomForestClassifier(
        n_estimators=40, max_depth=12, class_weight="balanced", random_state=0
    )
    clf.fit(train[FEATURE_NAMES], train["label"])
    # held-out subset: every 6th test window per flat, plus every non-normal one (capped)
    pick = test.iloc[::6]
    faulty = test[test["label"] != "normal"].iloc[::7]
    sub = pd.concat([pick, faulty]).drop_duplicates(["ts", "flat_id"])
    series = {fid: g.sort_values("ts").reset_index(drop=True) for fid, g in df.groupby("flat_id")}
    rows = []
    for r in sub.itertuples():
        g = series[r.flat_id]
        i = int(g.index[g["ts"] == r.ts][0])
        f = _backend_features(g, i, base[r.flat_id].to_slots())
        rows.append([f[k] for k in FEATURE_NAMES])
    xb = np.asarray(rows)
    np.testing.assert_allclose(xb, sub[FEATURE_NAMES].to_numpy(float), rtol=0, atol=TOL)
    f1_ml = f1_score(sub["label"], clf.predict(sub[FEATURE_NAMES]), average="macro")
    f1_be = f1_score(sub["label"], clf.predict(xb), average="macro")
    assert abs(f1_ml - f1_be) < 1e-12 and f1_ml > 0.5


def test_forecast_rollout_matches_backend_feature_path():
    df = generate_dataset(1, 21, seed=4, inject_faults=False)
    h = train_forecaster.hourly_series(df)
    data = train_forecaster.build_forecast_features(h)
    assert list(data.columns) == ["ts", *FORECAST_FEATURES, "y"]
    model = HistGradientBoostingRegressor(max_iter=30, random_state=0).fit(
        data[FORECAST_FEATURES].to_numpy(), data["y"]
    )
    origin = len(h) - 60
    pred, _ = train_forecaster.rollout(model, h, np.array([origin]), steps=30)
    # backend emulation: history up to the origin hour, own predictions fed back
    series = {t.to_pydatetime(): float(v) for t, v in h.iloc[: origin + 1].items()}
    for k in range(1, 31):
        target = h.index[origin].to_pydatetime() + timedelta(hours=k)
        x = np.asarray([backend_fc.forecast_features(series, target, lambda _t: np.nan)])
        yhat = max(float(model.predict(x)[0]), 0.0)
        series[target] = yhat
        assert abs(yhat - pred[0, k - 1]) < TOL, f"step {k}"


def test_bin_to_minutes_matches_training_grid_for_aligned_readings():
    end = 1_700_000_000.0 - (1_700_000_000.0 % 60)
    ts = end - 60.0 * np.arange(10)[::-1]
    out = bin_to_minutes(ts, np.arange(10.0), end, 12)
    assert np.isnan(out[:2]).all() and (out[2:] == np.arange(10.0)).all()

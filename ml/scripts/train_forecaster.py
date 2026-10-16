"""Train the hourly demand forecaster (mean L/min per hour) on synthetic, fault-free data.

The model is trained on ONE-step rows (features relative to the TARGET hour t, using data up to
hour t-1: lag_1, lag_2, lag_24, lag_168, rolling_24h, hour/dow sin/cos, is_weekend) but SERVED
recursively (the backend feeds its own predictions back as lags). So it is also EVALUATED
recursively: for each holdout origin the model is rolled out ``MAX_HORIZON`` steps and the
residual std is estimated PER HORIZON STEP (``residual_std_by_horizon``), and the coverage of the
95% band is measured out-of-sample. Serving protocol: step 1 = the current (incomplete) hour,
whose prediction is fed back but not returned; returned hours are steps 2..H+1.

Feature definitions come from ``libs/aquaguard_features`` (shared with the backend). Synthetic
data is UTC and treated as local time. Data: in memory, or ``--data-dir`` (generate_data output).

Usage (repo root): python -m ml.scripts.train_forecaster --seed 42 --flats 12 --days 30
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from aquaguard_features import FORECAST_FEATURES, forecast_feature_matrix
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import TimeSeriesSplit

from ml.scripts.artifacts import update_manifest
from ml.scripts.generate_data import dataset_hash, generate_dataset, load_dataset

ML_DIR = Path(__file__).parents[1]
MAPE_EPS = 0.1  # L/min; guards division by ~0 at night
HOLDOUT_FRAC = 0.3
MAX_HORIZON = 48  # steps with their own residual std; later steps reuse the last value
Z95 = 1.96
REPORT_STEPS = (1, 2, 6, 12, 24, 25, 48)


def hourly_series(flat_df: pd.DataFrame) -> pd.Series:
    """Mean flow (L/min) per clock hour for a single flat (NaN dropout ignored)."""
    return flat_df.set_index("ts")["flow"].astype(float).resample("h").mean()


def build_forecast_features(hourly: pd.Series, append_next: bool = False) -> pd.DataFrame:
    """Row i describes TARGET hour ts_i; `y` is that hour's mean flow.

    With ``append_next`` one extra, unlabeled row (y=NaN) for the hour after the last observation
    is added: its last row is the serving input. Rows without a full 168 h history are dropped.
    """
    s = hourly.copy()
    if append_next:
        s.loc[s.index[-1] + pd.Timedelta(hours=1)] = np.nan
    ts = s.index
    m = forecast_feature_matrix(
        s.shift(1).to_numpy(),
        s.shift(2).to_numpy(),
        s.shift(24).to_numpy(),
        s.shift(168).to_numpy(),
        s.shift(1).rolling(24).mean().to_numpy(),
        ts.hour.to_numpy(),
        ts.dayofweek.to_numpy(),
    )
    f = pd.DataFrame(m, columns=FORECAST_FEATURES, index=ts)
    f["y"] = s
    f = f.dropna(subset=FORECAST_FEATURES)
    return f.rename_axis("ts").reset_index()


def make_model(seed: int):
    """sklearn HistGradientBoostingRegressor: no extra dependency in the backend image."""
    return "HistGradientBoostingRegressor", HistGradientBoostingRegressor(
        max_iter=300, learning_rate=0.05, random_state=seed
    )


def rollout(
    model, hourly: pd.Series, origins: np.ndarray, steps: int = MAX_HORIZON
) -> tuple[np.ndarray, np.ndarray]:
    """Recursive forecasts exactly as the backend serves them.

    ``origins`` are integer positions of the LAST COMPLETE hour. For step k the target is
    ``origins + k``; lags that fall after the origin are the model's own earlier predictions.
    Returns (pred, truth), both ``(len(origins), steps)``; truth is NaN where unobserved.
    """
    s = hourly.to_numpy(float)
    idx = hourly.index
    n = len(origins)
    buf = np.full((n, 168 + steps), np.nan)
    for i, o in enumerate(origins):
        buf[i, :168] = s[o - 167 : o + 1]
    pred = np.zeros((n, steps))
    truth = np.full((n, steps), np.nan)
    for k in range(1, steps + 1):
        j = 167 + k
        hour = idx.hour.to_numpy()[origins + k]
        dow = idx.dayofweek.to_numpy()[origins + k]
        x = forecast_feature_matrix(
            buf[:, j - 1],
            buf[:, j - 2],
            buf[:, j - 24],
            buf[:, j - 168],
            buf[:, j - 24 : j].mean(axis=1),
            hour,
            dow,
        )
        p = np.clip(model.predict(x), 0.0, None)
        buf[:, j] = p
        pred[:, k - 1] = p
        truth[:, k - 1] = s[origins + k]
    return pred, truth


def rollout_eval(model, hourly_by_flat: dict[str, pd.Series], cut: pd.Timestamp) -> dict:
    """Per-horizon residual statistics on the holdout (first target >= ``cut``). The first half of
    the origins (chronological) calibrates the band, the second half measures its coverage."""
    preds, truths, times = [], [], []
    for h in hourly_by_flat.values():
        pos = np.arange(len(h))
        ok = (
            (pos >= 167)
            & (pos + MAX_HORIZON < len(h))
            & (h.index[np.minimum(pos + 1, len(h) - 1)] >= cut)
        )
        o = pos[ok]
        if len(o):
            p, t = rollout(model, h, o)
            preds.append(p)
            truths.append(t)
            times.append(h.index[o])
    pred, truth = np.vstack(preds), np.vstack(truths)
    when = np.concatenate([np.asarray(t) for t in times])
    order = np.argsort(when, kind="stable")
    pred, truth = pred[order], truth[order]
    half = len(pred) // 2
    res = truth - pred

    def rms(r: np.ndarray) -> np.ndarray:
        return np.sqrt(np.nanmean(r**2, axis=0))

    std_all, std_cal = rms(res), rms(res[:half])
    ev = res[half:]
    cover_oos = np.nanmean(np.abs(ev) <= Z95 * std_cal, axis=0)
    cover_in = np.nanmean(np.abs(res) <= Z95 * std_all, axis=0)
    step1 = std_all[0]
    cover_naive = np.nanmean(np.abs(res) <= Z95 * step1, axis=0)
    return {
        "n_origins": int(len(pred)),
        "n_origins_calibration": int(half),
        "residual_std_by_horizon": [float(v) for v in std_all],
        "mae_by_horizon": [float(v) for v in np.nanmean(np.abs(res), axis=0)],
        "bias_by_horizon": [float(v) for v in np.nanmean(res, axis=0)],
        "coverage_oos_by_horizon": [float(v) for v in cover_oos],
        "coverage_in_sample_by_horizon": [float(v) for v in cover_in],
        "coverage_if_one_step_std_used": [float(v) for v in cover_naive],
        "coverage_oos_served_steps_2_25": float(np.mean(cover_oos[1:25])),
        "coverage_if_one_step_std_served_steps_2_25": float(np.mean(cover_naive[1:25])),
    }


def regression_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    e = y - p
    return {
        "mae": float(np.mean(np.abs(e))),
        "rmse": float(np.sqrt(np.mean(e**2))),
        "mape": float(np.mean(np.abs(e) / np.maximum(np.abs(y), MAPE_EPS))),
    }


def write_report(path: Path, meta: dict) -> None:
    h, b, cv, ro = meta["holdout"], meta["baseline_holdout"], meta["cv"], meta["rollout"]
    rows = "\n".join(
        f"| {k} | {ro['residual_std_by_horizon'][k - 1]:.4f} | "
        f"{ro['mae_by_horizon'][k - 1]:.4f} | {ro['coverage_oos_by_horizon'][k - 1]:.3f} | "
        f"{ro['coverage_in_sample_by_horizon'][k - 1]:.3f} | "
        f"{ro['coverage_if_one_step_std_used'][k - 1]:.3f} |"
        for k in REPORT_STEPS
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# Forecaster report (generated by `python -m ml.scripts.train_forecaster`)

> **SYNTHETIC data.** Trained and evaluated on simulated, fault-free household flow. Absolute
> errors will not transfer to real sensors; the prediction-interval width in particular must be
> recalibrated on real residuals.

- Trained: {meta["trained_at"]} | seed {meta["seed"]} | model: {meta["model_type"]}
- Data: {meta["n_flats"]} flats x {meta["days"]} days hourly (hash `{meta["data_hash"]}`); target =
  mean flow of the hour in **L/min** (not litres per hour)
- Split: chronological, first {meta["n_train"]} rows train (TimeSeriesSplit(3) CV), last
  {meta["n_holdout"]} rows holdout. Model trained on one-step rows.

## One-step holdout (lag features all observed)

| metric | model | naive seasonal (same hour last week) |
|---|---|---|
| MAE (L/min) | {h["mae"]:.4f} | {b["mae"]:.4f} |
| RMSE (L/min) | {h["rmse"]:.4f} | {b["rmse"]:.4f} |
| MAPE (eps={MAPE_EPS}) | {h["mape"]:.3f} | {b["mape"]:.3f} |

CV MAE per fold (model / naive): {
        ", ".join(f"{m:.4f} / {n:.4f}" for m, n in zip(cv["mae"], cv["naive_mae"], strict=True))
    }

## Recursive multi-step rollout (how the backend serves it)

Rolled out {MAX_HORIZON} steps from {ro["n_origins"]} holdout origins (all flats); the model's own
predictions are fed back as lags. Serving: step 1 is the current incomplete hour (not returned),
so returned hours are steps 2..H+1. `residual_std_by_horizon` is the RMS residual per step (stored
in `forecaster_metadata.json`; the backend band is +/-{Z95} x that, steps beyond {MAX_HORIZON}
reuse the last value). Coverage of the nominal 95% band:

- out-of-sample (std calibrated on the first {ro["n_origins_calibration"]} origins, coverage
  measured on the remaining ones), mean over served steps 2-25:
  **{ro["coverage_oos_served_steps_2_25"]:.3f}**
- the previous scheme (one-step std for every step), same steps:
  {ro["coverage_if_one_step_std_served_steps_2_25"]:.3f}

| step | std (L/min) | MAE | cov. out-of-sample | cov. in-sample | cov. one-step std |
|---|---|---|---|---|---|
{rows}

Notes: MAPE is dominated by near-zero night hours even with the epsilon guard; prefer MAE/RMSE.
The saved model is the train-split fit (not refit on the holdout) so reported numbers match it.
Randomness in the simulated data is irreducible, so no model can reach zero error here. Coverage
is only as good as the synthetic residual distribution; recalibrate on real data.
""")


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--flats", type=int, default=12)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--data-dir", type=Path, default=None, help="read generate_data output")
    ap.add_argument("--out", type=Path, default=ML_DIR / "models")
    ap.add_argument("--report-dir", type=Path, default=ML_DIR / "reports")
    a = ap.parse_args(argv)

    t0 = time.time()
    if a.data_dir:
        df = load_dataset(a.data_dir)
        df = df[df["fault"].astype(str) == "none"]  # forecaster is fault-free
        a.flats, a.days = (
            int(df["flat_id"].nunique()),
            int(len(df) // df["flat_id"].nunique() // 1440),
        )
    else:
        df = generate_dataset(a.flats, a.days, a.seed, inject_faults=False)
    hourly = {
        str(fid): hourly_series(g.sort_values("ts"))
        for fid, g in df.groupby("flat_id", observed=True)
    }
    parts = [build_forecast_features(h).assign(flat_id=fid) for fid, h in hourly.items()]
    data = pd.concat(parts).sort_values(["ts", "flat_id"], ignore_index=True)
    cut = int(len(data) * (1 - HOLDOUT_FRAC))
    train, hold = data.iloc[:cut], data.iloc[cut:]
    assert train["ts"].max() <= hold["ts"].min()
    cut_ts = hold["ts"].min()

    name, _ = make_model(a.seed)
    cv = {"mae": [], "naive_mae": []}
    for tr, va in TimeSeriesSplit(n_splits=3).split(train):
        m = make_model(a.seed)[1].fit(
            train.iloc[tr][FORECAST_FEATURES].to_numpy(), train.iloc[tr]["y"]
        )
        yv = train.iloc[va]["y"].to_numpy()
        cv["mae"].append(
            float(np.mean(np.abs(yv - m.predict(train.iloc[va][FORECAST_FEATURES].to_numpy()))))
        )
        cv["naive_mae"].append(float(np.mean(np.abs(yv - train.iloc[va]["lag_168"].to_numpy()))))

    model = make_model(a.seed)[1].fit(train[FORECAST_FEATURES].to_numpy(), train["y"])
    pred = np.clip(model.predict(hold[FORECAST_FEATURES].to_numpy()), 0.0, None)
    y = hold["y"].to_numpy()
    ro = rollout_eval(model, hourly, cut_ts)
    meta = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_source": "synthetic",
        "data_hash": dataset_hash(df),
        "seed": a.seed,
        "n_flats": a.flats,
        "days": a.days,
        "model_type": name,
        "feature_names": list(FORECAST_FEATURES),
        "target": "hourly_mean_flow_lpm",
        "serving_protocol": "recursive; step 1 = current incomplete hour (not returned)",
        "n_train": int(len(train)),
        "n_holdout": int(len(hold)),
        "holdout": regression_metrics(y, pred),
        "baseline_holdout": regression_metrics(y, hold["lag_168"].to_numpy()),
        "cv": cv,
        "residual_std": float(np.std(y - pred, ddof=1)),
        "residual_std_by_horizon": ro["residual_std_by_horizon"],
        "rollout": ro,
        "max_horizon": MAX_HORIZON,
        "mape_epsilon": MAPE_EPS,
        "sklearn_version": sklearn.__version__,
        "time_basis": "synthetic UTC treated as local time",
        "note": "Synthetic data; recalibrate on real sensors before trusting intervals.",
    }
    a.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, a.out / "forecaster.joblib")
    (a.out / "forecaster_metadata.json").write_text(json.dumps(meta, indent=2))
    update_manifest(a.out, ["forecaster.joblib", "forecaster_metadata.json"])
    write_report(a.report_dir / "forecaster_report.md", meta)
    print(f"[forecaster] {name} trained in {time.time() - t0:.0f}s")
    print(f"  one-step holdout model={meta['holdout']}")
    print(f"  naive={meta['baseline_holdout']}  one-step residual_std={meta['residual_std']:.4f}")
    print(
        f"  rollout origins={ro['n_origins']}  std step1/2/25/48="
        f"{[round(ro['residual_std_by_horizon'][k - 1], 3) for k in (1, 2, 25, 48)]}"
    )
    print(
        f"  95% band coverage served steps 2-25: out-of-sample "
        f"{ro['coverage_oos_served_steps_2_25']:.3f} vs one-step std "
        f"{ro['coverage_if_one_step_std_served_steps_2_25']:.3f}"
    )
    return meta


if __name__ == "__main__":
    main()

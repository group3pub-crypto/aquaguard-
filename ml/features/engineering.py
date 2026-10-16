"""Windowed feature extraction shared by training and serving.

A window is the last ``WINDOW`` (15) minutes ending at a given minute; features are strictly
causal (only data up to and including the window end). The per-flat hour-of-week baseline must
be fitted on NORMAL data from the TRAIN period only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

FEATURE_NAMES: list[str] = [
    "mean_flow",
    "max_flow",
    "min_flow",
    "std_flow",
    "baseline_deviation",
    "night_min_flow",
    "duration_above_base",
    "rate_of_change",
    "hour_of_day",
    "day_of_week",
    "is_night",
    "rolling_volume_1h",
    "pct_above_p95",
    "zero_flow_pct",
]
CLASSES = ["normal", "leak", "burst", "overuse"]
_LABEL_MAP = {
    "none": "normal",
    "slow_leak": "leak",
    "running_toilet": "leak",
    "dripping_tap": "leak",
    "burst": "burst",
    "overuse": "overuse",
}
WINDOW = 15
NIGHT_MIN_SPAN = 120  # minutes; trailing minimum is robust to a single flush
WARMUP = 120  # drop the first rows so trailing aggregates are full
SLOTS = 168
MIN_SCALE = 0.3  # L/min floor for robust scale (avoids division by ~0 at night)
ZERO_EPS = 0.01


def collapse_label(fault: str) -> str:
    return _LABEL_MAP[str(fault)]


def hour_of_week(ts: pd.DatetimeIndex) -> np.ndarray:
    return (ts.dayofweek * 24 + ts.hour).to_numpy()


@dataclass
class Baseline:
    """Per-flat hour-of-week (168 slots) distribution of per-minute flow, normal data only."""

    median: np.ndarray
    mad: np.ndarray
    p25: np.ndarray
    p75: np.ndarray
    p95: np.ndarray

    @property
    def scale(self) -> np.ndarray:
        robust = np.maximum(1.4826 * self.mad, (self.p75 - self.p25) / 1.349)
        return np.maximum(robust, MIN_SCALE)

    def to_dict(self) -> dict[str, list[float]]:
        return {k: getattr(self, k).tolist() for k in ("median", "mad", "p25", "p75", "p95")}

    @classmethod
    def from_dict(cls, d: dict[str, list[float]]) -> Baseline:
        return cls(**{k: np.asarray(v, dtype=float) for k, v in d.items()})


def fit_baseline(ts: pd.DatetimeIndex, flow: np.ndarray) -> Baseline:
    """Fit from the given (already filtered: normal, train-period) minutes."""
    s = pd.Series(np.asarray(flow, float), index=None)
    slot = pd.Series(hour_of_week(ts))
    ok = s.notna().to_numpy()
    s, slot = s[ok], slot[ok]
    g = s.groupby(slot)
    med = g.median().reindex(range(SLOTS))
    dev = (s - slot.map(med)).abs()
    stats = {
        "median": med,
        "mad": dev.groupby(slot).median().reindex(range(SLOTS)),
        "p25": g.quantile(0.25).reindex(range(SLOTS)),
        "p75": g.quantile(0.75).reindex(range(SLOTS)),
        "p95": g.quantile(0.95).reindex(range(SLOTS)),
    }
    return Baseline(**{k: v.fillna(0.0).to_numpy(float) for k, v in stats.items()})


def build_baselines(df: pd.DataFrame, train_end: pd.Timestamp) -> dict[str, Baseline]:
    """Baselines per flat from minutes with ts < train_end and no injected fault."""
    m = df[(df["ts"] < train_end) & (df["fault"].astype(str) == "none")]
    return {
        str(fid): fit_baseline(pd.DatetimeIndex(g["ts"]), g["flow"].to_numpy(float))
        for fid, g in m.groupby("flat_id", observed=True)
    }


def extract_features(
    ts: pd.DatetimeIndex, flow: np.ndarray, baseline: Baseline, stride: int = 5
) -> pd.DataFrame:
    """Features for windows ending every ``stride`` minutes. ``ts`` must be per-minute,
    contiguous and sorted; NaN (dropout) is forward-filled (causal)."""
    x = pd.Series(np.asarray(flow, float)).ffill().fillna(0.0)
    xv = x.to_numpy()
    slot = hour_of_week(ts)
    r = x.rolling(WINDOW)
    mean = r.mean()
    m5 = x.rolling(5).mean()
    above75 = pd.Series((xv > baseline.p75[slot]).astype(float)).rolling(WINDOW).sum()
    above95 = pd.Series((xv > baseline.p95[slot]).astype(float)).rolling(WINDOW).mean()
    zero = pd.Series((xv <= ZERO_EPS).astype(float)).rolling(WINDOW).mean()
    hour = ts.hour.to_numpy()
    feats = pd.DataFrame(
        {
            "mean_flow": mean,
            "max_flow": r.max(),
            "min_flow": r.min(),
            "std_flow": r.std(ddof=0),
            "baseline_deviation": (mean.to_numpy() - baseline.median[slot]) / baseline.scale[slot],
            "night_min_flow": x.rolling(NIGHT_MIN_SPAN, min_periods=1).min(),
            "duration_above_base": above75,
            "rate_of_change": (m5 - m5.shift(WINDOW - 5)) / (WINDOW - 5),
            "hour_of_day": hour.astype(float),
            "day_of_week": ts.dayofweek.to_numpy().astype(float),
            "is_night": (hour < 6).astype(float),
            "rolling_volume_1h": x.rolling(60, min_periods=1).sum(),
            "pct_above_p95": above95,
            "zero_flow_pct": zero,
        }
    )
    sel = np.arange(WARMUP, len(x), stride)
    out = feats.iloc[sel][FEATURE_NAMES].reset_index(drop=True)
    out.insert(0, "ts", ts[sel])
    return out


def featurize_dataset(
    df: pd.DataFrame, baselines: dict[str, Baseline], stride: int = 5
) -> pd.DataFrame:
    """Window table for all flats: ts, flat_id, FEATURE_NAMES, fault, label."""
    parts = []
    for fid, g in df.groupby("flat_id", observed=True, sort=True):
        g = g.sort_values("ts")
        f = extract_features(pd.DatetimeIndex(g["ts"]), g["flow"].to_numpy(float), baselines[fid],
                             stride)
        f.insert(1, "flat_id", fid)
        sel = np.arange(WARMUP, len(g), stride)
        f["fault"] = g["fault"].astype(str).to_numpy()[sel]
        parts.append(f)
    out = pd.concat(parts, ignore_index=True).sort_values(["ts", "flat_id"], ignore_index=True)
    out["label"] = out["fault"].map(collapse_label)
    return out


def fault_episodes(df: pd.DataFrame) -> pd.DataFrame:
    """Contiguous fault runs from minute-level data: flat_id, fault, start, end."""
    rows = []
    for fid, g in df.groupby("flat_id", observed=True):
        f = g.sort_values("ts")["fault"].astype(str).to_numpy()
        t = g.sort_values("ts")["ts"].to_numpy()
        change = np.flatnonzero(f[1:] != f[:-1]) + 1
        starts = np.r_[0, change]
        ends = np.r_[change, len(f)] - 1
        for s, e in zip(starts, ends, strict=True):
            if f[s] != "none":
                rows.append((str(fid), f[s], pd.Timestamp(t[s]), pd.Timestamp(t[e])))
    return pd.DataFrame(rows, columns=["flat_id", "fault", "start", "end"])


def time_to_detect(
    episodes: pd.DataFrame, windows: pd.DataFrame, pred: np.ndarray
) -> tuple[list[float], int]:
    """Minutes from episode start to first non-normal prediction inside it.
    Returns (detected delays, number of episodes considered)."""
    w = windows.assign(pred=pred)
    delays: list[float] = []
    for ep in episodes.itertuples():
        g = w[(w["flat_id"] == ep.flat_id) & (w["ts"] >= ep.start) & (w["ts"] <= ep.end)]
        hit = g[g["pred"] != "normal"]
        if len(hit):
            delays.append((hit["ts"].iloc[0] - ep.start).total_seconds() / 60.0)
    return delays, len(episodes)

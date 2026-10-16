"""Windowed feature extraction shared by training and serving.

A window is the last ``WINDOW`` (15) minutes ending at a given minute; features are strictly
causal (only data up to and including the window end). The per-flat hour-of-week baseline must
be fitted on NORMAL data from the TRAIN period only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from aquaguard_features import (
    FEATURE_NAMES,
    HISTORY,
    SLOTS,
    WINDOW,
    SlotBaseline,
    extract_window_features,
    fit_slot_baseline,
    slot_index,
)

# Feature definitions live in libs/aquaguard_features and are shared with the backend.
Baseline = SlotBaseline
CLASSES = ["normal", "leak", "burst", "overuse"]
_LABEL_MAP = {
    "none": "normal",
    "slow_leak": "leak",
    "running_toilet": "leak",
    "dripping_tap": "leak",
    "burst": "burst",
    "overuse": "overuse",
}
WARMUP = HISTORY  # drop the first rows so trailing aggregates are full

__all__ = [
    "CLASSES",
    "FEATURE_NAMES",
    "SLOTS",
    "WARMUP",
    "WINDOW",
    "Baseline",
    "build_baselines",
    "collapse_label",
    "extract_features",
    "fault_episodes",
    "featurize_dataset",
    "fit_baseline",
    "hour_of_week",
    "time_to_detect",
]


def collapse_label(fault: str) -> str:
    return _LABEL_MAP[str(fault)]


def hour_of_week(ts: pd.DatetimeIndex) -> np.ndarray:
    """Slot of each timestamp. Synthetic training data is UTC and treated as LOCAL time; the
    backend converts to settings.timezone before computing the identical slot."""
    return np.asarray(slot_index(ts.dayofweek.to_numpy(), ts.hour.to_numpy()))


def fit_baseline(ts: pd.DatetimeIndex, flow: np.ndarray) -> SlotBaseline:
    """Fit from the given (already filtered: normal, train-period) per-minute flows."""
    hour_id = (ts.floor("h").asi8 // 3_600_000_000_000) if len(ts) else np.array([], dtype=int)
    return fit_slot_baseline(hour_of_week(ts), np.asarray(flow, float), hour_id)


def build_baselines(df: pd.DataFrame, train_end: pd.Timestamp) -> dict[str, SlotBaseline]:
    """Baselines per flat from minutes with ts < train_end and no injected fault."""
    m = df[(df["ts"] < train_end) & (df["fault"].astype(str) == "none")]
    return {
        str(fid): fit_baseline(pd.DatetimeIndex(g["ts"]), g["flow"].to_numpy(float))
        for fid, g in m.groupby("flat_id", observed=True)
    }


def extract_features(
    ts: pd.DatetimeIndex, flow: np.ndarray, baseline: SlotBaseline, stride: int = 5
) -> pd.DataFrame:
    """Features for windows ending every ``stride`` minutes (from index ``WARMUP``). ``ts`` must be
    per-minute, contiguous and sorted; NaN (dropout) is forward-filled (causal, in the shared code).
    """
    x = np.asarray(flow, float)
    n = len(x)
    slot = hour_of_week(ts)
    sel = np.arange(WARMUP, n, stride)
    pad = HISTORY - 1
    xp = np.concatenate([np.full(pad, np.nan), x])
    sp = np.concatenate([np.zeros(pad, dtype=int), slot])
    view = np.lib.stride_tricks.sliding_window_view
    idx = sel  # window for end index i is padded[i : i + HISTORY]
    fh = view(xp, HISTORY)[idx]
    sh = view(sp, HISTORY)[idx]
    mat = extract_window_features(
        fh, sh, ts.hour.to_numpy()[sel], ts.dayofweek.to_numpy()[sel], baseline
    )
    out = pd.DataFrame(mat, columns=FEATURE_NAMES)
    out.insert(0, "ts", ts[sel])
    return out


def featurize_dataset(
    df: pd.DataFrame, baselines: dict[str, Baseline], stride: int = 5
) -> pd.DataFrame:
    """Window table for all flats: ts, flat_id, FEATURE_NAMES, fault, label."""
    parts = []
    for fid, g in df.groupby("flat_id", observed=True, sort=True):
        g = g.sort_values("ts")
        f = extract_features(
            pd.DatetimeIndex(g["ts"]), g["flow"].to_numpy(float), baselines[fid], stride
        )
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

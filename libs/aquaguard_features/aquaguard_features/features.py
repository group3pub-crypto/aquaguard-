"""Window features, hour-of-week baseline semantics and forecast feature rows.

Contract (identical in training and serving):

* Input is a PER-MINUTE flow series (L/min). Serving builds it with :func:`bin_to_minutes`
  (bins ``(t-1min, t]``, mean of readings in the bin); training data is already per-minute.
* A window is the last ``WINDOW`` (15) minutes ending at the window end minute; trailing
  aggregates look back ``HISTORY`` (120) minutes. Missing minutes (NaN) are forward-filled
  causally within that history (gaps before the first sample take that sample; all-missing = 0).
* The baseline is the per-flat distribution of RAW per-minute flows per hour-of-week slot
  (``slot = weekday*24 + hour``, Monday = 0) in LOCAL time: median, mad, p25, p75, p95, count.
  Slots with ``count == 0`` have no baseline (baseline features are 0 for those minutes).
* Night is local hour in [0, 5) (00:00-05:00), see :func:`is_night_hour`.
* Timezone conversion is NOT done here: callers pass local weekday/hour. The backend does it in
  ``app.services.baseline.local``; synthetic training data is UTC and treated as local.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "FEATURE_NAMES",
    "FORECAST_FEATURES",
    "HISTORY",
    "MAD_SCALE",
    "MIN_SCALE",
    "NIGHT_END_HOUR",
    "NIGHT_START_HOUR",
    "SLOTS",
    "WINDOW",
    "ZERO_EPS",
    "SlotBaseline",
    "bin_to_minutes",
    "extract_window_features",
    "extract_window_features_one",
    "fit_slot_baseline",
    "forecast_feature_matrix",
    "is_night_hour",
    "slot_index",
]

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
FORECAST_FEATURES: list[str] = [
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
WINDOW = 15
HISTORY = 120  # minutes of look-back (trailing minimum of flow)
VOLUME_SPAN = 60
SLOTS = 168
MIN_SCALE = 0.3  # L/min floor for the robust scale (avoids dividing by ~0 at night)
MAD_SCALE = 1.4826
ZERO_EPS = 0.01
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 5  # exclusive


def is_night_hour(hour: int | np.ndarray) -> bool | np.ndarray:
    """Documented night: local 00:00-05:00."""
    return (hour >= NIGHT_START_HOUR) & (hour < NIGHT_END_HOUR)


def slot_index(weekday: int | np.ndarray, hour: int | np.ndarray) -> int | np.ndarray:
    """Hour-of-week slot from LOCAL weekday (Mon=0) and hour."""
    return weekday * 24 + hour


@dataclass(frozen=True)
class SlotBaseline:
    """168-slot distribution of raw per-minute flow. ``mean``/``hourly_std`` describe hourly means
    (used only by the forecaster fallback)."""

    median: np.ndarray
    mad: np.ndarray
    p25: np.ndarray
    p75: np.ndarray
    p95: np.ndarray
    count: np.ndarray
    mean: np.ndarray
    hourly_std: np.ndarray

    @property
    def scale(self) -> np.ndarray:
        robust = np.maximum(MAD_SCALE * self.mad, (self.p75 - self.p25) / 1.349)
        return np.maximum(robust, MIN_SCALE)

    _KEYS = ("median", "mad", "p25", "p75", "p95", "count", "mean", "hourly_std")

    def to_slots(self) -> list[dict]:
        """DB representation: list of 168 dicts (unrounded so train/serve match exactly)."""
        return [
            {"slot": i, **{k: float(getattr(self, k)[i]) for k in self._KEYS if k != "count"}}
            | {"sample_count": int(self.count[i])}
            for i in range(SLOTS)
        ]

    @classmethod
    def from_slots(cls, slots: list[dict] | None) -> SlotBaseline | None:
        if not slots or len(slots) != SLOTS:
            return None

        def col(k: str, alt: str | None = None) -> np.ndarray:
            return np.asarray([float(s.get(k, s.get(alt, 0.0) if alt else 0.0)) for s in slots])

        median = col("median")
        return cls(
            median=median,
            mad=col("mad"),
            p25=col("p25"),
            p75=col("p75"),
            p95=col("p95"),
            count=np.asarray([int(s.get("sample_count", 0)) for s in slots], dtype=float),
            mean=np.asarray([float(s.get("mean", s.get("median", 0.0))) for s in slots]),
            hourly_std=col("hourly_std"),
        )

    def to_dict(self) -> dict[str, list[float]]:
        return {k: getattr(self, k).tolist() for k in self._KEYS}

    @classmethod
    def from_dict(cls, d: dict[str, list[float]]) -> SlotBaseline:
        z = np.zeros(SLOTS)
        return cls(**{k: np.asarray(d.get(k, z), dtype=float) for k in cls._KEYS})


def fit_slot_baseline(
    slot: np.ndarray, flow: np.ndarray, hour_id: np.ndarray | None = None
) -> SlotBaseline:
    """Baseline from RAW per-minute flows. ``slot`` is the local hour-of-week of each minute;
    ``hour_id`` (optional) identifies the calendar hour of each minute, enabling the hourly-mean
    statistics (otherwise ``mean`` = minute mean and ``hourly_std`` = 0)."""
    slot = np.asarray(slot, dtype=int)
    flow = np.asarray(flow, dtype=float)
    ok = np.isfinite(flow)
    slot, flow = slot[ok], flow[ok]
    hid = None if hour_id is None else np.asarray(hour_id)[ok]
    out = {k: np.zeros(SLOTS) for k in SlotBaseline._KEYS}
    order = np.argsort(slot, kind="stable")
    slot_s, flow_s = slot[order], flow[order]
    bounds = np.searchsorted(slot_s, np.arange(SLOTS + 1))
    for i in range(SLOTS):
        v = flow_s[bounds[i] : bounds[i + 1]]
        if v.size == 0:
            continue
        med = np.median(v)
        out["median"][i] = med
        out["mad"][i] = np.median(np.abs(v - med))
        out["p25"][i], out["p75"][i], out["p95"][i] = np.percentile(v, [25, 75, 95])
        out["count"][i] = v.size
        out["mean"][i] = v.mean()
    if hid is not None:
        hs = hid[order]
        for i in range(SLOTS):
            sel = slice(bounds[i], bounds[i + 1])
            if bounds[i] == bounds[i + 1]:
                continue
            _, inv = np.unique(hs[sel], return_inverse=True)
            sums = np.bincount(inv, weights=flow_s[sel])
            cnt = np.bincount(inv)
            hm = sums / cnt
            out["mean"][i] = hm.mean()
            out["hourly_std"][i] = hm.std(ddof=1) if hm.size > 1 else 0.0
    return SlotBaseline(**out)


def bin_to_minutes(
    ts_epoch_s: np.ndarray, flow: np.ndarray, end_epoch_s: float, n_minutes: int = HISTORY
) -> np.ndarray:
    """Per-minute series of length ``n_minutes`` whose last bin ends at ``end_epoch_s``.
    Bin k (0 = oldest) covers ``(end - (n-k)*60, end - (n-k-1)*60]``; value = mean of readings in
    it, NaN if none. Readings after ``end`` are ignored (causal)."""
    out = np.full(n_minutes, np.nan)
    ts = np.asarray(ts_epoch_s, dtype=float)
    fl = np.asarray(flow, dtype=float)
    k = n_minutes - 1 - np.floor((end_epoch_s - ts) / 60.0 + 1e-9).astype(int)
    # a reading exactly at a bin end belongs to that bin: floor((end-ts)/60) handles it
    ok = (ts <= end_epoch_s + 1e-9) & (k >= 0) & (k < n_minutes) & np.isfinite(fl)
    if ok.any():
        sums = np.bincount(k[ok], weights=fl[ok], minlength=n_minutes)
        cnt = np.bincount(k[ok], minlength=n_minutes)
        has = cnt > 0
        out[has] = sums[has] / cnt[has]
    return out


def _ffill_rows(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Causal forward fill along axis 1. Gaps BEFORE the first valid sample (start of history, no
    earlier data to carry forward) take that first valid value for the 15-min window statistics;
    the returned mask marks them so the trailing minimum/volume can ignore them (as 'unknown').
    An all-NaN row becomes 0."""
    cols = np.arange(x.shape[1])[None, :]
    idx = np.maximum.accumulate(np.where(np.isnan(x), -1, cols), axis=1)
    out = np.take_along_axis(x, np.maximum(idx, 0), axis=1)
    lead = idx < 0
    if lead.any():
        first = np.argmax(~np.isnan(x), axis=1)
        first_val = np.where(np.isnan(x).all(axis=1), 0.0, x[np.arange(x.shape[0]), first])
        out = np.where(lead, first_val[:, None], out)
    return out, lead


def extract_window_features(
    flow_hist: np.ndarray,
    slot_hist: np.ndarray,
    hour: np.ndarray,
    weekday: np.ndarray,
    baseline: SlotBaseline | None,
) -> np.ndarray:
    """Feature matrix ``(N, 14)`` in ``FEATURE_NAMES`` order.

    ``flow_hist``/``slot_hist`` are ``(N, HISTORY)`` per-minute flow and local hour-of-week slot,
    oldest to newest, the last column being the window-end minute. ``hour``/``weekday`` are the
    local hour and weekday of that minute. ``baseline`` None => baseline features are 0.
    """
    fh = np.asarray(flow_hist, dtype=float).reshape(-1, HISTORY)
    sh = np.asarray(slot_hist, dtype=int).reshape(-1, HISTORY)
    hour = np.asarray(hour, dtype=float).reshape(-1)
    weekday = np.asarray(weekday, dtype=float).reshape(-1)
    x, lead = _ffill_rows(fh)
    night_min = np.where(lead.all(axis=1), 0.0, np.where(lead, np.inf, x).min(axis=1))
    w = x[:, -WINDOW:]
    ws = sh[:, -WINDOW:]
    mean = w.mean(axis=1)
    f = np.zeros((fh.shape[0], len(FEATURE_NAMES)))
    f[:, 0] = mean
    f[:, 1] = w.max(axis=1)
    f[:, 2] = w.min(axis=1)
    f[:, 3] = w.std(axis=1)
    if baseline is not None:
        have = baseline.count[ws] > 0
        end_slot = ws[:, -1]
        scale = baseline.scale[end_slot]
        dev = (mean - baseline.median[end_slot]) / scale
        f[:, 4] = np.where(have[:, -1], dev, 0.0)
        f[:, 6] = ((w > baseline.p75[ws]) & have).sum(axis=1)
        f[:, 12] = ((w > baseline.p95[ws]) & have).mean(axis=1)
    f[:, 5] = night_min
    f[:, 7] = (w[:, -5:].mean(axis=1) - w[:, :5].mean(axis=1)) / (WINDOW - 5)
    f[:, 8] = hour
    f[:, 9] = weekday
    f[:, 10] = is_night_hour(hour).astype(float)
    f[:, 11] = np.where(lead, 0.0, x)[:, -VOLUME_SPAN:].sum(axis=1)
    f[:, 13] = (w <= ZERO_EPS).mean(axis=1)
    return f


def extract_window_features_one(
    flow_hist: np.ndarray,
    slot_hist: np.ndarray,
    hour: int,
    weekday: int,
    baseline: SlotBaseline | None,
) -> dict[str, float]:
    row = extract_window_features(
        np.asarray(flow_hist)[None, :], np.asarray(slot_hist)[None, :], [hour], [weekday], baseline
    )[0]
    return {k: float(v) for k, v in zip(FEATURE_NAMES, row, strict=True)}


def forecast_feature_matrix(
    lag_1: np.ndarray,
    lag_2: np.ndarray,
    lag_24: np.ndarray,
    lag_168: np.ndarray,
    rolling_24h: np.ndarray,
    hour: np.ndarray,
    weekday: np.ndarray,
) -> np.ndarray:
    """Forecast feature rows ``(N, 10)`` in ``FORECAST_FEATURES`` order (local hour/weekday of
    the TARGET hour; lags are hourly mean L/min)."""
    hour = np.asarray(hour, dtype=float)
    weekday = np.asarray(weekday, dtype=float)
    return np.column_stack(
        [
            lag_1,
            lag_2,
            lag_24,
            lag_168,
            rolling_24h,
            np.sin(2 * math.pi * hour / 24),
            np.cos(2 * math.pi * hour / 24),
            np.sin(2 * math.pi * weekday / 7),
            np.cos(2 * math.pi * weekday / 7),
            (weekday >= 5).astype(float),
        ]
    )

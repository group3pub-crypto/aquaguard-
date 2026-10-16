"""Unit tests for libs/aquaguard_features."""

from __future__ import annotations

import numpy as np
import pandas as pd
from aquaguard_features import (
    FEATURE_NAMES,
    HISTORY,
    SlotBaseline,
    extract_window_features,
    fit_slot_baseline,
    is_night_hour,
    slot_index,
)


def _one(x, slot=0, hour=12, dow=2, base=None):
    x = np.asarray(x, float)
    return extract_window_features(x[None], np.full((1, HISTORY), slot), [hour], [dow], base)[0]


def test_night_is_local_00_to_05_and_slot_index():
    assert [bool(is_night_hour(h)) for h in (0, 4, 5, 12, 22, 23)] == [1, 1, 0, 0, 0, 0]
    assert slot_index(0, 0) == 0 and slot_index(6, 23) == 167


def test_baseline_matches_pandas_reference():
    rng = np.random.default_rng(1)
    slot = rng.integers(0, 168, 20000)
    flow = rng.gamma(2, 1, 20000)
    b = fit_slot_baseline(slot, flow)
    s = pd.Series(flow).groupby(slot)
    np.testing.assert_allclose(b.median[s.median().index], s.median().to_numpy())
    np.testing.assert_allclose(b.p95[s.quantile(0.95).index], s.quantile(0.95).to_numpy())
    assert b.count.sum() == 20000


def test_unobserved_slot_has_no_baseline_features():
    b = fit_slot_baseline(np.array([5] * 50), np.ones(50))
    f = dict(zip(FEATURE_NAMES, _one(np.ones(HISTORY), slot=3, base=b), strict=True))
    assert f["baseline_deviation"] == f["duration_above_base"] == f["pct_above_p95"] == 0.0


def test_features_are_causal_and_window_is_last_15():
    x = np.zeros(HISTORY)
    x[-16] = 99.0  # just outside the window
    f = dict(zip(FEATURE_NAMES, _one(x), strict=True))
    assert f["max_flow"] == 0.0 and f["zero_flow_pct"] == 1.0
    assert f["night_min_flow"] == 0.0 and f["rolling_volume_1h"] == 99.0


def test_slots_dict_roundtrip_is_exact():
    rng = np.random.default_rng(2)
    b = fit_slot_baseline(rng.integers(0, 168, 5000), rng.random(5000))
    b2 = SlotBaseline.from_slots(b.to_slots())
    for k in ("median", "mad", "p25", "p75", "p95", "count"):
        np.testing.assert_array_equal(getattr(b, k), getattr(b2, k))

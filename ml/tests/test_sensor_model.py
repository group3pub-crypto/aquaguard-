"""YF-S201 sensor model math, profiles and determinism (all SYNTHETIC)."""

import numpy as np
import pandas as pd
import pytest

from ml.scripts import generate_data as gd
from ml.scripts.generate_data import (
    PULSES_PER_LITRE,
    SensorSpec,
    apply_sensor_model,
    dataset_hash,
    generate_dataset,
)

# every non-deterministic effect off: only threshold + pulse counting remain
CLEAN = SensorSpec(
    min_flow_lpm=1.0,
    min_flow_spread=0.0,
    cal_error=0.0,
    drift_per_30d=0.0,
    pulse_loss=0.0,
    loss_event_rate_per_day=0.0,
    jitter_s=0.0,
    outage_rate_per_day=0.0,
    stuck_rate_per_day=0.0,
)
DEV = gd.DeviceParams(gain=1.0, drift=0.0, min_flow=1.0)


def run(flow, spec=CLEAN, dev=DEV, seed=0):
    return apply_sensor_model(np.asarray(flow, float), np.random.default_rng(seed), spec, dev)


def test_pulses_per_litre_is_450():
    # 7.5 Hz per L/min -> 7.5 * 60 = 450 pulses per minute per L/min = 450 pulses per litre
    assert PULSES_PER_LITRE == 7.5 * 60
    # one pulse resolves 1/450 L/min per (pulse/min): ~0.0022, NOT 0.133
    assert 1 / PULSES_PER_LITRE == pytest.approx(0.00222, abs=1e-5)


def test_integer_pulse_counts_exact_for_round_flows():
    out = run(np.full(500, 2.0))
    assert np.allclose(out, 2.0)  # 900 pulses/min exactly
    assert np.allclose(out * PULSES_PER_LITRE, np.round(out * PULSES_PER_LITRE))  # integers


def test_quantisation_step_and_unbiased_rounding():
    f = 1.2345  # 555.525 expected pulses/min -> 555 or 556
    out = run(np.full(20000, f), seed=3)
    counts = out * PULSES_PER_LITRE
    assert np.allclose(counts, np.round(counts))
    assert set(np.unique(np.round(counts))) <= {555.0, 556.0}
    assert out.mean() == pytest.approx(f, abs=2e-4)  # random-phase rounding is unbiased
    assert np.abs(out - f).max() <= 1 / PULSES_PER_LITRE + 1e-9


def test_start_up_threshold_hides_sub_threshold_flow():
    flow = np.array([0.0, 0.05, 0.5, 0.99, 1.0, 1.5, 3.0])
    out = run(flow)
    assert np.all(out[:4] == 0.0)  # below 1 L/min: no pulses at all
    assert np.allclose(out[4:], flow[4:], atol=1 / PULSES_PER_LITRE)


def test_threshold_is_configurable_and_per_device():
    flow = np.full(50, 0.4)
    assert run(flow, dev=gd.DeviceParams(1.0, 0.0, 0.3)).mean() == pytest.approx(0.4, abs=0.01)
    assert run(flow, dev=gd.DeviceParams(1.0, 0.0, 0.5)).max() == 0.0
    devs = {gd.make_device(np.random.default_rng(i), gd.YFS201).min_flow for i in range(20)}
    assert len(devs) > 10 and all(0.85 <= d <= 1.15 for d in devs)


def test_calibration_gain_and_drift_scale_the_reading():
    flow = np.full(100, 3.0)
    assert run(flow, dev=gd.DeviceParams(1.1, 0.0, 1.0)).mean() == pytest.approx(3.3, rel=1e-3)
    drift = run(np.full(30 * 1440, 3.0), dev=gd.DeviceParams(1.0, 0.03, 1.0))
    assert drift[-1440:].mean() > drift[:1440].mean() * 1.02


def test_deterministic_given_seed_and_sensitive_to_it():
    flow = np.abs(np.random.default_rng(1).normal(2.0, 2.0, 5000))
    a = run(flow, gd.YFS201, gd.make_device(np.random.default_rng(5), gd.YFS201), seed=9)
    b = run(flow, gd.YFS201, gd.make_device(np.random.default_rng(5), gd.YFS201), seed=9)
    c = run(flow, gd.YFS201, gd.make_device(np.random.default_rng(5), gd.YFS201), seed=10)
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(np.nan_to_num(a), np.nan_to_num(c))


def test_stuck_and_outage_effects():
    flow = np.full(3000, 5.0)
    stuck = run(flow, SensorSpec(**{**CLEAN.__dict__, "stuck_rate_per_day": 3.0}))
    assert (stuck == 0).any() or (np.diff(stuck[stuck > 0]) == 0).all()
    assert (stuck != 5.0).any()
    out = run(flow, SensorSpec(**{**CLEAN.__dict__, "outage_rate_per_day": 3.0}))
    assert np.isnan(out).any()


def test_default_generator_unchanged():
    # explicit defaults must equal the implicit ones (existing models/reports depend on them)
    a, b = (
        generate_dataset(2, 5, 11),
        generate_dataset(2, 5, 11, profile="default", sensor_model="none"),
    )
    pd.testing.assert_frame_equal(a, b)


def test_sensor_model_keeps_labels_and_changes_flow():
    a = generate_dataset(2, 6, 4)
    b = generate_dataset(2, 6, 4, sensor_model="yfs201")
    assert (a["fault"].astype(str) == b["fault"].astype(str)).all()  # same episodes
    assert not np.allclose(a["flow"].fillna(0), b["flow"].fillna(0))
    # sub-threshold readings vanish: far fewer small positive values
    small = lambda d: ((d["flow"] > 0) & (d["flow"] < 0.5)).sum()  # noqa: E731
    assert small(b) < small(a) or small(a) == 0
    pd.testing.assert_frame_equal(b, generate_dataset(2, 6, 4, sensor_model="yfs201"))


def test_yfs_min_flow_override():
    base = generate_dataset(1, 4, 2, sensor_model="yfs201")
    low = generate_dataset(1, 4, 2, sensor_model="yfs201", yfs_min_flow=0.0)
    assert (low["flow"] > 0).sum() > (base["flow"] > 0).sum()


def test_shifted_profile_is_a_different_world():
    d, s = generate_dataset(6, 10, 7), generate_dataset(6, 10, 7, profile="shifted")
    hs = gd.make_households(40, 7, "shifted")
    assert {h.schedule for h in hs} <= set(gd.SHIFTED_PROFILE.schedules)
    assert {h.schedule for h in hs}.isdisjoint(gd.SCHEDULES)

    def night(df):
        n = df[(df["fault"].astype(str) == "none") & df["ts"].dt.hour.between(1, 4)]["flow"]
        return n.mean()

    assert night(s) > 2 * night(d)  # 4x nocturnal use in the shifted world
    pd.testing.assert_frame_equal(s, generate_dataset(6, 10, 7, profile="shifted"))
    assert dataset_hash(s) != dataset_hash(d)


def test_cli_options(tmp_path):
    gd.main(
        [
            "--flats",
            "1",
            "--days",
            "2",
            "--out",
            str(tmp_path),
            "--profile",
            "shifted",
            "--sensor-model",
            "yfs201",
            "--yfs-min-flow",
            "0.5",
        ]
    )
    assert list(tmp_path.iterdir())

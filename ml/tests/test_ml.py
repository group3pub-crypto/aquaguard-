import json

import numpy as np
import pandas as pd
import pytest

from ml.features.engineering import (
    CLASSES,
    FEATURE_NAMES,
    build_baselines,
    collapse_label,
    fault_episodes,
    featurize_dataset,
)
from ml.scripts import demo_baseline, train_detector, train_forecaster
from ml.scripts.generate_data import (
    FAULT_TYPES,
    START,
    dataset_hash,
    generate_dataset,
    make_households,
)


@pytest.fixture(scope="module")
def df():
    return generate_dataset(4, 24, seed=3)


def test_deterministic_generation():
    a, b = generate_dataset(2, 5, 11), generate_dataset(2, 5, 11)
    pd.testing.assert_frame_equal(a, b)
    assert dataset_hash(a) == dataset_hash(b)
    assert dataset_hash(generate_dataset(2, 5, 12)) != dataset_hash(a)


def test_households_have_personalities():
    hs = make_households(40, 1)
    assert {h.schedule for h in hs} == {"early_bird", "night_owl", "regular", "wfh"}
    assert all(1 <= h.family_size <= 5 for h in hs)


def test_dropout_rate_and_nights_near_zero(df):
    assert 0.002 < df["flow"].isna().mean() < 0.01
    normal = df[df["fault"].astype(str) == "none"]
    night = normal[normal["ts"].dt.hour.between(1, 4)]["flow"]
    assert night.mean() < 0.1


def test_fault_labels_contiguous_and_ordered(df):
    eps = fault_episodes(df)
    assert len(eps) >= 8
    for _, g in eps.groupby("flat_id"):
        g = g.sort_values("start")
        assert (g["start"].iloc[1:].to_numpy() > g["end"].iloc[:-1].to_numpy()).all()
    # every episode is one contiguous run of a single label
    for e in eps.itertuples():
        seg = df[(df["flat_id"] == e.flat_id) & (df["ts"].between(e.start, e.end))]
        assert (seg["fault"].astype(str) == e.fault).all()
        assert len(seg) == int((e.end - e.start).total_seconds() / 60) + 1


def test_all_fault_types_and_durations(df):
    big = generate_dataset(12, 30, 5)
    eps = fault_episodes(big)
    assert set(eps["fault"]) == set(FAULT_TYPES)
    dur = (eps["end"] - eps["start"]).dt.total_seconds() / 60 + 1
    bursts = dur[eps["fault"] == "burst"]
    assert bursts.between(10, 120).all()


def test_fault_signature_levels(df):
    n = df[df["fault"].astype(str) == "none"]["flow"].mean()
    burst = df[df["fault"].astype(str) == "burst"]["flow"]
    if len(burst):
        assert burst.mean() > 10 * n


def test_collapse_labels():
    assert {collapse_label(f) for f in ["none", *FAULT_TYPES]} == set(CLASSES)
    assert collapse_label("running_toilet") == collapse_label("dripping_tap") == "leak"


def test_feature_names_exact():
    assert len(FEATURE_NAMES) == 14
    assert FEATURE_NAMES[0] == "mean_flow" and FEATURE_NAMES[-1] == "zero_flow_pct"
    assert FEATURE_NAMES[4] == "baseline_deviation"


def test_features_shape_finite(df):
    split = START + pd.Timedelta(days=16)
    w = featurize_dataset(df, build_baselines(df, split), stride=5)
    assert list(w[FEATURE_NAMES].columns) == FEATURE_NAMES
    assert np.isfinite(w[FEATURE_NAMES].to_numpy()).all()
    assert w["label"].isin(CLASSES).all()
    assert w["pct_above_p95"].between(0, 1).all() and w["zero_flow_pct"].between(0, 1).all()


def test_baseline_uses_train_normal_only(df):
    split = START + pd.Timedelta(days=16)
    ref = build_baselines(df, split)
    mod = df.copy()
    # corrupt (a) all post-split data and (b) faulty minutes before the split
    mod.loc[mod["ts"] >= split, "flow"] = 999.0
    bad = (mod["ts"] < split) & (mod["fault"].astype(str) != "none")
    mod.loc[bad, "flow"] = 999.0
    new = build_baselines(mod, split)
    for fid in ref:
        for k in ("median", "mad", "p25", "p75", "p95"):
            np.testing.assert_array_equal(getattr(ref[fid], k), getattr(new[fid], k))
    assert all(len(b.median) == 168 for b in ref.values())


def test_causal_features_ignore_future(df):
    split = START + pd.Timedelta(days=16)
    g = df[df["flat_id"] == "flat-01"]
    base = build_baselines(g, split)["flat-01"]
    from ml.features.engineering import extract_features

    full = extract_features(pd.DatetimeIndex(g["ts"]), g["flow"].to_numpy(float), base)
    cut = g.iloc[: 10 * 1440]
    part = extract_features(pd.DatetimeIndex(cut["ts"]), cut["flow"].to_numpy(float), base)
    np.testing.assert_allclose(
        full.iloc[: len(part)][FEATURE_NAMES].to_numpy(), part[FEATURE_NAMES].to_numpy()
    )


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    out = tmp_path_factory.mktemp("models")
    rep = tmp_path_factory.mktemp("reports")
    meta = train_detector.main(
        [
            "--flats",
            "6",
            "--days",
            "28",
            "--seed",
            "5",
            "--quick",
            "--out",
            str(out),
            "--report-dir",
            str(rep),
        ]
    )
    return out, rep, meta


def test_no_train_test_leakage():
    df = generate_dataset(3, 20, 2)
    train, test, _, split = train_detector.prepare(df, 20, 5)
    assert train["ts"].max() < split <= test["ts"].min()
    assert train["ts"].max() < test["ts"].min()


def test_classifier_beats_chance(tiny):
    _, _, meta = tiny
    m = meta["metrics"]
    assert m["macro_f1"] > 0.5  # chance for 4 classes is <= 0.25
    assert m["per_class"]["normal"]["recall"] > 0.9
    assert m["false_positive_rate_normal"] < 0.1


def test_metadata_schema_and_artifacts(tiny):
    out, rep, meta = tiny
    for f in (
        "isolation_forest.joblib",
        "classifier.joblib",
        "feature_names.joblib",
        "detector_metadata.json",
        "baselines.json",
    ):
        assert (out / f).exists()
    saved = json.loads((out / "detector_metadata.json").read_text())
    for k in (
        "trained_at",
        "data_hash",
        "n_train",
        "n_test",
        "seed",
        "hyperparams",
        "metrics",
        "sklearn_version",
        "feature_names",
    ):
        assert k in saved
    assert saved["feature_names"] == FEATURE_NAMES
    assert saved["n_iso_fit"] <= saved["n_train"]
    assert "SYNTHETIC" in (rep / "detector_report.md").read_text()


def test_forecaster_feature_builder_shape_and_alignment():
    df = generate_dataset(1, 14, 4, inject_faults=False)
    h = train_forecaster.hourly_series(df)
    f = train_forecaster.build_forecast_features(h)
    assert list(f.columns) == ["ts", *train_forecaster.FORECAST_FEATURES, "y"]
    assert len(f) == len(h) - 168
    assert np.isfinite(f[train_forecaster.FORECAST_FEATURES].to_numpy()).all()
    row = f.iloc[10]
    t = row["ts"]
    assert row["lag_1"] == h[t - pd.Timedelta(hours=1)]
    assert row["lag_168"] == h[t - pd.Timedelta(hours=168)]
    assert row["y"] == h[t]


def test_forecaster_serving_row():
    df = generate_dataset(1, 14, 4, inject_faults=False)
    h = train_forecaster.hourly_series(df)
    f = train_forecaster.build_forecast_features(h, append_next=True)
    assert f["ts"].iloc[-1] == h.index[-1] + pd.Timedelta(hours=1)
    assert np.isnan(f["y"].iloc[-1])
    assert f["lag_1"].iloc[-1] == h.iloc[-1]


def test_forecaster_trains_and_metadata(tmp_path):
    meta = train_forecaster.main(
        ["--flats", "3", "--days", "21", "--out", str(tmp_path), "--report-dir", str(tmp_path)]
    )
    assert (tmp_path / "forecaster.joblib").exists()
    saved = json.loads((tmp_path / "forecaster_metadata.json").read_text())
    assert saved["residual_std"] > 0 and np.isfinite(saved["holdout"]["mape"])
    assert saved["feature_names"] == train_forecaster.FORECAST_FEATURES
    assert meta["holdout"]["mae"] < meta["baseline_holdout"]["mae"] * 1.1


def test_demo_baseline_claim_holds():
    r = demo_baseline.run_demo()
    assert r["cap_missed"] and r["daily_total_l"] < r["daily_cap_l"]
    assert r["baseline_flagged"]
    assert r["baseline_detect_min_after_onset"] <= 60
    assert r["false_alarm_windows_other_days"] <= 3


def test_manifest_matches_artifacts_and_report_has_honesty_section(tiny):
    from ml.scripts.artifacts import sha256_file

    out, rep, _ = tiny
    files = json.loads((out / "manifest.json").read_text())["files"]
    for name in ("classifier.joblib", "isolation_forest.joblib", "feature_names.joblib"):
        assert files[name] == sha256_file(out / name)
    text = (rep / "detector_report.md").read_text()
    assert "Honesty section" in text and "episodes" in text and "95%" in text
    assert "Median time-to-detect" not in text and "excludes alert debounce" in text
    saved = json.loads((out / "detector_metadata.json").read_text())
    assert set(saved["bootstrap"]["ci95"]) >= {"macro_f1", "leak_burst_recall"}
    assert (
        saved["leave_flats_out"]["folds"] >= 2 and saved["single_feature"]["feature"] == "min_flow"
    )


def test_forecaster_per_horizon_std_manifest_and_model_type(tmp_path):
    meta = train_forecaster.main(
        ["--flats", "3", "--days", "24", "--out", str(tmp_path), "--report-dir", str(tmp_path)]
    )
    assert meta["model_type"] == "HistGradientBoostingRegressor"
    by_h = meta["residual_std_by_horizon"]
    assert len(by_h) == train_forecaster.MAX_HORIZON and all(v > 0 for v in by_h)
    assert 0 < meta["rollout"]["coverage_oos_served_steps_2_25"] <= 1
    assert "forecaster.joblib" in json.loads((tmp_path / "manifest.json").read_text())["files"]
    assert "Recursive multi-step" in (tmp_path / "forecaster_report.md").read_text()


def test_generate_data_output_is_readable_by_trainers(tmp_path):
    from ml.scripts.generate_data import load_dataset, save_dataset

    df = generate_dataset(2, 3, seed=9)
    save_dataset(df, tmp_path)
    back = load_dataset(tmp_path)
    assert dataset_hash(back) == dataset_hash(df.sort_values(["flat_id", "ts"], ignore_index=True))

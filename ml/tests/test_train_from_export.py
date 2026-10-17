"""train_from_export: synthetic export files, guard and promotion rules."""

import json

import joblib
import numpy as np
import pandas as pd
import pytest

from ml.features.engineering import fault_episodes
from ml.scripts import train_from_export as tfe
from ml.scripts.artifacts import sha256_file, update_manifest
from ml.scripts.generate_data import generate_dataset


class NeverFlag:
    """Stub incumbent IsolationForest: never flags anything (fp rate 0, recall 0)."""

    def predict(self, x):
        return np.ones(len(x), dtype=int)


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    d = tmp_path_factory.mktemp("export")
    df = generate_dataset(6, 30, seed=21)
    r = df.dropna(subset=["flow"])[["flat_id", "ts", "flow"]].rename(columns={"flow": "flow_lpm"})
    r.to_csv(d / "readings.csv", index=False)
    rows = []
    for i, e in enumerate(fault_episodes(df).itertuples()):
        typ = {"slow_leak": "leak"}.get(e.fault, e.fault)
        rows.append(
            (
                f"a{i}",
                e.flat_id,
                typ,
                e.start + pd.Timedelta(minutes=20),
                e.end,
                e.start,
                "confirmed" if i % 3 else "acknowledged",
            )
        )
    # operator-rejected alerts on normal behaviour
    rng = np.random.default_rng(0)
    for j in range(15):
        fid = f"flat-{rng.integers(1, 7):02d}"
        t = pd.Timestamp("2025-01-08", tz="UTC") + pd.Timedelta(
            minutes=int(rng.integers(0, 28 * 1440))
        )
        rows.append(
            (f"n{j}", fid, "overuse", t, t + pd.Timedelta(minutes=30), pd.NaT, "overridden")
        )
    pd.DataFrame(
        rows,
        columns=[
            "alert_id",
            "flat_id",
            "anomaly_type",
            "detected_at",
            "resolved_at",
            "fault_start",
            "outcome",
        ],
    ).to_csv(d / "feedback.csv", index=False)
    return d


def args(export, model_dir, *extra, feedback=True):
    a = [
        "--readings",
        str(export / "readings.csv"),
        "--model-dir",
        str(model_dir),
        "--min-positive-episodes",
        "5",
        "--min-holdout-positives",
        "2",
        "--min-holdout-windows",
        "100",
        "--min-clean-windows",
        "200",
    ]
    if feedback:
        a += ["--feedback", str(export / "feedback.csv")]
    return [*a, *extra]


def test_guard_logic():
    cand = {"iso_fp_rate": 0.02, "iso_recall": 0.8, "clf_macro_f1": 0.9, "x": None}
    inc = {"iso_fp_rate": 0.03, "iso_recall": 0.8, "clf_macro_f1": 0.85}
    assert tfe.guard(cand, inc, 0.0)["passed"] is True
    worse_fp = tfe.guard({**cand, "iso_fp_rate": 0.04}, inc, 0.0)
    assert worse_fp["passed"] is False and "iso_fp_rate" in worse_fp["reason"]
    assert tfe.guard({**cand, "iso_fp_rate": 0.031}, inc, 0.005)["passed"] is True  # tolerance
    assert tfe.guard({**cand, "iso_recall": 0.7}, inc, 0.0)["passed"] is False
    assert tfe.guard(cand, None, 0.0)["passed"] is None
    assert tfe.guard({"a": None}, {"a": 1.0}, 0.0)["passed"] is False  # nothing comparable


def test_candidate_written_live_untouched(export, tmp_path):
    d = tfe.main(args(export, tmp_path))
    cand = tmp_path / "candidates"
    out = next(cand.iterdir())
    for f in (
        "isolation_forest.joblib",
        "classifier.joblib",
        "feature_names.joblib",
        "detector_metadata.json",
        "baselines.json",
        "manifest.json",
        "export_report.md",
    ):
        assert (out / f).is_file(), f
    meta = json.loads((out / "detector_metadata.json").read_text())
    assert (
        meta["data_source"] == "real_export" and meta["classifier_source"] == "trained_from_export"
    )
    assert meta["feature_names"] and meta["sklearn_version"] and meta["data_hash"]
    manifest = json.loads((out / "manifest.json").read_text())["files"]
    assert manifest["classifier.joblib"] == sha256_file(out / "classifier.joblib")
    assert not (tmp_path / "isolation_forest.joblib").exists()  # live dir untouched
    assert d["guard"]["passed"] is None and not d["will_overwrite_live"]
    assert meta["labelled_episodes"]["positive"] > 5 and meta["labelled_episodes"]["negative"] > 0


def test_first_model_needs_explicit_allow(export, tmp_path):
    assert not tfe.main(args(export, tmp_path, "--promote"))["will_overwrite_live"]
    assert not (tmp_path / "isolation_forest.joblib").exists()
    d = tfe.main(args(export, tmp_path, "--promote", "--allow-first-model"))
    assert d["will_overwrite_live"]
    m = json.loads((tmp_path / "manifest.json").read_text())["files"]
    assert m["isolation_forest.joblib"] == sha256_file(tmp_path / "isolation_forest.joblib")
    assert joblib.load(tmp_path / "classifier.joblib").classes_.size >= 2


def test_same_data_candidate_is_not_worse_and_promotes(export, tmp_path):
    tfe.main(args(export, tmp_path, "--promote", "--allow-first-model"))
    d = tfe.main(args(export, tmp_path, "--promote"))  # incumbent = identical deterministic model
    assert d["guard"]["passed"] is True, d["guard"]
    assert d["will_overwrite_live"]


def test_guard_blocks_overwrite_when_worse(export, tmp_path):
    joblib.dump(NeverFlag(), tmp_path / "isolation_forest.joblib")
    update_manifest(tmp_path, ["isolation_forest.joblib"])
    before = sha256_file(tmp_path / "isolation_forest.joblib")
    d = tfe.main(args(export, tmp_path, "--promote", "--allow-first-model"))
    assert d["guard"]["passed"] is False and "iso_fp_rate" in d["guard"]["reason"]
    assert not d["will_overwrite_live"]
    assert sha256_file(tmp_path / "isolation_forest.joblib") == before
    assert any((tmp_path / "candidates").iterdir())  # candidate kept for shadow mode


def test_tampered_incumbent_is_refused(export, tmp_path):
    tfe.main(args(export, tmp_path, "--promote", "--allow-first-model"))
    with open(tmp_path / "isolation_forest.joblib", "ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(SystemExit) as e:
        tfe.main(args(export, tmp_path))
    assert e.value.code == 2


def test_unsupervised_only_without_feedback(export, tmp_path):
    d = tfe.main(args(export, tmp_path, feedback=False))
    meta = d["meta"]
    assert (
        meta["classifier_source"] == "none"
        and "not enough labelled" in meta["supervised_skipped_reason"]
    )
    out = next((tmp_path / "candidates").iterdir())
    assert (out / "isolation_forest.joblib").is_file() and not (out / "classifier.joblib").exists()


def test_dry_run_writes_nothing(export, tmp_path):
    tfe.main(args(export, tmp_path, "--dry-run"))
    assert not list(tmp_path.iterdir())


def test_bad_inputs(tmp_path):
    p = tmp_path / "r.csv"
    pd.DataFrame({"a": [1]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="readings missing columns"):
        tfe.load_inputs(p, None)

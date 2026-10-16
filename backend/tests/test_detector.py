import asyncio
import json
import sys
import types
from datetime import UTC, datetime, timedelta

import joblib
import numpy as np
import pytest
from sklearn.ensemble import IsolationForest, RandomForestClassifier

from app.models import FlowReading
from app.repositories import AnomalyRepository, FlatRepository
from app.services import baseline as bl
from app.services import detector as d
from tests.conftest import T0
from tests.helpers import DAY, NIGHT, series


def feats(**kw):
    base = dict.fromkeys(d.FEATURE_NAMES, 0.0)
    base.update(kw)
    return base


def test_feature_names_contract():
    assert len(d.FEATURE_NAMES) == 14
    assert d.FEATURE_NAMES[0] == "mean_flow" and d.FEATURE_NAMES[-1] == "zero_flow_pct"


def test_extract_features_basic_night_window():
    f = d.extract_features(series(NIGHT - timedelta(minutes=15), 15, 1.0), None, NIGHT)
    assert list(f) == d.FEATURE_NAMES
    assert f["mean_flow"] == pytest.approx(1.0) and f["std_flow"] == 0.0
    assert f["zero_flow_pct"] == 0.0 and f["night_min_flow"] == 1.0
    assert (f["is_night"], f["hour_of_day"], f["day_of_week"]) == (1.0, 2.0, 1.0)
    assert f["rolling_volume_1h"] == pytest.approx(16.0)  # 16 observed minutes x 1 L/min
    assert f["baseline_deviation"] == 0.0  # no baseline


def test_extract_features_zero_flow_and_rate_of_change():
    half = series(DAY - timedelta(minutes=15), 15, lambda i: 0.0 if i < 15 else 2.0)
    assert d.extract_features(half, None, DAY)["zero_flow_pct"] == pytest.approx(15 / 31, abs=0.02)
    ramp = series(DAY - timedelta(minutes=15), 15, lambda i: i * 0.1)  # +0.2 L/min per minute
    assert d.extract_features(ramp, None, DAY)["rate_of_change"] == pytest.approx(0.2, abs=0.01)


def test_extract_features_with_baseline():
    slots = [
        {**s, "median": 0.5, "mad": 0.1, "p25": 0.2, "p75": 0.8, "p95": 1.0, "sample_count": 9}
        for s in bl.empty_slots()
    ]
    f = d.extract_features(series(NIGHT - timedelta(minutes=15), 15, 2.5), slots, NIGHT)
    # robust scale = max(1.4826*mad, IQR/1.349, 0.3 floor) - identical to training
    assert f["baseline_deviation"] == pytest.approx((2.5 - 0.5) / (0.6 / 1.349))
    assert f["pct_above_p95"] == 1.0 and f["duration_above_base"] == 15.0


def test_window_is_exactly_15_minute_samples_and_night_is_00_to_05():
    f = d.extract_features(
        series(DAY - timedelta(minutes=30), 30, lambda i: float(i // 2)), None, DAY
    )
    assert (f["min_flow"], f["max_flow"]) == (15.5, 29.5)  # 15 one-minute bins, no 16th sample
    assert [d.is_night_hour(h) for h in (0, 4, 5, 23)] == [True, True, False, False]


def test_extract_features_ignores_future_and_handles_empty():
    f = d.extract_features([], None, NIGHT)
    assert f["mean_flow"] == 0.0 and f["hour_of_day"] == 2.0
    future = series(NIGHT + timedelta(minutes=1), 5, 9.0)
    assert d.extract_features(future, None, NIGHT)["mean_flow"] == 0.0


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        (feats(mean_flow=25, max_flow=30, min_flow=20, std_flow=3, baseline_deviation=9), "burst"),
        (feats(mean_flow=0.8, max_flow=0.9, min_flow=0.7, night_min_flow=0.7, is_night=1), "leak"),
        (
            feats(mean_flow=0.9, max_flow=1.0, min_flow=0.8, std_flow=0.05, night_min_flow=0.8),
            "running_toilet",
        ),
        (feats(mean_flow=0.06, max_flow=0.1, min_flow=0.0, zero_flow_pct=0.4), "dripping_tap"),
        (
            feats(
                mean_flow=8,
                max_flow=14,
                min_flow=0,
                std_flow=5,
                baseline_deviation=7,
                zero_flow_pct=0.3,
            ),
            "overuse",
        ),
    ],
)
def test_rules_classify_known_patterns(features, expected):
    det = d.classify_anomaly_rules(features, baseline_median=1.0)
    assert det is not None and det.anomaly_type == expected and det.source == "rules"
    assert 0 < det.confidence <= 1 and det.est_loss_lpm >= 0 and det.reasons


@pytest.mark.parametrize(
    "features",
    [
        feats(),
        feats(
            mean_flow=3, max_flow=9, min_flow=0, std_flow=3, zero_flow_pct=0.5, baseline_deviation=1
        ),
        feats(
            mean_flow=0.8,
            max_flow=0.9,
            min_flow=0.7,
            night_min_flow=0.0,
            zero_flow_pct=0.5,
            is_night=1,
        ),
    ],
)
def test_rules_return_none_for_normal_use(features):
    assert d.classify_anomaly_rules(features) is None


# ------------------------------------------------------------------ ML path with fakes
class FakeClf:
    classes_ = np.array(["burst", "leak", "normal", "overuse"])  # NOT assumed ordering

    def __init__(self, proba):
        self.proba = np.asarray([proba])
        self.calls = 0

    def predict_proba(self, x):
        self.calls += 1
        return self.proba


class FakeIso:
    def __init__(self, anomalous=True):
        self.anomalous = anomalous

    def predict(self, x):
        return np.asarray([-1 if self.anomalous else 1])

    def score_samples(self, x):
        return np.asarray([-0.7 if self.anomalous else -0.3])


def fake_store(proba=(0.05, 0.85, 0.05, 0.05), anomalous=True):
    s = d.ModelStore("/nonexistent")
    s.iso, s.clf, s._loaded = FakeIso(anomalous), FakeClf(proba), True
    return s


NIGHT_LEAK = feats(mean_flow=0.8, max_flow=0.9, min_flow=0.7, night_min_flow=0.7, is_night=1)


async def test_ml_uses_classifier_classes_order_not_alphabetical_assumption():
    store = fake_store((0.02, 0.9, 0.05, 0.03))  # index 1 == 'leak' in classes_
    det = await d.classify_anomaly_ml(NIGHT_LEAK, store)
    assert det.source == "ml" and det.anomaly_type == "leak" and det.confidence == 0.9
    assert det.class_probabilities["leak"] == 0.9


async def test_ml_without_shap_is_honest_about_explanation():
    det = await d.classify_anomaly_ml(NIGHT_LEAK, fake_store())
    assert det.shap_values is None
    exp = d.build_explanation(det, NIGHT_LEAK)
    assert exp["generated_by"] == "rule_heuristic" and exp["classifier"] == "ml"


async def test_ml_with_shap_stores_top3(monkeypatch):
    class FakeExplainer:
        def __init__(self, model): ...

        def shap_values(self, x):  # list per class, like older shap versions
            out = [np.zeros((1, 14)) for _ in range(4)]
            out[1][0, [4, 5, 0, 2]] = [0.5, -0.9, 0.1, 0.05]
            return out

    monkeypatch.setitem(sys.modules, "shap", types.SimpleNamespace(TreeExplainer=FakeExplainer))
    det = await d.classify_anomaly_ml(NIGHT_LEAK, fake_store())
    assert [v["feature"] for v in det.shap_values] == [
        "night_min_flow",
        "baseline_deviation",
        "mean_flow",
    ]
    assert det.shap_values[0]["shap"] == -0.9
    assert d.build_explanation(det, NIGHT_LEAK)["generated_by"] == "shap_tree_explainer"


async def test_ml_shap_3d_array_shape_supported(monkeypatch):
    class Exp3D:
        def __init__(self, model): ...

        def shap_values(self, x):
            arr = np.zeros((1, 14, 4))
            arr[0, 3, 1] = 2.0
            return arr

    monkeypatch.setitem(sys.modules, "shap", types.SimpleNamespace(TreeExplainer=Exp3D))
    det = await d.classify_anomaly_ml(NIGHT_LEAK, fake_store())
    assert det.shap_values[0]["feature"] == "std_flow"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
async def test_nan_or_inf_feature_vector_falls_back_to_rules(bad):
    store = fake_store()
    det = await d.classify_anomaly_ml({**NIGHT_LEAK, "std_flow": bad}, store)
    assert store.clf.calls == 0  # model never saw the bad vector
    assert det is None or det.source == "rules"


async def test_missing_feature_falls_back_to_rules():
    store = fake_store()
    partial = {k: v for k, v in NIGHT_LEAK.items() if k != "zero_flow_pct"}
    det = await d.classify_anomaly_ml(partial, store)
    assert store.clf.calls == 0 and det.source == "rules"


async def test_no_models_falls_back_to_rules(tmp_path):
    det = await d.classify_anomaly_ml(NIGHT_LEAK, d.ModelStore(tmp_path))
    assert det.source == "rules" and det.anomaly_type == "leak"
    assert d.build_explanation(det, NIGHT_LEAK)["generated_by"] == "rule_heuristic"


async def test_inference_error_falls_back_to_rules():
    store = fake_store()
    store.clf.predict_proba = lambda x: (_ for _ in ()).throw(RuntimeError("corrupt model"))
    assert (await d.classify_anomaly_ml(NIGHT_LEAK, store)).source == "rules"


async def test_ml_normal_prediction_behaviour():
    normal = (0.01, 0.01, 0.97, 0.01)
    assert await d.classify_anomaly_ml(NIGHT_LEAK, fake_store(normal, anomalous=False)) is None
    det = await d.classify_anomaly_ml(NIGHT_LEAK, fake_store(normal, anomalous=True))
    assert det.anomaly_type == "unknown" and det.confidence < 0.7


async def test_ml_leak_refined_to_subtype_by_rules():
    f = feats(mean_flow=0.9, max_flow=1.0, min_flow=0.8, std_flow=0.05, night_min_flow=0.8)
    det = await d.classify_anomaly_ml(f, fake_store((0.02, 0.9, 0.05, 0.03)))
    assert det.source == "ml" and det.anomaly_type == "running_toilet"


def train_models(path, seed=0, trained_at="2026-01-02T03:04:05+00:00"):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(200, 14))
    y = np.array(["normal", "leak", "burst", "overuse"] * 50)
    joblib.dump(
        IsolationForest(n_estimators=10, random_state=seed).fit(X), path / "isolation_forest.joblib"
    )
    joblib.dump(
        RandomForestClassifier(n_estimators=10, random_state=seed).fit(X, y),
        path / "classifier.joblib",
    )
    joblib.dump(list(d.FEATURE_NAMES), path / "feature_names.joblib")
    (path / "detector_metadata.json").write_text(
        json.dumps({"trained_at": trained_at, "data_hash": "abcdef0123456789"})
    )
    write_manifest(path)


def write_manifest(path, names=("isolation_forest", "classifier", "feature_names", "forecaster")):
    files = {
        f"{n}.joblib": d.sha256_file(path / f"{n}.joblib")
        for n in names
        if (path / f"{n}.joblib").is_file()
    }
    (path / "manifest.json").write_text(json.dumps({"files": files}))


async def test_store_loads_real_sklearn_models_and_versions_from_metadata(tmp_path):
    train_models(tmp_path)
    store = d.ModelStore(tmp_path)
    await store.ensure_loaded()
    assert store.ml_ready and store.feature_names == d.FEATURE_NAMES
    trained, data, digest = store.version.rsplit("+", 2)
    assert trained == "2026-01-02T03:04:05+00:00" and data == "abcdef01" and len(digest) == 8
    assert store.model_status["detector"] == "ok" and store.model_status["classifier"] == "ok"
    det = await d.classify_anomaly_ml(NIGHT_LEAK, store)
    assert det.source == "ml" and set(det.class_probabilities) == {
        "burst",
        "leak",
        "normal",
        "overuse",
    }
    assert det.anomaly_type in {
        "burst",
        "leak",
        "overuse",
        "running_toilet",
        "dripping_tap",
        "unknown",
    }


async def test_store_reload_picks_up_new_models(tmp_path):
    train_models(tmp_path, seed=0)
    store = d.ModelStore(tmp_path)
    await store.ensure_loaded()
    v1 = store.version
    train_models(tmp_path, seed=1, trained_at="2026-02-02T00:00:00+00:00")
    await store.reload()
    assert store.version != v1 and store.version.startswith("2026-02-02")


async def test_store_loads_once_under_concurrency(tmp_path, monkeypatch):
    store = d.ModelStore(tmp_path)
    calls = []
    real = store.load_sync
    monkeypatch.setattr(store, "load_sync", lambda: (calls.append(1), real())[1])
    await asyncio.gather(*(store.ensure_loaded() for _ in range(10)))
    assert len(calls) == 1 and store.version == d.RULES_VERSION and not store.ml_ready


async def test_store_survives_corrupt_model_file(tmp_path):
    (tmp_path / "classifier.joblib").write_bytes(b"not a pickle")
    store = d.ModelStore(tmp_path)
    await store.ensure_loaded()
    assert not store.ml_ready


# ------------------------------------------------------------------ severity
def test_severity_formula_exact_values():
    assert d.compute_severity(1, 10, 360, True, "burst") == 1.0
    assert d.compute_severity(0, 0, 0, False, "overuse") == pytest.approx(0.105)
    assert d.compute_severity(0.8, 5, 180, True, "leak") == pytest.approx(
        0.24 + 0.125 + 0.1 + 0.15 + 0.07
    )


@pytest.mark.parametrize(
    "args", [(-5, -1, -10, False, "x"), (9, 1e6, 1e9, True, "burst"), (0.5, 0, 0, True, "unknown")]
)
def test_severity_is_bounded(args):
    assert 0.0 <= d.compute_severity(*args) <= 1.0


def test_severity_monotonic():
    base = dict(confidence=0.5, est_loss_lpm=2.0, duration_min=60, night=False, anomaly_type="leak")
    ref = d.compute_severity(**base)
    assert d.compute_severity(**{**base, "confidence": 0.9}) > ref
    assert d.compute_severity(**{**base, "est_loss_lpm": 8.0}) > ref
    assert d.compute_severity(**{**base, "duration_min": 300}) > ref
    assert d.compute_severity(**{**base, "night": True}) > ref
    assert d.compute_severity(**{**base, "anomaly_type": "burst"}) > ref
    assert d.compute_severity(**{**base, "anomaly_type": "overuse"}) < ref


# ------------------------------------------------------------------ orchestration
async def test_run_detection_creates_updates_and_resolves(db, world, tmp_path):
    store = d.ModelStore(tmp_path)
    fid = world.flats[0].id

    async def add(start, minutes, flow, tag):
        async with db.session() as s:
            for i in range(minutes * 2 + 1):
                s.add(
                    FlowReading(
                        flat_id=fid,
                        timestamp=start + timedelta(seconds=30 * i),
                        flow_rate=flow,
                        volume=0.1,
                        message_id=f"{tag}{i:05x}"[:8],
                    )
                )

    await add(T0 - timedelta(minutes=10), 10, 30.0, "a")
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        out = await d.run_detection(s, store, flat, T0)
        assert out.created and out.anomaly.anomaly_type == "burst"
        assert (
            0.3 < out.anomaly.severity <= 1
            and out.anomaly.explanation["generated_by"] == "rule_heuristic"
        )
        first_id = out.anomaly.id
    async with db.session() as s:  # still bursting: same anomaly row updated, not duplicated
        flat = await FlatRepository(s).get(fid)
        out = await d.run_detection(s, store, flat, T0 + timedelta(minutes=1))
        assert not out.created and out.anomaly.id == first_id
    await add(T0 + timedelta(minutes=30), 16, 0.0, "b")
    async with db.session() as s:
        flat = await FlatRepository(s).get(fid)
        out = await d.run_detection(s, store, flat, T0 + timedelta(minutes=46))
        assert out.anomaly is None and [a.id for a in out.resolved] == [first_id]
        assert (await AnomalyRepository(s).get(first_id)).resolved_at is not None


async def test_run_detection_needs_enough_readings(db, world, tmp_path):
    async with db.session() as s:
        flat = await FlatRepository(s).get(world.flats[0].id)
        assert await d.run_detection(s, d.ModelStore(tmp_path), flat, datetime.now(UTC)) is None

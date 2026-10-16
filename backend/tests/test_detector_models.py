"""ModelStore hardening: manifest, feature-name validation, status, off-loop inference."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import joblib
import numpy as np
import pytest

from app.services import detector as d
from tests.test_detector import NIGHT_LEAK, train_models, write_manifest


async def _store(path, **kw) -> d.ModelStore:
    s = d.ModelStore(path, **kw)
    await s.ensure_loaded()
    return s


async def test_manifest_verified_models_load(tmp_path):
    train_models(tmp_path)
    s = await _store(tmp_path, manifest_required=True)
    assert s.ml_ready and s.model_status["classifier"] == "ok"


async def test_required_manifest_rejects_tampered_model(tmp_path):
    train_models(tmp_path)
    p = tmp_path / "classifier.joblib"
    p.write_bytes(p.read_bytes() + b"\0")  # still unpicklable-safe, but hash differs
    s = await _store(tmp_path, manifest_required=True)
    assert not s.ml_ready and s.version == d.RULES_VERSION
    assert s.model_status["classifier"] == "rejected:hash_mismatch"
    assert s.model_status["detector"].startswith("unavailable")
    det = await d.classify_anomaly_ml(NIGHT_LEAK, s)
    assert det.source == "rules"


async def test_required_manifest_rejects_missing_manifest(tmp_path):
    train_models(tmp_path)
    (tmp_path / "manifest.json").unlink()
    assert not (await _store(tmp_path, manifest_required=True)).ml_ready
    s = await _store(tmp_path, manifest_required=False)  # dev: loads, but says unverified
    assert s.ml_ready and s.model_status["classifier"].startswith("ok_unverified")


def test_manifest_required_defaults_to_production(monkeypatch):
    monkeypatch.delenv("MODEL_MANIFEST_REQUIRED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert d.manifest_required_default() is True
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert d.manifest_required_default() is False
    monkeypatch.setenv("MODEL_MANIFEST_REQUIRED", "true")
    assert d.manifest_required_default() is True


async def test_feature_name_mismatch_is_rejected(tmp_path):
    train_models(tmp_path)
    joblib.dump([*d.FEATURE_NAMES[:-1], "other"], tmp_path / "feature_names.joblib")
    write_manifest(tmp_path)
    s = await _store(tmp_path, manifest_required=True)
    assert not s.ml_ready and s.model_status["detector"].startswith("rejected")


async def test_sklearn_major_mismatch_warns_but_loads(tmp_path):
    train_models(tmp_path)
    (tmp_path / "detector_metadata.json").write_text(
        json.dumps({"trained_at": "t", "sklearn_version": "0.24.2"})
    )
    s = await _store(tmp_path, manifest_required=False)
    assert s.ml_ready and any("sklearn 0.24.2" in w for w in s.model_status["warnings"])


async def test_missing_forecaster_is_reported_not_silent(tmp_path):
    s = await _store(tmp_path)
    assert s.model_status["forecaster"].startswith("baseline_fallback")
    assert s.forecaster_version == "baseline-median"


async def test_unloadable_forecaster_reports_load_failure(tmp_path):
    (tmp_path / "forecaster.joblib").write_bytes(b"garbage")
    s = await _store(tmp_path, manifest_required=False)
    assert s.model_status["forecaster"] == "baseline_fallback:load_failed:UnpicklingError" or (
        s.model_status["forecaster"].startswith("baseline_fallback:load_failed")
    )


async def test_inference_runs_off_the_event_loop(tmp_path):
    train_models(tmp_path)
    s = await _store(tmp_path, manifest_required=True)
    seen: list[int] = []
    real = s.clf.predict_proba

    def spy(x):
        seen.append(threading.get_ident())
        return real(x)

    s.clf.predict_proba = spy  # type: ignore[method-assign]
    det = await d.classify_anomaly_ml(NIGHT_LEAK, s)
    assert det is not None and seen and seen[0] != threading.get_ident()
    assert np.isclose(sum(det.class_probabilities.values()), 1.0, atol=1e-3)


@pytest.mark.parametrize("bad", ["classifier_metadata.json", "isolation_forest_metadata.json"])
async def test_legacy_metadata_names_are_not_required(tmp_path, bad):
    train_models(tmp_path)
    assert not (tmp_path / bad).exists()
    assert (await _store(tmp_path, manifest_required=True)).version != d.RULES_VERSION


def test_raw_zero_flow_pct_sees_subminute_stops():
    from tests.helpers import DAY, series

    r = series(DAY - timedelta(minutes=5), 5, lambda i: 0.0 if i % 2 else 1.0)
    assert d.extract_features(r, None, DAY)["zero_flow_pct"] == 0.0  # 1-minute bins hide it
    assert d.raw_zero_flow_pct(r, DAY) == pytest.approx(0.5, abs=0.05)

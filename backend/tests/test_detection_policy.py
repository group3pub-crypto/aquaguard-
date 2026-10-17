"""DETECTION_POLICY = rules | ml | hybrid (rules gate + ML class/confidence/veto)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services import detector as d
from tests.conftest import make_settings
from tests.test_detector import NIGHT_LEAK, fake_store, feats, train_models

NORMAL = feats(mean_flow=0.0, max_flow=0.0, min_flow=0.0)  # rules: nothing happening
# classes_ order of fake_store: burst, leak, normal, overuse


def test_default_policy_is_hybrid():
    assert Settings(_env_file=None, environment="test").detection_policy == "hybrid"
    with pytest.raises(ValueError):
        Settings(_env_file=None, environment="test", detection_policy="bogus")


async def test_rules_policy_never_touches_the_models():
    store = fake_store((1.0, 0.0, 0.0, 0.0))
    det = await d.classify_anomaly_ml(NIGHT_LEAK, store, policy="rules")
    assert det.source == "rules" and det.anomaly_type != "burst"


async def test_hybrid_ml_supplies_class_and_confidence_when_rules_fire():
    store = fake_store((0.8, 0.1, 0.05, 0.05))
    det = await d.classify_anomaly_ml(NIGHT_LEAK, store, policy="hybrid")
    assert det.source == "ml" and det.anomaly_type == "burst" and det.confidence == 0.8


async def test_hybrid_rules_gate_blocks_ml_only_alerts():
    store = fake_store((0.03, 0.9, 0.05, 0.02))  # ML would call it a leak
    assert d.classify_anomaly_rules(NORMAL) is None
    assert await d.classify_anomaly_ml(NORMAL, store, policy="hybrid") is None
    # ... whereas policy ml trusts the model
    assert (await d.classify_anomaly_ml(NORMAL, store, policy="ml")).anomaly_type == "leak"


async def test_hybrid_ml_veto_when_p_normal_dominates():
    store = fake_store((0.05, 0.2, 0.7, 0.05))
    assert d.classify_anomaly_rules(NIGHT_LEAK) is not None
    assert await d.classify_anomaly_ml(NIGHT_LEAK, store, policy="hybrid") is None


@pytest.mark.parametrize("policy", ["ml", "hybrid"])
async def test_missing_models_fall_back_to_rules(tmp_path, policy):
    store = d.ModelStore(tmp_path / "nothing", manifest_required=True)
    det = await d.classify_anomaly_ml(NIGHT_LEAK, store, policy=policy)
    assert det.source == "rules"
    assert d.effective_policy(policy, store) == (
        "rules",
        "ml_unavailable:" + store.model_status["detector"],
    )


async def test_manifest_failure_falls_back_to_rules(tmp_path):
    train_models(tmp_path)
    p = tmp_path / "classifier.joblib"
    p.write_bytes(p.read_bytes() + b"\0")
    store = d.ModelStore(tmp_path, manifest_required=True)
    det = await d.classify_anomaly_ml(NIGHT_LEAK, store, policy="hybrid")
    assert det.source == "rules"
    assert d.effective_policy("hybrid", store)[0] == "rules"


async def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        await d.classify_anomaly_ml(NIGHT_LEAK, fake_store(), policy="x")


async def test_health_details_reports_effective_policy(client, world, tmp_path, app):
    r = await client.get("/health/details", headers=world.admin)
    det = r.json()["detection"]
    assert det["policy"] == "hybrid" and det["effective"] == "rules"
    assert det["reason"].startswith("ml_unavailable")
    app.state.rt.settings.detection_policy = "rules"
    det = (await client.get("/health/details", headers=world.admin)).json()["detection"]
    assert det == {"policy": "rules", "effective": "rules", "reason": None}


def test_settings_factory_supports_policy(tmp_path):
    assert make_settings(tmp_path, detection_policy="ml").detection_policy == "ml"

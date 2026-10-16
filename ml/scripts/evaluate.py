"""Evaluate saved detector models on a FRESH synthetic dataset (new seed => unseen households).

Per-flat baselines are rebuilt from each new flat's first 70% of days (as a real onboarding would),
then the saved classifier is scored on the remainder. Optionally prints SHAP feature importance.

Usage: python -m ml.scripts.evaluate --models ml/models --seed 1042 [--shap]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np

from ml.features.engineering import FEATURE_NAMES, fault_episodes
from ml.scripts.generate_data import generate_dataset
from ml.scripts.train_detector import ML_DIR, compute_metrics, prepare


def shap_importance(clf, X, n: int = 300) -> dict[str, float]:
    import shap

    sv = shap.TreeExplainer(clf).shap_values(X.sample(min(n, len(X)), random_state=0))
    arr = np.stack(sv) if isinstance(sv, list) else np.asarray(sv)
    feat_axis = [i for i, s in enumerate(arr.shape) if s == len(FEATURE_NAMES)][-1]
    other = tuple(i for i in range(arr.ndim) if i != feat_axis)
    imp = np.abs(arr).mean(axis=other)
    return dict(sorted(zip(FEATURE_NAMES, imp.tolist(), strict=True), key=lambda kv: -kv[1]))


def evaluate(models: Path, seed: int, flats: int, days: int, stride: int = 5) -> dict:
    meta = json.loads((models / "detector_metadata.json").read_text())
    assert joblib.load(models / "feature_names.joblib") == FEATURE_NAMES == meta["feature_names"]
    clf = joblib.load(models / "classifier.joblib")
    df = generate_dataset(flats, days, seed)
    _, test, _, split = prepare(df, days, stride)
    pred = clf.predict(test[FEATURE_NAMES])
    eps = fault_episodes(df)
    metrics = compute_metrics(test, pred, eps[eps["start"] > split])
    return {"metrics": metrics, "test": test, "clf": clf}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", type=Path, default=ML_DIR / "models")
    ap.add_argument("--seed", type=int, default=1042)
    ap.add_argument("--flats", type=int, default=8)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--shap", action="store_true")
    a = ap.parse_args(argv)
    r = evaluate(a.models, a.seed, a.flats, a.days)
    m = r["metrics"]
    print(f"[evaluate] fresh synthetic households (seed {a.seed}); still synthetic, not field data")
    print(
        f"  macro-F1={m['macro_f1']:.3f} leak+burst recall={m['leak_burst_recall']} "
        f"FPR(normal)={m['false_positive_rate_normal']}"
    )
    print(f"  per-class F1: { {c: round(v['f1'], 3) for c, v in m['per_class'].items()} }")
    print(
        "  onset->first non-normal window (offline, any class, stride-quantized): "
        f"{m['onset_to_first_flag_min']}"
    )
    if a.shap:
        try:
            top = shap_importance(r["clf"], r["test"][FEATURE_NAMES])
            print("  SHAP mean|value|:", {k: round(v, 4) for k, v in list(top.items())[:6]})
        except ImportError:
            print("  shap not installed; skipping")


if __name__ == "__main__":
    main()

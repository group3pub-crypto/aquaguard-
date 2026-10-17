"""Benchmark smoke test (tiny config) and statistics helpers."""

import json

import numpy as np
import pytest

from ml.scripts import bench_stats as bs
from ml.scripts import benchmark


def test_rules_loader_returns_production_classifier():
    fn, prov = benchmark.load_production_rules()
    assert len(prov["sha256"]) == 64 and prov["mode"]
    burst = dict.fromkeys(
        [
            "mean_flow",
            "max_flow",
            "min_flow",
            "std_flow",
            "baseline_deviation",
            "night_min_flow",
            "zero_flow_pct",
            "is_night",
        ],
        0.0,
    )
    burst.update(mean_flow=20.0, max_flow=25.0, min_flow=15.0, baseline_deviation=8.0)
    assert fn(burst, 0.0).anomaly_type == "burst"
    assert fn(dict.fromkeys(burst, 0.0), 0.0) is None


def test_stats_helpers():
    # perfect classifier
    y = np.array([0, 0, 1, 2, 3])
    assert bs.macro_f1(bs.class_counts(y, y)) == pytest.approx(1.0)
    # UNKNOWN predictions are never correct
    p = np.array([0, 0, bs.UNKNOWN, 2, 3])
    assert bs.macro_f1(bs.class_counts(y, p)) < 1.0
    lo, hi = bs.wilson(9, 10)
    assert 0.5 < lo < 0.9 < hi <= 1.0
    adj = bs.holm([0.01, 0.04, 0.03])
    assert adj[0] == pytest.approx(0.03) and all(
        a >= p for a, p in zip(adj, [0.01, 0.04, 0.03], strict=True)
    )
    assert bs.mcnemar_exact(np.ones(20, bool), np.zeros(20, bool))["p"] < 1e-4
    assert bs.mcnemar_exact(np.ones(5, bool), np.ones(5, bool))["p"] == 1.0
    assert bs.verdict(0.2, 0.5, True) == "not demonstrated"
    assert bs.verdict(0.01, 0.5, True) == "better"
    assert bs.verdict(0.01, 0.5, False) == "worse"
    assert bs.boot_pvalue(np.zeros(100)) == 1.0


def test_benchmark_smoke(tmp_path):
    ctx = benchmark.run(
        [
            "--flats",
            "4",
            "--days",
            "20",
            "--boot",
            "60",
            "--quick",
            "--curve-flats",
            "2",
            "--skip-shift",
            "--out-dir",
            str(tmp_path),
        ]
    )
    md = (tmp_path / "benchmark.md").read_text()
    for needle in (
        "SYNTHETIC",
        "What this does NOT show",
        "Recommendation",
        "SMOKE RUN",
        "production rules",
        "Minimum detectable leak",
    ):
        assert needle in md
    data = json.loads((tmp_path / "benchmark_results.json").read_text())
    summ = data["results"]["default"]["summary"]
    assert set(summ) == set(benchmark.POLICIES)
    m = summ["rf"]["metrics"]["macro_f1"]
    assert 0 <= m["ci"][0] <= m["value"] + 1e-9 and m["ci"][1] <= 1
    assert summ["iso"]["metrics"]["macro_f1"] is None
    assert data["curves"]  # leak-response curve ran
    # deterministic: same args -> identical numbers
    again = benchmark.run(
        [
            "--flats",
            "4",
            "--days",
            "20",
            "--boot",
            "60",
            "--quick",
            "--curve-flats",
            "2",
            "--skip-shift",
            "--out-dir",
            str(tmp_path / "b"),
        ]
    )

    def j(c):
        return json.dumps(benchmark._jsonable(c["results"]["default"]["summary"]), sort_keys=True)

    assert j(ctx) == j(again)  # NaN-safe comparison


def test_min_detectable():
    rows = [
        {"policy": "p", "timing": "night", "magnitude": m, "detect": d}
        for m, d in [(0.0, 0.0), (0.1, 0.0), (0.5, 0.6), (1.0, 1.0), (2.0, 1.0)]
    ]
    md = benchmark.min_detectable(rows, "p", "night")
    assert md["m90"] == 1.0 and 0.1 < md["m50"] < 0.5 and md["control_rate"] == 0.0

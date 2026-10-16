"""Train the leak/burst/overuse detector (IsolationForest + RandomForest) on synthetic data.

Chronological split, baselines from the train period only, IsolationForest fitted on
train-normal windows only, hyper-parameters chosen with TimeSeriesSplit(3) on train only.
The classifier consumes exactly the 14 shared FEATURE_NAMES (the isolation score is NOT an input).
Features come from ``libs/aquaguard_features`` - the same code the backend serves with.

Data: generated in memory from --seed/--flats/--days, or read from ``--data-dir`` (the output of
``python -m ml.scripts.generate_data --out DIR``; flats/days are then taken from the files).

Usage (from the repo root):
    python -m ml.scripts.train_detector --seed 42 --flats 12 --days 30 --out ml/models
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.tree import DecisionTreeClassifier

from ml.features.engineering import (
    CLASSES,
    FEATURE_NAMES,
    WINDOW,
    build_baselines,
    collapse_label,
    fault_episodes,
    featurize_dataset,
)
from ml.scripts.artifacts import update_manifest
from ml.scripts.generate_data import (
    _DURATION,
    _MAGNITUDE,
    START,
    dataset_hash,
    generate_dataset,
    load_dataset,
)

ML_DIR = Path(__file__).parents[1]
FULL_GRID = {
    "n_estimators": [100, 200],
    "max_depth": [12, None],
    "min_samples_leaf": [1, 3],
}
QUICK_GRID = {"n_estimators": [60], "max_depth": [12], "min_samples_leaf": [2]}
TRAIN_FRAC = 0.7
N_BOOT = 1000
LFO_FOLDS = 4
_IDX = {c: i for i, c in enumerate(CLASSES)}


def split_timestamp(days: int) -> pd.Timestamp:
    return START + pd.Timedelta(days=int(days * TRAIN_FRAC))


def prepare(df: pd.DataFrame, days: int, stride: int):
    """Returns (train_windows, test_windows, baselines, split_ts). Baselines use train only."""
    split = split_timestamp(days)
    baselines = build_baselines(df, split)
    w = featurize_dataset(df, baselines, stride)
    return (
        w[w["ts"] < split].reset_index(drop=True),
        w[w["ts"] >= split].reset_index(drop=True),
        baselines,
        split,
    )


# ----------------------------------------------------------------------------- metrics
def confusion(y: np.ndarray, pred: np.ndarray) -> np.ndarray:
    cm = np.zeros((len(CLASSES), len(CLASSES)), dtype=np.int64)
    np.add.at(cm, ([_IDX[v] for v in y], [_IDX[v] for v in pred]), 1)
    return cm


def metrics_from_confusion(cm: np.ndarray) -> dict:
    """Per-class P/R/F1, macro-F1 (over classes with support), leak+burst recall, normal FPR."""
    tp = np.diag(cm).astype(float)
    sup, pred_n = cm.sum(axis=1).astype(float), cm.sum(axis=0).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        prec = np.where(pred_n > 0, tp / pred_n, 0.0)
        rec = np.where(sup > 0, tp / sup, 0.0)
        f1 = np.where(prec + rec > 0, 2 * prec * rec / (prec + rec), 0.0)
    present = sup > 0
    lb = [_IDX["leak"], _IDX["burst"]]
    lb_n = sup[lb].sum()
    n = _IDX["normal"]
    return {
        "per_class": {
            c: {
                "precision": float(prec[i]),
                "recall": float(rec[i]),
                "f1": float(f1[i]),
                "support": int(sup[i]),
            }
            for c, i in _IDX.items()
        },
        "macro_f1": float(f1[present].mean()),
        "leak_burst_recall": float((lb_n - cm[lb, n].sum()) / lb_n) if lb_n else None,
        "false_positive_rate_normal": float(1 - rec[n]) if sup[n] else None,
    }


def episode_outcomes(
    episodes: pd.DataFrame, windows: pd.DataFrame, pred: np.ndarray
) -> pd.DataFrame:
    """Per fault episode: collapsed class, first non-normal window delay (min, NaN if never)."""
    w = windows.assign(pred=pred)
    rows = []
    for ep in episodes.itertuples():
        g = w[(w["flat_id"] == ep.flat_id) & (w["ts"] >= ep.start) & (w["ts"] <= ep.end)]
        hit = g[g["pred"] != "normal"]
        delay = (hit["ts"].iloc[0] - ep.start).total_seconds() / 60.0 if len(hit) else np.nan
        rows.append((ep.flat_id, collapse_label(ep.fault), len(g), delay))
    return pd.DataFrame(rows, columns=["flat_id", "label", "n_windows", "delay_min"])


def compute_metrics(test: pd.DataFrame, pred: np.ndarray, episodes: pd.DataFrame) -> dict:
    y = test["label"].to_numpy()
    cm = confusion(y, pred)
    m = metrics_from_confusion(cm)
    out = episode_outcomes(episodes, test, pred)
    delays = out["delay_min"].dropna()
    eps_by_class = {c: int((out["label"] == c).sum()) for c in CLASSES if c != "normal"}
    m["confusion_matrix"] = {"labels": CLASSES, "matrix": cm.tolist()}
    m["episodes_by_class"] = eps_by_class
    m["episodes_detected_by_class"] = {
        c: int(((out["label"] == c) & out["delay_min"].notna()).sum()) for c in eps_by_class
    }
    m["onset_to_first_flag_min"] = {
        "median": float(delays.median()) if len(delays) else None,
        "episodes_detected": int(len(delays)),
        "episodes_total": int(len(out)),
    }
    return m


def bootstrap_ci(test: pd.DataFrame, pred: np.ndarray, episodes: pd.DataFrame, seed: int) -> dict:
    """95% percentile CIs. Windows are resampled in CLUSTERS of whole flats (windows overlap and
    are autocorrelated, so window-level resampling would be far too optimistic); the episode
    detection rate is resampled over episodes."""
    rng = np.random.default_rng(seed)
    y = test["label"].to_numpy()
    flats = test["flat_id"].to_numpy()
    ids = sorted(set(flats))
    per_flat = np.stack([confusion(y[flats == f], pred[flats == f]) for f in ids])
    stats = {"macro_f1": [], "leak_burst_recall": [], "overuse_f1": [], "overuse_precision": []}
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(ids), len(ids))
        m = metrics_from_confusion(per_flat[pick].sum(axis=0))
        stats["macro_f1"].append(m["macro_f1"])
        if m["leak_burst_recall"] is not None:
            stats["leak_burst_recall"].append(m["leak_burst_recall"])
        stats["overuse_f1"].append(m["per_class"]["overuse"]["f1"])
        stats["overuse_precision"].append(m["per_class"]["overuse"]["precision"])
    ci = {
        k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in stats.items()
    }
    out = episode_outcomes(episodes, test, pred)
    lb = out[out["label"].isin(["leak", "burst"])]
    det = lb["delay_min"].notna().to_numpy(dtype=float)
    if len(det):
        boots = [det[rng.integers(0, len(det), len(det))].mean() for _ in range(N_BOOT)]
        ci["leak_burst_episode_detection"] = [
            float(np.percentile(boots, 2.5)),
            float(np.percentile(boots, 97.5)),
        ]
    return {
        "method": f"{N_BOOT} bootstrap resamples over {len(ids)} test flats (windows) / "
        f"{len(det)} leak+burst episodes (episode detection)",
        "n_flats": len(ids),
        "ci95": ci,
        "leak_burst_episode_detection": float(det.mean()) if len(det) else None,
        "leak_burst_episodes": int(len(det)),
    }


def leave_flats_out(train: pd.DataFrame, test: pd.DataFrame, params: dict, seed: int) -> dict:
    """K-fold over FLATS: the classifier never sees the held-out flats' windows (their baseline
    is still their own, as at onboarding); scored on the held-out flats' test-period windows."""
    flats = np.array(sorted(set(train["flat_id"])))
    folds = np.array_split(flats, min(LFO_FOLDS, len(flats)))
    total = np.zeros((len(CLASSES),) * 2, dtype=np.int64)
    per_fold = []
    for held in folds:
        tr = train[~train["flat_id"].isin(held)]
        te = test[test["flat_id"].isin(held)]
        clf = RandomForestClassifier(
            class_weight="balanced", random_state=seed, n_jobs=-1, **params
        ).fit(tr[FEATURE_NAMES], tr["label"])
        cm = confusion(te["label"].to_numpy(), clf.predict(te[FEATURE_NAMES]))
        total += cm
        per_fold.append(metrics_from_confusion(cm)["macro_f1"])
    m = metrics_from_confusion(total)
    return {
        "folds": len(folds),
        "flats_per_fold": [len(f) for f in folds],
        "macro_f1": m["macro_f1"],
        "macro_f1_per_fold": per_fold,
        "leak_burst_recall": m["leak_burst_recall"],
        "per_class_f1": {c: v["f1"] for c, v in m["per_class"].items()},
    }


def single_feature_baseline(train: pd.DataFrame, test: pd.DataFrame, seed: int) -> dict:
    """Depth-3 tree on min_flow ONLY: how much of the task the generator makes trivial."""
    t = DecisionTreeClassifier(max_depth=3, class_weight="balanced", random_state=seed)
    t.fit(train[["min_flow"]], train["label"])
    m = metrics_from_confusion(confusion(test["label"].to_numpy(), t.predict(test[["min_flow"]])))
    return {
        "feature": "min_flow",
        "macro_f1": m["macro_f1"],
        "leak_burst_recall": m["leak_burst_recall"],
        "per_class_f1": {c: v["f1"] for c, v in m["per_class"].items()},
    }


def isolation_stats(iso: IsolationForest, test: pd.DataFrame) -> dict:
    flagged = iso.predict(test[FEATURE_NAMES]) == -1
    y = test["label"].to_numpy()
    return {
        "flag_rate_on_normal": float(flagged[y == "normal"].mean()),
        "flag_rate_on_faults": float(flagged[y != "normal"].mean())
        if (y != "normal").any()
        else None,
    }


def honesty_section(meta: dict) -> str:
    """Shared by the report and ``evaluate``; every number is read from ``meta``."""
    m, b, lfo, sf = (
        meta["metrics"],
        meta["bootstrap"],
        meta["leave_flats_out"],
        meta["single_feature"],
    )
    ci = b["ci95"]

    def rng(k: str) -> str:
        return f"[{ci[k][0]:.3f}, {ci[k][1]:.3f}]" if k in ci else "n/a"

    eps = m["episodes_by_class"]
    det = m["episodes_detected_by_class"]
    mags = ", ".join(f"{k} {v[0]}-{v[1]}" for k, v in _MAGNITUDE.items())
    durs = ", ".join(f"{k} {v[0]}-{v[1]}" for k, v in _DURATION.items())
    f1 = sf["per_class_f1"]
    ttd = m["onset_to_first_flag_min"]
    ttd_s = "n/a" if ttd["median"] is None else f"{ttd['median']:.0f} min"
    return f"""## Honesty section (read before quoting any number above)

**Support in episodes, not just windows.** Test period fault episodes (onset after the split):
leak {eps["leak"]}, burst {eps["burst"]}, overuse {eps["overuse"]} (detected at least once:
leak {det["leak"]}, burst {det["burst"]}, overuse {det["overuse"]}) across {b["n_flats"]} flats.
The window supports in the table count overlapping windows (window {WINDOW} min, stride
{meta["stride"]} min) of those few episodes - they are NOT independent samples.

**Uncertainty (95% bootstrap CI, {b["method"]}).**

| metric | point | 95% CI |
|---|---|---|
| macro-F1 | {m["macro_f1"]:.3f} | {rng("macro_f1")} |
| leak+burst window recall | {m["leak_burst_recall"]:.3f} | {rng("leak_burst_recall")} |
| leak+burst episode detection ({b["leak_burst_episodes"]} episodes) | {
        b["leak_burst_episode_detection"]:.3f} | {rng("leak_burst_episode_detection")} |
| overuse precision | {m["per_class"]["overuse"]["precision"]:.3f} | {rng("overuse_precision")} |
| overuse F1 | {m["per_class"]["overuse"]["f1"]:.3f} | {rng("overuse_f1")} |

**Leave-flats-out ({lfo["folds"]}-fold over flats; classifier never sees the held-out flats).**
Pooled macro-F1 {lfo["macro_f1"]:.3f} (per fold {
        ", ".join(f"{v:.3f}" for v in lfo["macro_f1_per_fold"])
    }), leak+burst recall {lfo["leak_burst_recall"]:.3f}, per-class F1 {
        {c: round(v, 3) for c, v in lfo["per_class_f1"].items()}
    }. Train windows are from the train period only and held-out flats are scored on the test
period, so this tests unseen households and unseen time together.

**Circularity.** A depth-3 tree on `min_flow` alone gets leak F1 {f1["leak"]:.3f}, burst F1
{f1["burst"]:.3f}, overuse F1 {f1["overuse"]:.3f}, macro-F1 {sf["macro_f1"]:.3f} on the same test
set. On this synthetic generator the additive leak/burst floors make `min_flow` highly informative
for leak and burst (F1 above), and the generator's fault ranges (magnitude, L/min or x: {
        mags
    }; duration, min:
{durs}) mirror the thresholds of the rule fallback in the backend. High leak/burst scores
therefore show the pipeline is wired correctly, not that real leaks are this separable. Overuse
(multiplicative, overlaps normal peaks) is the hard class and the number to watch.

**Onset to first flag ({ttd_s} median, {ttd["episodes_detected"]}/{
        ttd["episodes_total"]
    } episodes).**
Offline classifier, onset to the first window predicted non-normal; ANY non-normal class counts
(a wrong type still counts as detected); quantized by the {meta["stride"]}-minute window stride;
excludes alert debounce, MQTT transport and escalation latency; synthetic data only. It is not an
operational time-to-alert.

**Train/serve parity.** Features are computed by `libs/aquaguard_features`, the same code the
backend imports; `ml/tests/test_parity.py` asserts identical vectors and equal classifier F1.
Synthetic timestamps are UTC and treated as local time for the hour-of-week slot and the night
window (local 00:00-05:00); the backend converts to `settings.timezone` first.
"""


def write_report(path: Path, meta: dict) -> None:
    m = meta["metrics"]
    rows = "\n".join(
        f"| {c} | {v['precision']:.3f} | {v['recall']:.3f} | {v['f1']:.3f} | {v['support']} |"
        for c, v in m["per_class"].items()
    )
    cm = m["confusion_matrix"]
    cm_rows = "\n".join(
        f"| {lab} | " + " | ".join(str(v) for v in row) + " |"
        for lab, row in zip(cm["labels"], cm["matrix"], strict=True)
    )
    ttd = m["onset_to_first_flag_min"]
    ttd_s = "n/a" if ttd["median"] is None else f"{ttd['median']:.0f} min"
    lbr = m["leak_burst_recall"]
    fpr = m["false_positive_rate_normal"]
    iso = meta["isolation"]
    iso_f = "n/a" if iso["flag_rate_on_faults"] is None else f"{iso['flag_rate_on_faults']:.1%}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# Detector report (generated by `python -m ml.scripts.train_detector`)

> **These numbers come from SYNTHETIC data.** The household behaviour, the sensor noise and the
> fault signatures are simulated by `ml/scripts/generate_data.py`, and the faults are far more
> regular than real plumbing failures. The metrics below will **not** transfer to real sensors
> without recalibration (per-flat baselines from real data, threshold/operating-point tuning and
> a labelled field validation). See the honesty section at the end.

- Trained: {meta["trained_at"]} | seed {meta["seed"]} | sklearn {meta["sklearn_version"]}
- Data: {meta["n_flats"]} flats x {meta["days"]} days (hash `{meta["data_hash"]}`), window = {
        WINDOW
    }
  min, stride {meta["stride"]} min
- Split: chronological, train < {meta["split_ts"]} ({meta["n_train"]} windows) /
  test >= split ({meta["n_test"]} windows). Baselines (raw per-minute flow per hour-of-week slot)
  use train-period normal data only; IsolationForest (contamination 0.02) fitted on
  {meta["n_iso_fit"]} train-normal windows.
- Selected hyper-parameters (TimeSeriesSplit(3) on train, macro-F1 {meta["cv_macro_f1"]:.3f}):
  `{json.dumps(meta["hyperparams"])}`

## Held-out test metrics

| class | precision | recall | F1 | support (windows) |
|---|---|---|---|---|
{rows}

- Macro-F1: **{m["macro_f1"]:.3f}**
- Leak+burst recall (any non-normal prediction on a leak/burst window):
  **{"n/a" if lbr is None else f"{lbr:.3f}"}**
- False-positive rate on normal windows: **{"n/a" if fpr is None else f"{fpr:.3f}"}**
- Onset to first non-normal window: median {ttd_s} ({ttd["episodes_detected"]}/{
        ttd["episodes_total"]
    } test episodes flagged; offline, any class counts, quantized by the
  {meta["stride"]}-min stride, excludes debounce/MQTT/escalation latency, synthetic only)
- IsolationForest on test: flags {iso["flag_rate_on_normal"]:.1%} of normal and {iso_f} of fault
  windows (unsupervised side signal)

Confusion matrix (rows = truth, columns = prediction):

| truth \\ pred | {" | ".join(cm["labels"])} |
|---|{"---|" * len(cm["labels"])}
{cm_rows}

## Caveats

- Window labels are the fault state at the window end; windows overlap (stride < window), so
  neighbouring test samples are correlated and the effective sample size is small.
- Slow leaks and drips add a constant floor in a world where normal nights are exactly ~0; real
  households (dripping appliances, filling tanks, pressure cycling) are much noisier.
- Timestamps are treated as UTC (= local) in training; the backend converts to its timezone.

{honesty_section(meta)}""")


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--flats", type=int, default=12)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--stride", type=int, default=5, help="minutes between windows")
    ap.add_argument("--data-dir", type=Path, default=None, help="read generate_data output")
    ap.add_argument("--out", type=Path, default=ML_DIR / "models")
    ap.add_argument("--report-dir", type=Path, default=ML_DIR / "reports")
    ap.add_argument("--quick", action="store_true", help="single hyper-parameter point")
    a = ap.parse_args(argv)

    t0 = time.time()
    if a.data_dir:
        df = load_dataset(a.data_dir)
        a.flats = int(df["flat_id"].nunique())
        a.days = int(len(df) // a.flats // 1440)
    else:
        df = generate_dataset(a.flats, a.days, a.seed)
    train, test, baselines, split = prepare(df, a.days, a.stride)
    assert train["ts"].max() < test["ts"].min(), "temporal leakage"

    normal_tr = train[train["label"] == "normal"]
    iso = IsolationForest(n_estimators=200, contamination=0.02, random_state=a.seed, n_jobs=-1)
    iso.fit(normal_tr[FEATURE_NAMES])

    n_gap = a.flats * max(1, 120 // a.stride)  # ~2h gap between folds (overlapping windows)
    cv = TimeSeriesSplit(n_splits=3, gap=n_gap)
    search = GridSearchCV(
        RandomForestClassifier(class_weight="balanced", random_state=a.seed, n_jobs=-1),
        QUICK_GRID if a.quick else FULL_GRID,
        cv=cv,
        scoring="f1_macro",
        refit=True,
    )
    search.fit(train[FEATURE_NAMES], train["label"])
    clf = search.best_estimator_
    params = {k: (None if v is None else int(v)) for k, v in search.best_params_.items()}

    pred = clf.predict(test[FEATURE_NAMES])
    eps = fault_episodes(df)
    eps = eps[eps["start"] > split]  # onset observable in the test period
    metrics = compute_metrics(test, pred, eps)

    a.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(iso, a.out / "isolation_forest.joblib")
    joblib.dump(clf, a.out / "classifier.joblib")
    joblib.dump(list(FEATURE_NAMES), a.out / "feature_names.joblib")
    (a.out / "baselines.json").write_text(
        json.dumps({k: v.to_dict() for k, v in baselines.items()})
    )
    meta = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_source": "synthetic",
        "data_hash": dataset_hash(df),
        "seed": a.seed,
        "n_flats": a.flats,
        "days": a.days,
        "stride": a.stride,
        "split_ts": split.isoformat(),
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "n_iso_fit": int(len(normal_tr)),
        "hyperparams": params,
        "cv_macro_f1": float(search.best_score_),
        "classes": CLASSES,
        "feature_names": list(FEATURE_NAMES),
        "night_definition": "local hour in [0, 5)",
        "time_basis": "synthetic UTC treated as local time",
        "sklearn_version": sklearn.__version__,
        "isolation": isolation_stats(iso, test),
        "metrics": metrics,
        "bootstrap": bootstrap_ci(test, pred, eps, a.seed),
        "leave_flats_out": leave_flats_out(train, test, params, a.seed),
        "single_feature": single_feature_baseline(train, test, a.seed),
        "note": "Synthetic data; metrics will not transfer to real sensors without recalibration.",
    }
    (a.out / "detector_metadata.json").write_text(json.dumps(meta, indent=2))
    update_manifest(
        a.out,
        [
            "isolation_forest.joblib",
            "classifier.joblib",
            "feature_names.joblib",
            "detector_metadata.json",
            "baselines.json",
        ],
    )
    write_report(a.report_dir / "detector_report.md", meta)

    m, ci = metrics, meta["bootstrap"]["ci95"]
    print(f"[detector] trained in {time.time() - t0:.0f}s  train={len(train)} test={len(test)}")
    print(
        f"  macro-F1={m['macro_f1']:.3f} (95% CI {ci['macro_f1'][0]:.3f}-{ci['macro_f1'][1]:.3f})  "
        f"leak+burst recall={m['leak_burst_recall']:.3f}  FPR(normal)="
        f"{m['false_positive_rate_normal']:.4f}"
    )
    print(f"  per-class F1: { {c: round(v['f1'], 3) for c, v in m['per_class'].items()} }")
    print(
        f"  episodes in test: {m['episodes_by_class']}  onset->first flag: "
        f"{m['onset_to_first_flag_min']}"
    )
    print(
        f"  leave-flats-out macro-F1={meta['leave_flats_out']['macro_f1']:.3f}  "
        f"min_flow-only macro-F1={meta['single_feature']['macro_f1']:.3f}"
    )
    return meta


if __name__ == "__main__":
    main()

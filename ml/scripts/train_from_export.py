"""Train / update the detector from a REAL export of flow_readings + alert feedback.

This is the path to ever claiming more than "validated on synthetic data": it learns from what
the deployed system actually saw and what operators did with its alerts.

Inputs (CSV or Parquet; header samples and the export SQL are in ``ml/README.md``):

* ``--readings``: ``flat_id, ts, flow_lpm``               (UTC timestamps, any sampling rate)
* ``--feedback``: ``alert_id, flat_id, anomaly_type, detected_at, resolved_at, fault_start,
  outcome``
  where ``outcome`` is one of ``confirmed`` (plumber confirmed a real fault: gold positive),
  ``acknowledged`` (operator accepted the alert: weak positive), ``overridden`` / ``false_alarm``
  (operator rejected it: negative), anything else = unlabeled.

What it does
1. Resamples readings to the per-minute grid the shared feature code expects (gaps stay NaN).
2. Chronological split: the last ``--holdout-frac`` of time is a HELD-OUT period. Per-flat
   baselines use TRAIN-period minutes outside confirmed/acknowledged fault intervals only.
3. Unsupervised: IsolationForest fitted on train windows that are not near ANY alert.
4. Supervised (only if enough labelled episodes exist in BOTH periods): RandomForest on
   confirmed/acknowledged positives, overridden negatives and a sample of un-alerted windows
   (assumed normal - a selection-biased label, documented in the report).
5. GUARD: candidate and incumbent (``--model-dir``) are scored on the SAME held-out windows (same
   baselines). Every available metric must be not worse (within ``--tolerance``). The incumbent
   is only unpickled if its SHA-256 matches ``manifest.json`` (joblib files are pickles).
6. The candidate is ALWAYS written to ``<model-dir>/candidates/<utc timestamp>/`` with the same
   manifest/metadata layout as ``train_detector``. The live ``--model-dir`` files are overwritten
   ONLY with ``--promote`` AND a passing guard. Never promote straight away: run the candidate in
   SHADOW MODE first (below).

SHADOW MODE (recommended before ``--promote``): load the candidate directory in a second
``ModelStore`` in the backend (or replay the export through both), log both decisions per window
without alerting from the candidate, and compare against operator outcomes for >= 2-4 weeks. Only
promote if the candidate's alert precision (vs 'overridden') and recall (vs 'confirmed') are not
worse than the incumbent on the same live windows.

Usage:
    python -m ml.scripts.train_from_export --readings readings.parquet --feedback feedback.csv \\
        --model-dir ml/models --tz Europe/Berlin [--promote]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest, RandomForestClassifier

from ml.features.engineering import (
    CLASSES,
    FEATURE_NAMES,
    WARMUP,
    collapse_label,
    extract_features,
    fit_baseline,
)
from ml.scripts import bench_stats as bs
from ml.scripts.artifacts import sha256_file, update_manifest

ML_DIR = Path(__file__).parents[1]
READ_COLS = ["flat_id", "ts", "flow_lpm"]
FB_COLS = ["flat_id", "anomaly_type", "detected_at", "outcome"]  # + optional columns
# outcome -> (is_fault, sample_weight)
OUTCOMES: dict[str, tuple[bool, float]] = {
    "confirmed": (True, 1.0),
    "acknowledged": (True, 0.5),
    "overridden": (False, 0.5),
    "false_alarm": (False, 1.0),
}
ART_FILES = ["isolation_forest.joblib", "classifier.joblib", "feature_names.joblib"]
COOLDOWN_MIN = 120  # windows within this long after an alert are not "clean normal"
LEAD_MIN = 15


# ----------------------------------------------------------------------------- loading
def read_table(path: Path) -> pd.DataFrame:
    return (
        pd.read_parquet(path) if path.suffix.lower() in {".parquet", ".pq"} else pd.read_csv(path)
    )


def _utc(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce")


def load_inputs(readings: Path, feedback: Path | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    r = read_table(readings)
    missing = [c for c in READ_COLS if c not in r.columns]
    if missing:
        raise ValueError(f"readings missing columns {missing}; expected {READ_COLS}")
    r = r[READ_COLS].copy()
    r["flat_id"] = r["flat_id"].astype(str)
    r["ts"] = _utc(r["ts"])
    r["flow_lpm"] = pd.to_numeric(r["flow_lpm"], errors="coerce")
    r = r.dropna(subset=["ts"])
    r = r[(r["flow_lpm"].isna()) | (r["flow_lpm"] >= 0)]  # negative flow is a sensor error
    if feedback is None:
        f = pd.DataFrame(columns=[*FB_COLS, "resolved_at", "fault_start"])
    else:
        f = read_table(feedback)
        missing = [c for c in FB_COLS if c not in f.columns]
        if missing:
            raise ValueError(f"feedback missing columns {missing}; expected {FB_COLS} (+optional)")
        f = f.copy()
        f["flat_id"] = f["flat_id"].astype(str)
        for c in ("detected_at", "resolved_at", "fault_start"):
            f[c] = _utc(f[c]) if c in f.columns else pd.NaT
        f["outcome"] = f["outcome"].astype(str).str.lower().str.strip()
    return r, f


def input_hash(*paths: Path | None) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p is not None:
            h.update(sha256_file(p).encode())
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------------- windows
def anomaly_class(anomaly_type: str) -> str:
    """Backend anomaly_type -> detector class ('' if it has no class, e.g. 'unknown')."""
    t = str(anomaly_type).strip().lower()
    if t in ("leak", "burst", "overuse"):
        return t
    try:
        return collapse_label(t) if t != "none" else ""
    except KeyError:
        return ""


def _minute_series(
    g: pd.DataFrame, tz: str
) -> tuple[pd.DatetimeIndex, np.ndarray, pd.DatetimeIndex]:
    m = g.assign(min=g["ts"].dt.floor("min")).groupby("min")["flow_lpm"].mean()
    full = pd.date_range(m.index.min(), m.index.max(), freq="min", tz="UTC")
    flow = m.reindex(full).to_numpy(dtype=float)
    return full.tz_convert(tz), flow, full


def build_windows(
    readings: pd.DataFrame,
    feedback: pd.DataFrame,
    *,
    tz: str,
    stride: int,
    holdout_frac: float,
) -> tuple[pd.DataFrame, pd.Timestamp, dict]:
    """Window table (FEATURE_NAMES + flat_id, ts(UTC), cls, status, weight, clean, period) and
    the held-out cut time. Baselines are per-flat from TRAIN-period non-fault minutes only."""
    grids = {}
    for fid, g in readings.groupby("flat_id", sort=True):
        ts_local, flow, ts_utc = _minute_series(g, tz)
        if len(flow) > WARMUP + 2 * stride:
            grids[fid] = (ts_local, flow, ts_utc)
    if not grids:
        raise ValueError("no flat has enough readings (need > 2 h of per-minute data)")
    t_min = min(v[2][0] for v in grids.values())
    t_max = max(v[2][-1] for v in grids.values())
    cut = t_min + (t_max - t_min) * (1.0 - holdout_frac)
    parts, info = [], {"n_flats": len(grids), "episodes": {"positive": 0, "negative": 0}}
    for fid, (ts_local, flow, ts_utc) in grids.items():
        n = len(flow)
        pos_cls = np.full(n, "", dtype=object)
        pos_w = np.zeros(n)
        neg_w = np.zeros(n)
        near = np.zeros(n, bool)  # near ANY alert (excluded from clean normals)
        fb = feedback[feedback["flat_id"] == fid]
        for row in fb.itertuples():
            if pd.isna(row.detected_at):
                continue
            start = (
                row.fault_start
                if pd.notna(getattr(row, "fault_start", pd.NaT))
                else (row.detected_at - pd.Timedelta(minutes=LEAD_MIN))
            )
            end = (
                row.resolved_at
                if pd.notna(getattr(row, "resolved_at", pd.NaT))
                else row.detected_at
            )
            end = max(end, row.detected_at)
            lo = int(ts_utc.searchsorted(start, "left"))
            hi = int(ts_utc.searchsorted(end, "right"))
            near[lo : min(n, hi + COOLDOWN_MIN)] = True
            is_fault, w = OUTCOMES.get(row.outcome, (None, 0.0))
            if is_fault is None:
                continue
            if is_fault:
                typ = str(row.anomaly_type)
                cls = anomaly_class(typ)
                info["episodes"]["positive"] += 1
                pos_cls[lo:hi] = cls or "unknown"
                pos_w[lo:hi] = np.maximum(pos_w[lo:hi], w)
            else:
                info["episodes"]["negative"] += 1
                neg_w[lo:hi] = np.maximum(neg_w[lo:hi], w)
        base_ok = (pos_cls == "") & np.isfinite(flow) & (ts_utc < cut)
        base = fit_baseline(ts_local[base_ok], flow[base_ok])
        feats = extract_features(ts_local, flow, base, stride)
        sel = np.arange(WARMUP, n, stride)
        feats.insert(1, "flat_id", fid)
        feats["ts"] = ts_utc[sel]
        cls = pos_cls[sel]
        status = np.where(cls != "", "positive", np.where(neg_w[sel] > 0, "negative", "unlabeled"))
        feats["cls"] = np.where(cls != "", cls, "normal")
        feats["status"] = status
        feats["weight"] = np.where(cls != "", pos_w[sel], np.where(neg_w[sel] > 0, neg_w[sel], 1.0))
        feats["clean"] = (~near[sel]) & (status == "unlabeled")
        feats["period"] = np.where(ts_utc[sel] < cut, "train", "holdout")
        parts.append(feats)
        info.setdefault("baselines", {})[fid] = base.to_dict()
    w = pd.concat(parts, ignore_index=True).sort_values(["flat_id", "ts"], ignore_index=True)
    return w, cut, info


# ----------------------------------------------------------------------------- guard
def guard(candidate: dict, incumbent: dict | None, tolerance: float) -> dict:
    """Candidate must be NOT WORSE than the incumbent on every shared metric (within tolerance).

    Metric dicts map name -> value (None = not computable). Higher is better except names ending
    in ``_fp_rate`` (lower is better). No incumbent -> ``first_model`` (caller decides)."""
    if incumbent is None:
        return {"passed": None, "reason": "no incumbent", "checks": []}
    checks = []
    for k, cv in candidate.items():
        iv = incumbent.get(k)
        if cv is None or iv is None:
            continue
        lower = k.endswith("_fp_rate")
        ok = cv <= iv + tolerance if lower else cv >= iv - tolerance
        checks.append(
            {
                "metric": k,
                "candidate": cv,
                "incumbent": iv,
                "ok": bool(ok),
                "lower_is_better": lower,
            }
        )
    if not checks:
        return {
            "passed": False,
            "reason": "no comparable metric on the held-out period",
            "checks": [],
        }
    bad = [c["metric"] for c in checks if not c["ok"]]
    return {
        "passed": not bad,
        "reason": "all metrics not worse" if not bad else f"worse on: {', '.join(bad)}",
        "checks": checks,
    }


def held_out_metrics(iso, clf, ho: pd.DataFrame, classes: list[str] | None = None) -> dict:
    """Metrics on held-out windows. fp_rate: flag rate on clean-normal + overridden windows."""
    x = ho[FEATURE_NAMES]
    neg = ho["status"].isin(["negative"]) | ho["clean"]
    pos = ho["status"] == "positive"
    out: dict[str, float | None] = {}
    flag = iso.predict(x) == -1
    out["iso_fp_rate"] = float(flag[neg.to_numpy()].mean()) if neg.any() else None
    out["iso_recall"] = float(flag[pos.to_numpy()].mean()) if pos.any() else None
    out["clf_anomaly_f1"] = out["clf_macro_f1"] = None
    lab = ho[ho["status"].isin(["positive", "negative"]) | ho["clean"]]
    if clf is not None and len(lab) and (lab["status"] == "positive").any():
        pred = clf.predict(lab[FEATURE_NAMES])
        y = bs.to_idx(lab["cls"].where(lab["cls"].isin(CLASSES), "overuse").to_numpy())
        p = bs.to_idx(np.asarray([str(v) for v in pred]))
        out["clf_anomaly_f1"] = float(bs.binary_f1(bs.binary_counts(y, p)))
        out["clf_macro_f1"] = float(bs.macro_f1(bs.class_counts(y, p)))
    return out


# ----------------------------------------------------------------------------- incumbent
class UntrustedModelError(RuntimeError):
    pass


def load_incumbent(model_dir: Path) -> tuple[object | None, object | None, dict]:
    """(iso, clf, meta) from a model dir, ONLY if every file matches manifest.json."""
    iso_p, clf_p = model_dir / "isolation_forest.joblib", model_dir / "classifier.joblib"
    if not iso_p.is_file():
        return None, None, {}
    try:
        files = json.loads((model_dir / "manifest.json").read_text())["files"]
    except (OSError, ValueError, KeyError):
        raise UntrustedModelError(
            f"{model_dir}: models exist but manifest.json is missing/invalid"
        ) from None
    for p in (iso_p, clf_p):
        if p.is_file() and files.get(p.name) != sha256_file(p):
            raise UntrustedModelError(
                f"{p.name}: SHA-256 does not match manifest.json; refusing to unpickle"
            )
    meta_p = model_dir / "detector_metadata.json"
    meta = json.loads(meta_p.read_text()) if meta_p.is_file() else {}
    # SAFE: joblib files are pickles; they are loaded only after the SHA-256 check above matched
    # manifest.json (a trusted, locally trained artefact), never from an arbitrary path.
    return joblib.load(iso_p), (joblib.load(clf_p) if clf_p.is_file() else None), meta


# ----------------------------------------------------------------------------- main
def train(a: argparse.Namespace) -> dict:
    readings, feedback = load_inputs(a.readings, a.feedback)
    w, cut, info = build_windows(
        readings, feedback, tz=a.tz, stride=a.stride, holdout_frac=a.holdout_frac
    )
    tr, ho = w[w["period"] == "train"], w[w["period"] == "holdout"]
    tr = tr[(tr["ts"].dt.minute % a.train_stride) == 0]
    rng = np.random.default_rng(a.seed)

    # incumbent (hash-verified) - raises UntrustedModelError if tampered
    inc_iso, inc_clf, inc_meta = load_incumbent(a.model_dir)

    clean_tr = tr[tr["clean"]]
    if len(clean_tr) < a.min_clean_windows:
        raise SystemExit(
            f"only {len(clean_tr)} clean train windows (< {a.min_clean_windows}); need more data"
        )
    iso = IsolationForest(
        n_estimators=200, contamination=a.contamination, random_state=a.seed, n_jobs=-1
    ).fit(clean_tr[FEATURE_NAMES])

    # supervised part, only with enough labelled episodes in both periods
    pos_tr, pos_ho = tr[tr["status"] == "positive"], ho[ho["status"] == "positive"]
    reason = None

    def n_runs(d: pd.DataFrame) -> int:
        if d.empty:
            return 0
        d = d.sort_values(["flat_id", "ts"])
        gap = (d["ts"].diff() > pd.Timedelta(minutes=a.stride * 3)) | (
            d["flat_id"] != d["flat_id"].shift()
        )
        return int(gap.sum())

    ep_tr, ep_ho = n_runs(pos_tr), n_runs(pos_ho)
    clf = None
    if ep_tr < a.min_positive_episodes or ep_ho < a.min_holdout_positives:
        reason = (
            f"not enough labelled positive episodes (train {ep_tr} < {a.min_positive_episodes} or "
            f"holdout {ep_ho} < {a.min_holdout_positives}); supervised classifier skipped"
        )
    elif pos_tr["cls"].nunique() < 1:
        reason = "no classed positives"
    else:
        neg_tr = tr[tr["status"] == "negative"]
        unl = tr[tr["clean"]]
        k = min(len(unl), int(a.normal_ratio * max(len(pos_tr) + len(neg_tr), 1)))
        unl = unl.iloc[rng.permutation(len(unl))[:k]].assign(weight=0.25)
        sup = pd.concat([pos_tr[pos_tr["cls"] != "unknown"], neg_tr, unl])
        clf = RandomForestClassifier(
            n_estimators=200, class_weight="balanced", random_state=a.seed, n_jobs=-1
        ).fit(sup[FEATURE_NAMES], sup["cls"], sample_weight=sup["weight"].to_numpy())

    cand_clf = clf if clf is not None else inc_clf
    clf_source = (
        "trained_from_export"
        if clf is not None
        else ("copied_from_incumbent" if inc_clf is not None else "none")
    )
    cand_m = held_out_metrics(iso, clf, ho)
    inc_m = held_out_metrics(inc_iso, inc_clf, ho) if inc_iso is not None else None
    if inc_m is not None and clf is None:
        # classifier unchanged: only the isolation-forest metrics are a real comparison
        cand_m = {k: v for k, v in cand_m.items() if k.startswith("iso_")}
    g = guard(cand_m, inc_m, a.tolerance)
    if len(ho) < a.min_holdout_windows:
        g = {
            "passed": False,
            "reason": f"held-out period has only {len(ho)} windows",
            "checks": g["checks"],
        }
    first = g["passed"] is None
    decision = {
        "guard": g,
        "first_model": first,
        "promote_requested": bool(a.promote),
        "will_overwrite_live": False,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    cand_dir = a.model_dir / "candidates" / stamp
    meta = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_source": "real_export",
        "data_hash": input_hash(a.readings, a.feedback),
        "seed": a.seed,
        "n_flats": info["n_flats"],
        "stride": a.stride,
        "split_ts": cut.isoformat(),
        "n_train": int(len(tr)),
        "n_test": int(len(ho)),
        "n_iso_fit": int(len(clean_tr)),
        "hyperparams": {"contamination": a.contamination, "n_estimators": 200},
        "classes": CLASSES,
        "feature_names": list(FEATURE_NAMES),
        "night_definition": "local hour in [0, 5)",
        "time_basis": f"timestamps converted to {a.tz} before hour/weekday features",
        "sklearn_version": sklearn.__version__,
        "iso_offset": float(iso.offset_),
        "classifier_source": clf_source,
        "supervised_skipped_reason": reason,
        "labelled_episodes": {
            "train_positive": ep_tr,
            "holdout_positive": ep_ho,
            **info["episodes"],
        },
        "holdout_metrics": {"candidate": cand_m, "incumbent": inc_m},
        "guard": g,
        "shadow_mode": "REQUIRED before promotion: run the candidate beside the incumbent "
        "and compare against operator outcomes",
        "note": "Trained from a real export; labels are operator feedback "
        "(selection-biased). See ml/README.md.",
    }
    decision["candidate_dir"] = str(cand_dir)
    if not a.dry_run:
        cand_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(iso, cand_dir / "isolation_forest.joblib")
        names = [
            "isolation_forest.joblib",
            "feature_names.joblib",
            "baselines.json",
            "detector_metadata.json",
        ]
        if cand_clf is not None:
            joblib.dump(cand_clf, cand_dir / "classifier.joblib")
            names.insert(1, "classifier.joblib")
        joblib.dump(list(FEATURE_NAMES), cand_dir / "feature_names.joblib")
        (cand_dir / "baselines.json").write_text(json.dumps(info["baselines"]))
        (cand_dir / "detector_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
        update_manifest(cand_dir, names)
        (cand_dir / "export_report.md").write_text(render_report(meta, decision))
        ok = g["passed"] is True or (first and a.allow_first_model)
        if a.promote and ok:
            promote(cand_dir, a.model_dir, names)
            decision["will_overwrite_live"] = True
    decision["meta"] = meta
    return decision


def promote(cand_dir: Path, model_dir: Path, names: list[str]) -> None:
    """Copy candidate files over the live ones (atomic per file) and refresh the manifest."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        fd, tmp = tempfile.mkstemp(dir=model_dir, prefix=f".{n}.")
        os.close(fd)
        shutil.copyfile(cand_dir / n, tmp)
        os.replace(tmp, model_dir / n)
    update_manifest(model_dir, names)


def render_report(meta: dict, decision: dict) -> str:
    g = decision["guard"]
    rows = "\n".join(
        f"| {c['metric']} | {c['candidate']:.4f} | {c['incumbent']:.4f} | "
        f"{'ok' if c['ok'] else 'WORSE'} |"
        for c in g["checks"]
    )
    verdict = {True: "PASSED", False: "FAILED", None: "NO INCUMBENT"}[g["passed"]]
    return f"""# Export training report

- Data source: **{meta["data_source"]}** (hash `{meta["data_hash"]}`), {meta["n_flats"]} flats
- Held-out period starts {meta["split_ts"]} ({meta["n_test"]} windows); train windows {
        meta["n_train"]
    }
- Labelled episodes: {meta["labelled_episodes"]}
- Classifier: {meta["classifier_source"]}{
        " - " + meta["supervised_skipped_reason"] if meta["supervised_skipped_reason"] else ""
    }

## Guard: {verdict} ({g["reason"]})

| metric | candidate | incumbent | status |
|---|---|---|---|
{rows}

Live models overwritten: **{
        decision["will_overwrite_live"]
    }** (requires `--promote` and a passing guard).

> Labels come from operator feedback: `overridden` = negative,
> `acknowledged`/`confirmed` = positive;
> un-alerted windows are *assumed* normal. This is selection-biased evidence. Run the candidate in
> shadow mode before promotion.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--readings", type=Path, required=True)
    ap.add_argument(
        "--feedback", type=Path, default=None, help="alert feedback (omit = unsupervised only)"
    )
    ap.add_argument("--model-dir", type=Path, default=ML_DIR / "models")
    ap.add_argument(
        "--tz", default="UTC", help="IANA timezone of the households (hour/weekday features)"
    )
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--train-stride", type=int, default=15)
    ap.add_argument("--holdout-frac", type=float, default=0.25)
    ap.add_argument("--contamination", type=float, default=0.02)
    ap.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help="allowed worsening per metric (0 = strictly not worse)",
    )
    ap.add_argument("--min-positive-episodes", type=int, default=20)
    ap.add_argument("--min-holdout-positives", type=int, default=5)
    ap.add_argument("--min-clean-windows", type=int, default=500)
    ap.add_argument("--min-holdout-windows", type=int, default=300)
    ap.add_argument("--normal-ratio", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--promote", action="store_true", help="overwrite live models if the guard passes"
    )
    ap.add_argument(
        "--allow-first-model",
        action="store_true",
        help="with --promote: allow when no incumbent exists",
    )
    ap.add_argument("--dry-run", action="store_true", help="evaluate only, write nothing")
    return ap


def main(argv: list[str] | None = None) -> dict:
    a = build_parser().parse_args(argv)
    try:
        d = train(a)
    except UntrustedModelError as exc:
        print(f"[export] REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    g = d["guard"]
    print(f"[export] guard: {g['reason']} (passed={g['passed']}); candidate: {d['candidate_dir']}")
    if a.promote and not d["will_overwrite_live"]:
        print("[export] live models NOT overwritten", file=sys.stderr)
    return d


if __name__ == "__main__":
    out = main()
    sys.exit(3 if out["promote_requested"] and not out["will_overwrite_live"] else 0)

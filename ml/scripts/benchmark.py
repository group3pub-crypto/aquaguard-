# ruff: noqa: E501  (report text is long-form prose/tables)
"""Benchmark: does the ML detector beat simple rules? (SYNTHETIC data only.)

Compares seven detection policies on the SAME chronological held-out split with the SAME
episode-level metrics, bootstrap CIs and paired tests, then stress-tests them under distribution
shift and an assumed YF-S201 sensor model, and measures the smallest leak the pipeline can see.

Policies
    rules          production ``classify_anomaly_rules`` (backend/app/services/detector.py)
    tree_min_flow  depth-3 decision tree on min_flow only
    night_rules    two thresholds on night_min_flow, fitted on the train period
    iso            IsolationForest alone as a class-less anomaly flag
    rf             the full RandomForest classifier (as trained by train_detector)
    ml_served      rf + the IsolationForest "unknown" fallback, as classify_anomaly_ml serves it
    hybrid_gate    rules gate; ML (rf) vetoes (P(normal)>0.5) and supplies the class
    hybrid_union   rf alert OR rules alert (rf class first)

BACKEND DEPENDENCY (dev script only): the rule baseline is loaded from the backend source file by
path with importlib (falling back to AST-extracting just ``classify_anomaly_rules`` + ``Detection``
when the backend's own dependencies are not installed). The ``ml`` package itself never imports
the backend; only this benchmark does, so the baseline is always the code that ships. The SHA-256
of the loaded file is printed in the report.

Usage (repo root):
    python -m ml.scripts.benchmark                          # full run, writes ml/reports/benchmark.md
    python -m ml.scripts.benchmark --flats 3 --days 14 --boot 50 --quick --curve-flats 2 \
        --out-dir /tmp/bench                                # smoke run
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.tree import DecisionTreeClassifier

from ml.features.engineering import (
    FEATURE_NAMES,
    build_baselines,
    collapse_label,
    extract_features,
    fault_episodes,
    featurize_dataset,
    fit_baseline,
)
from ml.scripts import bench_stats as bs
from ml.scripts.generate_data import (
    START,
    FaultEpisode,
    apply_faults,
    apply_sensor_model,
    generate_dataset,
    make_device,
    make_households,
    resolve_sensor,
    true_base_flow,
)
from ml.scripts.train_detector import TRAIN_FRAC, split_timestamp

ML_DIR = Path(__file__).parents[1]
REPO = ML_DIR.parent
DEFAULT_RULES_PATH = REPO / "backend" / "app" / "services" / "detector.py"
BASELINES = ["rules", "tree_min_flow", "night_rules", "iso"]
CONTENDERS = ["rf", "ml_served", "hybrid_gate"]
POLICIES = [
    "rules",
    "tree_min_flow",
    "night_rules",
    "iso",
    "rf",
    "ml_served",
    "hybrid_gate",
    "hybrid_union",
]
POLICY_DESC = {
    "rules": "(a) production rules",
    "tree_min_flow": "(b) tree on min_flow",
    "night_rules": "(c) night_min_flow rules",
    "iso": "(d) IsolationForest alone",
    "rf": "(e) full RandomForest",
    "ml_served": "(e') RF + IsoForest fallback (as served)",
    "hybrid_gate": "(f) hybrid: rules gate + ML",
    "hybrid_union": "(f') hybrid: rules OR ML",
}
NO_CLASS = {"iso"}  # policies without a class output (macro-F1 not defined)
LB_FAULTS = ("slow_leak", "running_toilet", "dripping_tap", "burst")
FAULT_ORDER = ("slow_leak", "running_toilet", "dripping_tap", "burst", "overuse")
RULE_TYPE = {
    "burst": "burst",
    "leak": "leak",
    "running_toilet": "leak",
    "dripping_tap": "leak",
    "overuse": "overuse",
}
# (metric, label, higher_is_better, unit)
METRICS = [
    ("macro_f1", "macro-F1 (4 classes)", True),
    ("anomaly_f1", "alert F1 (any non-normal, windows)", True),
    ("lb_detect", "leak+burst episode detection", True),
    ("lb_detect_deb", "leak+burst episode detection, debounced", True),
    ("fa_week", "false alarms / flat-week", False),
    ("fa_week_deb", "false alarms / flat-week, debounced", False),
    ("fa_week_lb", "leak/burst-class false alarms / flat-week", False),
    ("fa_week_lb_deb", "leak/burst-class false alarms / flat-week, debounced", False),
    ("ttd_med", "median time-to-detect, min (undetected censored)", False),
    ("ttd_med_deb", "median time-to-detect, debounced, min", False),
]
CLASS_METRICS = {"macro_f1", "fa_week_lb", "fa_week_lb_deb"}  # undefined without a class output
HIGHER = {m: h for m, _, h in METRICS}
LABEL = {m: lab for m, lab, _ in METRICS}


# ----------------------------------------------------------------------------- rules baseline
def load_production_rules(path: Path | None = None) -> tuple[callable, dict]:
    """Return (``classify_anomaly_rules``, provenance) from the backend source file.

    Tries a real importlib import of the file first (backend dir on sys.path temporarily); if the
    backend's dependencies (sqlalchemy, structlog, ...) are missing, compiles only the pure
    functions ``Detection``, ``_clip`` and ``classify_anomaly_rules`` out of the same file via
    ``ast`` (identical source text, no behaviour change).
    """
    p = Path(path or DEFAULT_RULES_PATH).resolve()
    src = p.read_text()
    prov = {"path": str(p), "sha256": hashlib.sha256(src.encode()).hexdigest()}
    name = "_aquaguard_backend_detector"
    before = set(sys.modules)
    sys.path.insert(0, str(p.parents[2]))  # backend/ so ``import app...`` works
    try:
        spec = importlib.util.spec_from_file_location(name, p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        prov["mode"] = "importlib (full module import)"
        return mod.classify_anomaly_rules, prov
    except Exception as exc:  # backend deps not installed: extract the pure functions
        for k in set(sys.modules) - before:
            sys.modules.pop(k, None)
        prov["import_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        sys.path.remove(str(p.parents[2]))
    tree = ast.parse(src)
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.ClassDef) and n.name == "Detection")
        or (isinstance(n, ast.FunctionDef) and n.name in ("_clip", "classify_anomaly_rules"))
    ]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    import __future__

    from dataclasses import dataclass as _dc
    from dataclasses import field as _field
    from typing import Any

    mod.__dict__.update({"dataclass": _dc, "field": _field, "Any": Any})
    code = compile(
        ast.fix_missing_locations(ast.Module(body=keep, type_ignores=[])),
        str(p),
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    exec(code, mod.__dict__)
    prov["mode"] = "ast extraction of classify_anomaly_rules/Detection (same source file)"
    return mod.classify_anomaly_rules, prov


def rules_labels(w: pd.DataFrame, fn) -> np.ndarray:
    """Collapsed class per window from the production rule classifier."""
    out = np.empty(len(w), dtype=object)
    for i, rec in enumerate(w[FEATURE_NAMES].to_dict("records")):
        d = fn(rec, 0.0)
        out[i] = "normal" if d is None else RULE_TYPE[d.anomaly_type]
    return out


# ----------------------------------------------------------------------------- data
@dataclass(frozen=True)
class Cond:
    name: str
    profile: str = "default"
    sensor: str = "none"
    yfs_min: float | None = None

    @property
    def synthetic_label(self) -> str:
        return f"profile={self.profile}, sensor={self.sensor}" + (
            f" (min flow {self.yfs_min})" if self.yfs_min is not None else ""
        )


COND_DEFAULT = Cond("default", "default", "none")
COND_SENSOR = Cond("default+yfs201", "default", "yfs201")
COND_SHIFT = Cond("shifted", "shifted", "none")
COND_BOTH = Cond("shifted+yfs201", "shifted", "yfs201")
ALL_CONDS = [COND_DEFAULT, COND_SENSOR, COND_SHIFT, COND_BOTH]


@dataclass
class Geo:
    """Sorted-window geometry shared by every policy evaluated on one window table."""

    n_flats: int
    flat: np.ndarray
    ts: np.ndarray  # minutes since START
    pos: np.ndarray  # position within flat
    first: np.ndarray
    starts: np.ndarray
    ends: np.ndarray
    y: np.ndarray
    stride: int

    @classmethod
    def make(cls, w: pd.DataFrame, stride: int) -> tuple[Geo, dict]:
        codes, uniq = pd.factorize(w["flat_id"], sort=True)
        flat = codes.astype(int)
        first = np.r_[True, flat[1:] != flat[:-1]]
        starts = np.flatnonzero(first)
        ends = np.r_[starts[1:], len(w)]
        pos = np.arange(len(w)) - starts[flat]
        ts = ((w["ts"] - START) // pd.Timedelta(minutes=1)).to_numpy(dtype=np.int64)
        y = bs.to_idx(w["label"].to_numpy())
        return cls(len(uniq), flat, ts, pos, first, starts, ends, y, stride), {
            f: i for i, f in enumerate(uniq)
        }


@dataclass
class Prepared:
    cond: Cond
    train: pd.DataFrame
    test: pd.DataFrame
    geo: Geo
    ep: pd.DataFrame  # test-period episodes with window slices
    normal: pd.DataFrame
    ngeo: Geo
    test_days: float
    _rules_test: np.ndarray | None = None
    _rules_norm: np.ndarray | None = None
    ep_lo: np.ndarray = field(default_factory=lambda: np.zeros(0, int))
    ep_hi: np.ndarray = field(default_factory=lambda: np.zeros(0, int))

    def rules(self, fn) -> tuple[np.ndarray, np.ndarray]:
        if self._rules_test is None:
            self._rules_test = rules_labels(self.test, fn)
            self._rules_norm = rules_labels(self.normal, fn)
        return self._rules_test, self._rules_norm


def _window_table(df: pd.DataFrame, days: int, stride: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    split = split_timestamp(days)
    w = featurize_dataset(df, build_baselines(df, split), stride)
    w = w.sort_values(["flat_id", "ts"], ignore_index=True)
    return w[w["ts"] < split].reset_index(drop=True), w[w["ts"] >= split].reset_index(drop=True)


def prepare_condition(
    cond: Cond,
    flats: int,
    days: int,
    seed: int,
    stride: int,
    normal_flats: int,
    train_stride: int = 15,
) -> Prepared:
    kw = {"profile": cond.profile, "sensor_model": cond.sensor, "yfs_min_flow": cond.yfs_min}
    df = generate_dataset(flats, days, seed, **kw)
    train, test = _window_table(df, days, stride)
    train = train[(train["ts"].dt.minute % train_stride) == 0].reset_index(drop=True)
    split = split_timestamp(days)
    eps = fault_episodes(df)
    eps = eps[eps["start"] > split].reset_index(drop=True)
    dfn = generate_dataset(normal_flats, days, seed + 1000, inject_faults=False, **kw)
    _, normal = _window_table(dfn, days, stride)
    geo, fmap = Geo.make(test, stride)
    ngeo, _ = Geo.make(normal, stride)
    ep_lo, ep_hi = [], []
    for e in eps.itertuples():
        f = fmap[e.flat_id]
        a, b = geo.starts[f], geo.ends[f]
        s = int((e.start - START) // pd.Timedelta(minutes=1))
        t = int((e.end - START) // pd.Timedelta(minutes=1))
        seg = geo.ts[a:b]
        ep_lo.append(a + int(np.searchsorted(seg, s, "left")))
        ep_hi.append(a + int(np.searchsorted(seg, t, "right")))
    eps = eps.assign(
        label=[collapse_label(f) for f in eps["fault"]],
        dur=((eps["end"] - eps["start"]) // pd.Timedelta(minutes=1)).astype(int) + 1,
        s_min=((eps["start"] - START) // pd.Timedelta(minutes=1)).astype(int),
    )
    return Prepared(
        cond,
        train,
        test,
        geo,
        eps,
        normal,
        ngeo,
        float(days - int(days * TRAIN_FRAC)),
        ep_lo=np.asarray(ep_lo, int),
        ep_hi=np.asarray(ep_hi, int),
    )


# ----------------------------------------------------------------------------- policies
@dataclass
class Models:
    tree: DecisionTreeClassifier
    night: tuple[float, float]  # (leak threshold, burst threshold) on night_min_flow
    iso: IsolationForest
    rf: RandomForestClassifier


def _night_rule(night_min: np.ndarray, tl: float, tb: float) -> np.ndarray:
    return np.where(night_min >= tb, "burst", np.where(night_min >= tl, "leak", "normal"))


def fit_models(train: pd.DataFrame, seed: int, quick: bool) -> Models:
    tree = DecisionTreeClassifier(max_depth=3, class_weight="balanced", random_state=seed)
    tree.fit(train[["min_flow"]], train["label"])
    y = bs.to_idx(train["label"].to_numpy())
    nm = train["night_min_flow"].to_numpy()
    best, best_t = -1.0, (0.15, 8.0)
    for tl in (0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0):
        for tb in (3.0, 5.0, 8.0, 12.0, 15.0, 20.0):
            if tb <= tl:
                continue
            p = bs.to_idx(_night_rule(nm, tl, tb))
            f = float(bs.macro_f1(bs.class_counts(y, p)))
            if f > best:
                best, best_t = f, (tl, tb)
    normal_tr = train[train["label"] == "normal"]
    iso = IsolationForest(n_estimators=200, contamination=0.02, random_state=seed, n_jobs=-1)
    iso.fit(normal_tr[FEATURE_NAMES])
    rf_kw = (
        {"n_estimators": 60, "max_depth": 12, "min_samples_leaf": 2}
        if quick
        else {"n_estimators": 200, "max_depth": None, "min_samples_leaf": 1}
    )
    rf = RandomForestClassifier(class_weight="balanced", random_state=seed, n_jobs=-1, **rf_kw)
    rf.fit(train[FEATURE_NAMES], train["label"])
    return Models(tree, best_t, iso, rf)


def predict_policies(m: Models, w: pd.DataFrame, rules_lbl: np.ndarray) -> dict[str, np.ndarray]:
    x = w[FEATURE_NAMES]
    proba = m.rf.predict_proba(x)
    classes = np.array([str(c) for c in m.rf.classes_])
    rf = classes[np.argmax(proba, axis=1)]
    ni = list(classes).index("normal")
    p_norm = proba[:, ni]
    pn = proba.copy()
    pn[:, ni] = -1.0
    nn_label = classes[np.argmax(pn, axis=1)]
    iso_anom = m.iso.predict(x) == -1
    fire = rules_lbl != "normal"
    ml_fire = p_norm <= 0.5
    return {
        "rules": rules_lbl,
        "tree_min_flow": m.tree.predict(w[["min_flow"]]),
        "night_rules": _night_rule(w["night_min_flow"].to_numpy(), *m.night),
        "iso": np.where(iso_anom, "unknown", "normal"),
        "rf": rf,
        "ml_served": np.where((rf == "normal") & iso_anom, "unknown", rf),
        "hybrid_gate": np.where(fire & ml_fire, nn_label, "normal"),
        "hybrid_union": np.where(ml_fire, nn_label, np.where(fire, rules_lbl, "normal")),
    }


# ----------------------------------------------------------------------------- unit statistics
@dataclass
class Units:
    """Sufficient statistics of one policy on one evaluation (resampled by the bootstrap)."""

    cc: np.ndarray  # (n_flats, 4, 3)
    bc: np.ndarray  # (n_flats, 3)
    y: np.ndarray
    p: np.ndarray
    lb_det: np.ndarray  # per leak+burst episode
    lb_det_deb: np.ndarray
    lb_ttd_c: np.ndarray  # censored at episode duration
    lb_ttd_c_deb: np.ndarray
    lb_ttd: np.ndarray  # NaN if undetected
    lb_ttd_deb: np.ndarray
    ep_fault: np.ndarray  # per ALL episodes
    ep_det: np.ndarray
    fa: np.ndarray  # per normal-cohort flat
    fa_deb: np.ndarray
    fa_lb: np.ndarray  # only leak/burst-class alarms count
    fa_lb_deb: np.ndarray
    weeks: np.ndarray


def _debounce(flag: np.ndarray, geo: Geo, k: int) -> np.ndarray:
    out = flag.copy()
    for j in range(1, k):
        sh = np.r_[np.zeros(j, bool), flag[:-j]]
        out &= sh & (geo.pos >= j)
    return out


def _events(flag: np.ndarray, geo: Geo) -> np.ndarray:
    prev = np.r_[False, flag[:-1]] & ~geo.first
    return np.bincount(geo.flat[flag & ~prev], minlength=geo.n_flats).astype(float)


def compute_units(
    prep: Prepared, labels_test: np.ndarray, labels_norm: np.ndarray, debounce: int
) -> Units:
    geo = prep.geo
    p = bs.to_idx(labels_test)
    flag, deb = p != 0, _debounce(p != 0, geo, debounce)
    cc = np.stack(
        [bs.class_counts(geo.y[a:b], p[a:b]) for a, b in zip(geo.starts, geo.ends, strict=True)]
    )
    bc = np.stack(
        [bs.binary_counts(geo.y[a:b], p[a:b]) for a, b in zip(geo.starts, geo.ends, strict=True)]
    )
    ep = prep.ep
    det_all, ttd, ttd_d = [], [], []
    for lo, hi, s in zip(prep.ep_lo, prep.ep_hi, ep["s_min"], strict=True):
        seg, dseg = flag[lo:hi], deb[lo:hi].copy()
        dseg[: debounce - 1] = False  # all k windows must lie inside the episode
        for arr, store in ((seg, ttd), (dseg, ttd_d)):
            nz = np.flatnonzero(arr)
            store.append(float(geo.ts[lo + nz[0]] - s) if len(nz) else np.nan)
    ttd, ttd_d = np.asarray(ttd), np.asarray(ttd_d)
    det_all = ~np.isnan(ttd)
    lb = ep["fault"].isin(LB_FAULTS).to_numpy()
    dur = ep["dur"].to_numpy(dtype=float)
    ng = prep.ngeo
    pn = bs.to_idx(labels_norm)
    fn_ = pn != 0
    weeks = np.array(
        [(b - a) * ng.stride / 1440.0 / 7.0 for a, b in zip(ng.starts, ng.ends, strict=True)]
    )
    return Units(
        cc=cc,
        bc=bc,
        y=geo.y,
        p=p,
        lb_det=(~np.isnan(ttd[lb])).astype(float),
        lb_det_deb=(~np.isnan(ttd_d[lb])).astype(float),
        lb_ttd_c=np.where(np.isnan(ttd[lb]), dur[lb], ttd[lb]),
        lb_ttd_c_deb=np.where(np.isnan(ttd_d[lb]), dur[lb], ttd_d[lb]),
        lb_ttd=ttd[lb],
        lb_ttd_deb=ttd_d[lb],
        ep_fault=ep["fault"].to_numpy(),
        ep_det=det_all.astype(float),
        fa=_events(fn_, ng),
        fa_deb=_events(_debounce(fn_, ng, debounce), ng),
        fa_lb=_events(np.isin(pn, (1, 2)), ng),
        fa_lb_deb=_events(_debounce(np.isin(pn, (1, 2)), ng, debounce), ng),
        weeks=weeks,
    )


def metric_samples(u: Units, name: str, f: np.ndarray, e: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Metric values for resampled index matrices f (flats), e (LB episodes), n (normal flats)."""
    if name == "macro_f1":
        return bs.macro_f1(u.cc[f].sum(axis=1))
    if name == "anomaly_f1":
        return bs.binary_f1(u.bc[f].sum(axis=1))
    if name.startswith("f1_"):
        return bs.prf(u.cc[f].sum(axis=1))[2][:, bs.IDX[name[3:]]]
    if name == "lb_detect":
        return u.lb_det[e].mean(axis=1)
    if name == "lb_detect_deb":
        return u.lb_det_deb[e].mean(axis=1)
    if name == "ttd_med":
        return np.median(u.lb_ttd_c[e], axis=1)
    if name == "ttd_med_deb":
        return np.median(u.lb_ttd_c_deb[e], axis=1)
    if name == "fa_week":
        return u.fa[n].sum(axis=1) / u.weeks[n].sum(axis=1)
    if name == "fa_week_deb":
        return u.fa_deb[n].sum(axis=1) / u.weeks[n].sum(axis=1)
    if name == "fa_week_lb":
        return u.fa_lb[n].sum(axis=1) / u.weeks[n].sum(axis=1)
    if name == "fa_week_lb_deb":
        return u.fa_lb_deb[n].sum(axis=1) / u.weeks[n].sum(axis=1)
    raise KeyError(name)


def point(u: Units, name: str) -> float:
    f = np.arange(len(u.cc))[None, :]
    e = np.arange(len(u.lb_det))[None, :]
    n = np.arange(len(u.fa))[None, :]
    if (name.startswith("ttd") or name.startswith("lb_")) and len(u.lb_det) == 0:
        return float("nan")
    return float(metric_samples(u, name, f, e, n)[0])


@dataclass
class Boot:
    f: np.ndarray
    e: np.ndarray
    n: np.ndarray

    @classmethod
    def make(cls, u: Units, b: int, seed: int) -> Boot:
        rng = np.random.default_rng(seed)
        return cls(
            bs.boot_idx(len(u.cc), b, rng),
            bs.boot_idx(max(len(u.lb_det), 1), b, rng),
            bs.boot_idx(len(u.fa), b, rng),
        )

    def samples(self, u: Units, name: str) -> np.ndarray:
        if len(u.lb_det) == 0 and (name.startswith("ttd") or name.startswith("lb_")):
            return np.full(len(self.f), np.nan)
        return metric_samples(u, name, self.f, self.e, self.n)


def detected_ttd_quantiles(u: Units, deb: bool) -> dict:
    t = u.lb_ttd_deb if deb else u.lb_ttd
    d = t[~np.isnan(t)]
    q = (
        {"p25": 0.0, "median": 0.0, "p75": 0.0, "p90": 0.0}
        if len(d) == 0
        else dict(
            zip(("p25", "median", "p75", "p90"), np.percentile(d, [25, 50, 75, 90]), strict=True)
        )
    )
    return {
        "n_detected": int(len(d)),
        "n_episodes": int(len(t)),
        **{k: float(v) for k, v in q.items()},
    }


def policy_summary(u: Units, name: str, boot: Boot) -> dict:
    out: dict = {"metrics": {}, "per_class": {}, "by_fault": {}}
    for m, _, _ in METRICS:
        if m in CLASS_METRICS and name in NO_CLASS:
            out["metrics"][m] = None
            continue
        v = point(u, m)
        out["metrics"][m] = {"value": v, "ci": bs.pct_ci(boot.samples(u, m))}
    cc = u.cc.sum(axis=0)
    pr, rc, f1 = bs.prf(cc)
    for c in bs.CLASSES:
        i = bs.IDX[c]
        s = boot.samples(u, f"f1_{c}")
        out["per_class"][c] = {
            "precision": float(pr[i]),
            "recall": float(rc[i]),
            "f1": float(f1[i]),
            "f1_ci": bs.pct_ci(s),
            "support": int(cc[i, 0] + cc[i, 2]),
        }
    for ft in FAULT_ORDER:
        sel = u.ep_fault == ft
        k, n = int(u.ep_det[sel].sum()), int(sel.sum())
        out["by_fault"][ft] = {"detected": k, "episodes": n, "ci": bs.wilson(k, n)}
    out["ttd"] = detected_ttd_quantiles(u, False)
    out["ttd_deb"] = detected_ttd_quantiles(u, True)
    return out


def compare(
    units: dict[str, Units], contender: str, boot: Boot, vs: str | None = None
) -> list[dict]:
    """contender vs the best simple baseline per metric (or vs the fixed policy ``vs``):
    paired bootstrap, McNemar, Holm."""
    rows = []
    uc = units[contender]
    for m, lab, hi in METRICS:
        if m in CLASS_METRICS and contender in NO_CLASS:
            continue
        cands = [
            b for b in ([vs] if vs else BASELINES) if not (m in CLASS_METRICS and b in NO_CLASS)
        ]
        pts = {b: point(units[b], m) for b in cands}
        valid = {b: v for b, v in pts.items() if np.isfinite(v)}
        if not valid:
            continue
        best = (max if hi else min)(valid, key=valid.get)
        ub = units[best]
        d = boot.samples(uc, m) - boot.samples(ub, m)
        mc = None
        if m == "lb_detect" and len(uc.lb_det):
            mc = bs.mcnemar_exact(uc.lb_det > 0, ub.lb_det > 0)
        elif m == "lb_detect_deb" and len(uc.lb_det_deb):
            mc = bs.mcnemar_exact(uc.lb_det_deb > 0, ub.lb_det_deb > 0)
        elif m == "macro_f1":
            mc = bs.mcnemar_exact(uc.p == uc.y, ub.p == ub.y)
        elif m == "anomaly_f1":
            mc = bs.mcnemar_exact((uc.p != 0) == (uc.y != 0), (ub.p != 0) == (ub.y != 0))
        rows.append(
            {
                "metric": m,
                "label": lab,
                "higher_better": hi,
                "baseline": best,
                "model": point(uc, m),
                "base": valid[best],
                "diff": point(uc, m) - valid[best],
                "diff_ci": bs.pct_ci(d),
                "p": bs.boot_pvalue(d),
                "mcnemar": mc,
            }
        )
    adj = bs.holm([r["p"] for r in rows])
    for r, a in zip(rows, adj, strict=True):
        r["p_holm"] = a
        r["verdict"] = bs.verdict(a, r["diff"], r["higher_better"])
    return rows


def evaluate(models: Models, prep: Prepared, rules_fn, debounce: int, b: int, seed: int) -> dict:
    rt, rn = prep.rules(rules_fn)
    lt = predict_policies(models, prep.test, rt)
    ln = predict_policies(models, prep.normal, rn)
    units = {p: compute_units(prep, lt[p], ln[p], debounce) for p in POLICIES}
    boot = Boot.make(units["rules"], b, seed)
    # one fixed "best simple baseline": highest class-agnostic alert F1 among (a)-(d)
    best_overall = max(BASELINES, key=lambda k: point(units[k], "anomaly_f1"))
    return {
        "units": units,
        "boot": boot,
        "summary": {p: policy_summary(units[p], p, boot) for p in POLICIES},
        "compare": {c: compare(units, c, boot) for c in [*CONTENDERS, "hybrid_union"]},
        "compare_rules": {
            c: compare(units, c, boot, vs="rules") for c in [*CONTENDERS, "hybrid_union"]
        },
        "best_overall": best_overall,
        "compare_best": {
            c: compare(units, c, boot, vs=best_overall) for c in [*CONTENDERS, "hybrid_union"]
        },
    }


# ----------------------------------------------------------------------------- leak curve
def leak_curve(
    models: Models,
    cond: Cond,
    rules_fn,
    *,
    flats: int,
    days: int,
    seed: int,
    mags: list[float],
    hours: float,
    reps: int,
    stride: int,
    debounce: int,
) -> dict:
    """Controlled additive leaks (night 00:30 vs day 10:00, ``hours`` long) injected into
    fault-free flats BEFORE the sensor model; detection = any flagged window inside the episode,
    versus the identical window set of the leak-free control series (paired sensor noise)."""
    hs = make_households(flats, seed, cond.profile)
    sensor = resolve_sensor(cond.sensor, cond.yfs_min)
    split_day = int(days * TRAIN_FRAC)
    reps = max(1, min(reps, (days - split_day - 1) // 4))
    dur = int(hours * 60)
    n_win = len(np.arange(120, 120 + dur + 1, stride))
    timing_off = {"night": 30, "day": 600}
    blocks: list[tuple[float, str]] = []  # (magnitude, timing) per window block, in order
    frames = []

    def sensed(flow: np.ndarray, i: int) -> np.ndarray:
        if sensor is None:
            return flow.copy()
        srng = np.random.default_rng([seed, i, 7777])
        return apply_sensor_model(flow, srng, sensor, make_device(srng, sensor))

    for i, h in enumerate(hs):
        base, _ = true_base_flow(h, days, seed, i, cond.profile)
        ts_all = START + pd.to_timedelta(np.arange(len(base)), unit="min")
        control = sensed(base, i)
        sp = split_day * 1440
        baseline = fit_baseline(ts_all[:sp], control[:sp])
        spans = [
            (t, (split_day + off + 4 * r) * 1440 + timing_off[t])
            for t, off in (("night", 1), ("day", 3))
            for r in range(reps)
        ]
        for mag in [0.0, *mags]:
            if mag == 0.0:
                flow = control
            else:
                eps = [FaultEpisode("slow_leak", s, s + dur, mag) for _, s in spans]
                lrng = np.random.default_rng([seed, i, int(round(mag * 1000))])
                flow = sensed(apply_faults(base, eps, lrng)[0], i)
            for t, s in spans:
                a = s - 120
                feats = extract_features(
                    ts_all[a : s + dur + 1], flow[a : s + dur + 1], baseline, stride
                )
                assert len(feats) == n_win
                feats = feats.assign(mag=mag, timing=t, flat=i)
                frames.append(feats)
                blocks.append((mag, t))
    w = pd.concat(frames, ignore_index=True)
    rt = rules_labels(w, rules_fn)
    labs = predict_policies(models, w, rt)
    mag_a = np.array([b[0] for b in blocks])
    tim_a = np.array([b[1] for b in blocks])
    rows = []
    for pol in POLICIES:
        flag = (np.asarray(labs[pol]) != "normal").reshape(len(blocks), n_win)
        deb = flag.copy()
        for j in range(1, debounce):
            deb[:, j:] &= flag[:, :-j]
            deb[:, :j] = False
        for t in ("night", "day"):
            for mag in [0.0, *mags]:
                sel = (mag_a == mag) & (tim_a == t)
                k, kd, n = (
                    int(flag[sel].any(axis=1).sum()),
                    int(deb[sel].any(axis=1).sum()),
                    int(sel.sum()),
                )
                rows.append(
                    {
                        "policy": pol,
                        "timing": t,
                        "magnitude": mag,
                        "n": n,
                        "detect": k / n,
                        "detect_ci": bs.wilson(k, n),
                        "detect_deb": kd / n,
                        "detect_deb_ci": bs.wilson(kd, n),
                    }
                )
    return {
        "cond": cond.synthetic_label,
        "flats": flats,
        "reps": reps,
        "hours": hours,
        "rows": rows,
        "sensor_min_flow": None if sensor is None else sensor.min_flow_lpm,
    }


def min_detectable(rows: list[dict], pol: str, timing: str, key: str = "detect") -> dict:
    """Smallest tested magnitude whose EXCESS detection rate is >= 90% (and stays >= 90% for every
    larger tested magnitude); 50% crossing by linear interpolation.

    excess = (detect - control) / (1 - control): the fraction of leak episodes caught beyond what
    the policy flags anyway in the identical leak-free windows (so a policy that alarms all the
    time cannot look sensitive). ``None`` if never reached."""
    r = sorted(
        (x for x in rows if x["policy"] == pol and x["timing"] == timing and x["magnitude"] > 0),
        key=lambda x: x["magnitude"],
    )
    ctrl = next(
        (
            x[key]
            for x in rows
            if x["policy"] == pol and x["timing"] == timing and x["magnitude"] == 0.0
        ),
        float("nan"),
    )
    c = 0.0 if not np.isfinite(ctrl) else ctrl
    ex = [{"magnitude": x["magnitude"], "v": (x[key] - c) / (1 - c) if c < 1 else 0.0} for x in r]
    m90 = None
    for i in range(len(ex)):
        if all(x["v"] >= 0.9 for x in ex[i:]):
            m90 = ex[i]["magnitude"]
            break
    m50 = None
    for a, b in zip(ex[:-1], ex[1:], strict=True):
        if a["v"] < 0.5 <= b["v"]:
            m50 = a["magnitude"] + (0.5 - a["v"]) * (b["magnitude"] - a["magnitude"]) / (
                b["v"] - a["v"]
            )
            break
    if m50 is None and ex and ex[0]["v"] >= 0.5:
        m50 = ex[0]["magnitude"]
    return {"m90": m90, "m50": m50, "control_rate": ctrl}


# ----------------------------------------------------------------------------- report
def _f(v, d=3) -> str:
    return "n/a" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{d}f}"


def _ci(cell: dict | None, d=3) -> str:
    if cell is None:
        return "n/a"
    lo, hi = cell["ci"]
    return f"{_f(cell['value'], d)} [{_f(lo, d)}, {_f(hi, d)}]"


def _metric_table(summary: dict, metrics: list[str]) -> str:
    head = "| policy | " + " | ".join(LABEL[m] for m in metrics) + " |"
    sep = "|---|" + "---|" * len(metrics)
    rows = [
        f"| {POLICY_DESC[p]} | "
        + " | ".join(
            _ci(summary[p]["metrics"][m], 2 if m.startswith(("fa", "ttd")) else 3) for m in metrics
        )
        + " |"
        for p in POLICIES
    ]
    return "\n".join([head, sep, *rows])


def _class_table(summary: dict) -> str:
    rows = [
        "| policy | class | precision | recall | F1 [95% CI] | support (windows) |",
        "|---|---|---|---|---|---|",
    ]
    for p in POLICIES:
        if p in NO_CLASS:
            rows.append(f"| {POLICY_DESC[p]} | (no class output) | | | | |")
            continue
        for c in bs.CLASSES:
            v = summary[p]["per_class"][c]
            lo, hi = v["f1_ci"]
            rows.append(
                f"| {POLICY_DESC[p]} | {c} | {_f(v['precision'])} | {_f(v['recall'])} | "
                f"{_f(v['f1'])} [{_f(lo)}, {_f(hi)}] | {v['support']} |"
            )
    return "\n".join(rows)


def _ttd_table(summary: dict) -> str:
    rows = [
        "| policy | detected / episodes | p25 | median | p75 | p90 | detected / episodes (debounced) | median (debounced) | p90 (debounced) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for p in POLICIES:
        a, d = summary[p]["ttd"], summary[p]["ttd_deb"]
        rows.append(
            f"| {POLICY_DESC[p]} | {a['n_detected']}/{a['n_episodes']} | {a['p25']:.0f} | {a['median']:.0f} | "
            f"{a['p75']:.0f} | {a['p90']:.0f} | {d['n_detected']}/{d['n_episodes']} | {d['median']:.0f} | {d['p90']:.0f} |"
        )
    return "\n".join(rows)


def _fault_table(summary: dict) -> str:
    rows = ["| policy | " + " | ".join(FAULT_ORDER) + " |", "|---|" + "---|" * len(FAULT_ORDER)]
    for p in POLICIES:
        cells = []
        for ft in FAULT_ORDER:
            v = summary[p]["by_fault"][ft]
            cells.append(
                f"{v['detected']}/{v['episodes']} [{_f(v['ci'][0], 2)}, {_f(v['ci'][1], 2)}]"
            )
        rows.append(f"| {POLICY_DESC[p]} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _compare_table(rows: list[dict]) -> str:
    out = [
        "| metric | model | best simple baseline | diff (model - baseline) [95% CI] | p (boot) | p (Holm) | McNemar p | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        mc = "-" if r["mcnemar"] is None else f"{r['mcnemar']['p']:.3g}"
        d = 2 if r["metric"].startswith(("fa", "ttd")) else 3
        out.append(
            f"| {r['label']} | {_f(r['model'], d)} | {POLICY_DESC[r['baseline']]}: {_f(r['base'], d)} | "
            f"{_f(r['diff'], d)} [{_f(r['diff_ci'][0], d)}, {_f(r['diff_ci'][1], d)}] | "
            f"{r['p']:.3g} | {r['p_holm']:.3g} | {mc} | **{r['verdict']}** |"
        )
    return "\n".join(out)


def verdict_lines(res: dict, name: str, key: str = "compare") -> str:
    lines = []
    for c in [*CONTENDERS, "hybrid_union"]:
        rows = res[key][c]
        good = [r["label"] for r in rows if r["verdict"] == "better"]
        bad = [r["label"] for r in rows if r["verdict"] == "worse"]
        nd = [r["label"] for r in rows if r["verdict"] == "not demonstrated"]
        lines.append(
            f"- **{POLICY_DESC[c]}** on `{name}`: statistically better on "
            f"{len(good)}/{len(rows)} metrics ({'; '.join(good) or 'none'}); worse on "
            f"{len(bad)} ({'; '.join(bad) or 'none'}); **not demonstrated** on {len(nd)} "
            f"({'; '.join(nd) or 'none'})."
        )
    return "\n".join(lines)


def _curve_table(curve: dict, pols: list[str], key: str) -> str:
    rows = curve["rows"]
    mags = sorted({r["magnitude"] for r in rows if r["magnitude"] > 0})
    out = []
    for t in ("night", "day"):
        out.append(
            f"\n*{t} onset ({'00:30' if t == 'night' else '10:00'}), detection rate in the episode"
            f" ({'debounced' if key == 'detect_deb' else 'undebounced'}); control = same windows without leak*\n"
        )
        out.append("| policy | control | " + " | ".join(f"{m:g}" for m in mags) + " |")
        out.append("|---|---|" + "---|" * len(mags))
        for p in pols:
            by = {r["magnitude"]: r for r in rows if r["policy"] == p and r["timing"] == t}
            cells = " | ".join(f"{by[m][key]:.2f}" for m in mags)
            out.append(f"| {p} | {by[0.0][key]:.2f} | {cells} |")
    return "\n".join(out)


def _min_det_table(curves: dict[str, dict], pols: list[str]) -> str:
    out = [
        "| scenario | policy | night: 50% / 90% (L/min) | day: 50% / 90% (L/min) | control FA (night/day) |",
        "|---|---|---|---|---|",
    ]
    for sc, cv in curves.items():
        for p in pols:
            cells, ctrl = [], []
            for t in ("night", "day"):
                md = min_detectable(cv["rows"], p, t)
                cells.append(
                    f"{_f(md['m50'], 2) if md['m50'] is not None else '> max'} / {_f(md['m90'], 2) if md['m90'] is not None else 'never'}"
                )
                ctrl.append(_f(md["control_rate"], 2))
            out.append(f"| {sc} | {p} | {cells[0]} | {cells[1]} | {ctrl[0]} / {ctrl[1]} |")
    return "\n".join(out)


def write_report(path: Path, ctx: dict) -> None:
    a = ctx["args"]
    main_res = ctx["results"][COND_DEFAULT.name]
    both_res = ctx["results"].get(COND_BOTH.name)
    prov = ctx["rules_prov"]
    smoke = a["boot"] < 1000 or a["flats"] < 12
    md = [
        "# Benchmark: does the ML beat simple rules? (generated by `python -m ml.scripts.benchmark`)",
        "",
        "> **SYNTHETIC DATA ONLY.** Every number below comes from the simulator in "
        "`ml/scripts/generate_data.py` (and, where labelled, an *assumed* YF-S201 sensor model). No real "
        "household, sensor, plumbing fault or operator label was involved. Read the "
        "'What this does NOT show' section before quoting anything.",
    ]
    if smoke:
        md += ["", "> **SMOKE RUN** (small config / few resamples): numbers are NOT meaningful."]
    md += [
        "",
        f"- Run: {ctx['when']} | seed {a['seed']} | sklearn {sklearn.__version__} | {a['flats']} flats x "
        f"{a['days']} days | window 15 min, stride {a['stride']} min | bootstrap resamples {a['boot']} | "
        f"debounce = {a['debounce']} consecutive flagged windows",
        f"- Rule baseline loaded from `{Path(prov['path']).relative_to(REPO) if Path(prov['path']).is_relative_to(REPO) else prov['path']}` "
        f"(sha256 `{prov['sha256'][:16]}`), mode: {prov['mode']}. The `ml` package does not import the backend; only this dev "
        "script does, so the baseline is the code that ships.",
        f"- Time: {ctx['elapsed']:.0f} s. Reproduce: `python -m ml.scripts.benchmark --flats {a['flats']} --days {a['days']} --seed {a['seed']}`.",
        "",
        "## 1. Verdict (read this first)",
        "",
        "Rule: a model is called better than the best simple baseline on a metric only if the paired "
        "bootstrap difference is significant after Holm correction across the metrics (adjusted p < 0.05); "
        "otherwise the result is **not demonstrated**. The 'best simple baseline' is picked per metric "
        "among (a)-(d) using the TEST point estimate, which favours the baseline (conservative for ML).",
        "",
        "**Versus the best simple baseline, picked per metric** (an oracle: the best of (a)-(d) on each metric "
        "separately, e.g. `night_min_flow` rules win false alarms only by almost never alerting):",
        "",
        verdict_lines(main_res, COND_DEFAULT.name),
    ]
    if both_res:
        md += ["", verdict_lines(both_res, COND_BOTH.name)]
    md += [
        "",
        "**Versus the single best simple baseline overall** (highest alert F1 among (a)-(d): "
        f"{POLICY_DESC[main_res['best_overall']]} on `default`"
        + (f", {POLICY_DESC[both_res['best_overall']]} on `{COND_BOTH.name}`" if both_res else "")
        + "):",
        "",
        verdict_lines(main_res, COND_DEFAULT.name, "compare_best"),
    ]
    if both_res:
        md += ["", verdict_lines(both_res, COND_BOTH.name, "compare_best")]
    md += [
        "",
        "**Versus the production rules (a) specifically** (the operationally relevant comparison):",
        "",
        verdict_lines(main_res, COND_DEFAULT.name, "compare_rules"),
    ]
    if both_res:
        md += ["", verdict_lines(both_res, COND_BOTH.name, "compare_rules")]
    md += [
        "",
        "## 2. Methodology",
        "",
        f"- Data: `generate_dataset` (profile `default`, no sensor model), {a['flats']} flats x {a['days']} days, seed "
        f"{a['seed']}. Chronological split at {int(TRAIN_FRAC * 100)}% of days (same as `train_detector`). Per-flat "
        "baselines use train-period normal minutes only. Features: the shared `aquaguard_features` code.",
        f"- Train windows are subsampled to every {a['train_stride']} min (non-overlapping 15-min windows); test windows use "
        f"stride {a['stride']} min. Trained on the train period only: tree, night rules, IsolationForest "
        f"(contamination 0.02, fitted on train-normal windows), RandomForest (class_weight=balanced, "
        f"{'quick 60 trees' if a['quick'] else '200 trees, unlimited depth'}).",
        "- **Episode detection**: a leak/burst episode (slow leak, running toilet, drip, burst) that starts after the split "
        "is *detected* if any window ending inside it is flagged (ANY non-normal class). **Time-to-detect** = onset to the "
        "first flagged window end (quantised by the stride; excludes MQTT/escalation latency). Undetected episodes are "
        "censored at the episode duration for the median/CI. **Debounced** = k consecutive flagged windows all inside the "
        "episode (k=3 as asked; note the backend's own `detect_debounce_seconds` is a 30 s rate limit on running "
        "detection, not a k-window confirmation, so this is a stress setting).",
        f"- **False alarms per flat-week** are measured on a separate FAULT-FREE cohort ({a['normal_flats']} flats, seed+1000, "
        "same profile/sensor, models trained on the main cohort): an alarm = a maximal run of consecutive flagged windows.",
        f"- **Bootstrap**: {a['boot']} resamples; flats are resampled as clusters for window metrics (windows overlap and are "
        "autocorrelated), episodes for episode metrics, fault-free flats for false alarms. Differences use the SAME resample "
        "for both policies (paired). McNemar (exact) is reported for episode detection (paired per episode) and for "
        "window correctness; window-level McNemar ignores autocorrelation, so treat it as optimistic and rely on the "
        "cluster bootstrap.",
        "- `iso` has no class output: macro-F1 is n/a; its flags count as class 'unknown' (never correct for per-class scores).",
        "",
        "## 3. Results on the default generator (train and test: default)",
        "",
        _metric_table(main_res["summary"], [m for m, _, _ in METRICS]),
        "",
        "### Per-class precision / recall / F1",
        "",
        _class_table(main_res["summary"]),
        "",
        "### Time-to-detect among detected leak+burst episodes (minutes)",
        "",
        _ttd_table(main_res["summary"]),
        "",
        "### Detection rate by fault type (episodes, Wilson 95% CI)",
        "",
        _fault_table(main_res["summary"]),
        "",
        "### Model vs best simple baseline",
    ]
    for c in CONTENDERS:
        md += [
            "",
            f"**{POLICY_DESC[c]}** vs best simple baseline (per metric)",
            "",
            _compare_table(main_res["compare"][c]),
        ]
    md += ["", "### Model vs the single best simple baseline overall"]
    for c in CONTENDERS:
        md += [
            "",
            f"**{POLICY_DESC[c]}** vs {POLICY_DESC[main_res['best_overall']]}",
            "",
            _compare_table(main_res["compare_best"][c]),
        ]
    md += ["", "### Model vs the production rules (a)"]
    for c in CONTENDERS:
        md += ["", f"**{POLICY_DESC[c]}** vs (a)", "", _compare_table(main_res["compare_rules"][c])]
    md += ["", "## 4. Distribution shift and sensor model (STILL SYNTHETIC)", ""]
    md += [
        "Conditions: `default` = original generator; `shifted` = `--profile shifted` (different schedules, burstier draws, "
        "4x nocturnal use, noisier days, different fault ranges); `+yfs201` = `--sensor-model yfs201` (assumed YF-S201: "
        "stall threshold ~1 L/min, 450 pulses/L, +-10% per-device calibration, drift, lost pulses, jitter, outages, stuck "
        "sensor; see section 5 for every assumption). Each cell is a model TRAINED on the row condition and TESTED on the "
        "column condition (baselines, thresholds, tree and night rules are all re-fitted per training condition; "
        "per-flat baselines are always built from the test condition's own train period, as at onboarding).",
        "",
    ]
    matrix = ctx["matrix"]
    names = [c.name for c in ALL_CONDS if c.name in matrix]
    for metric in ("macro_f1", "lb_detect", "fa_week"):
        md += [
            f"**{LABEL[metric]}** (point [95% CI]); rows: train condition, columns: test condition",
            "",
        ]
        md += [
            "| policy | train | " + " | ".join(f"test: {n}" for n in names) + " |",
            "|---|---|" + "---|" * len(names),
        ]
        for p in ["rules", "tree_min_flow", "night_rules", "rf", "ml_served", "hybrid_gate"]:
            for tr in names[:1] if p == "rules" else names:
                cells = []
                for te in names:
                    cell = matrix[tr][te]["summary"][p]["metrics"][metric]
                    cells.append(_ci(cell, 2 if metric.startswith("fa") else 3))
                trl = "(no training)" if p == "rules" else tr
                md.append(f"| {POLICY_DESC[p]} | {trl} | " + " | ".join(cells) + " |")
        md.append("")
    deg = ctx["degradation"]
    md += [
        "**Degradation, default -> shifted+yfs201** (trained on default, tested on shifted+yfs201 vs in-distribution), and the reverse:",
        "",
    ]
    md += [
        "| policy | metric | in-dist (default) | default -> shifted+yfs201 | shifted+yfs201 in-dist | shifted+yfs201 -> default |",
        "|---|---|---|---|---|---|",
    ]
    for r in deg:
        md.append(
            f"| {POLICY_DESC[r['policy']]} | {LABEL[r['metric']]} | {r['a_a']} | {r['a_b']} | {r['b_b']} | {r['b_a']} |"
        )
    if both_res:
        md += [
            "",
            f"### Model vs best simple baseline on `{COND_BOTH.name}` (in-distribution for that condition)",
            "",
        ]
        for c in [*CONTENDERS, "hybrid_union"]:
            md += [
                f"**{POLICY_DESC[c]}** vs best simple baseline",
                "",
                _compare_table(both_res["compare"][c]),
                "",
            ]
        md += [
            f"Detection rate by fault type on `{COND_BOTH.name}` (episodes, Wilson 95% CI):",
            "",
            _fault_table(both_res["summary"]),
            "",
        ]
    sens_res = ctx["results"].get(COND_SENSOR.name)
    if sens_res:
        md += [
            f"Detection rate by fault type on `{COND_SENSOR.name}` (sensor effect only, same households and "
            "episodes as `default`):",
            "",
            _fault_table(sens_res["summary"]),
            "",
            f"Headline metrics on `{COND_SENSOR.name}`:",
            "",
            _metric_table(sens_res["summary"], [m for m, _, _ in METRICS]),
            "",
        ]
    md += ["## 5. Minimum detectable leak with the assumed YF-S201 (key hardware finding)", ""]
    md += [
        "**Assumptions (not measured here).** (1) The YF-S201 operating range is commonly published as 1-30 L/min; we assume a "
        "start-up/stall threshold of 1.0 L/min nominal (+-15% per device) below which NO pulses are emitted. This is "
        "taken from typical datasheet ranges, not from a bench test, and the threshold is applied to the per-minute mean flow "
        "(a per-minute simulator cannot resolve sub-minute draw patterns). (2) 7.5 Hz per L/min = 450 pulses/L: one pulse "
        "resolves 1/450 L = 0.0022 L, i.e. 0.0022 L/min per pulse/min. **Quantisation is therefore negligible; it is the "
        "stall threshold that matters.** (3) +-10% per-device calibration error, 3%/30 d drift, 0.2% pulse loss, rare "
        "loss events, 1 s timing jitter, 0.05 outages/day (5-180 min), 0.03 stuck episodes/day (30-720 min, half read 0, half freeze). "
        "A digital pulse counter has no ADC; the timing jitter stands in for it.",
        "",
        "**Method.** Fault-free flats; an additive constant leak of the given magnitude is injected for "
        f"{ctx['curve_hours']:g} h starting at night (00:30) or in the day (10:00) BEFORE the sensor model, in "
        f"{ctx['curve_flats']} flats x {ctx['curve_reps']} repetitions per cell. Detection = any flagged window during the leak. The 'control' "
        "column is the flag rate on the identical windows without the leak (false-alarm floor; paired sensor noise). Wilson CIs are in "
        "`benchmark_results.json`. The summary table uses EXCESS detection = (detect - control) / (1 - control), so a policy that "
        "alarms all the time cannot look sensitive.",
        "",
    ]
    curves = ctx["curves"]
    md += [
        _min_det_table(curves, ["rules", "rf", "ml_served", "hybrid_gate", "iso"]),
        "",
        "Reading: cells are the smallest tested leak (L/min) at which the excess detection rate reaches 50% / 90% "
        "(and stays there for all larger magnitudes); 'never' = 90% not reached up to 3 L/min, '> max' = 50% not reached. Tested "
        "magnitudes: 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 1.5, 2, 3 L/min, so values are resolution-limited. The control column "
        "is the raw flag rate with NO leak: when it is high, raw detection rates in the curve tables below are mostly false alarms.",
        "",
    ]
    for sc, cv in curves.items():
        md += [
            f"### Curve: {sc}",
            "",
            f"Condition: {cv['cond']}; sensor min flow: {cv['sensor_min_flow']}",
            "",
        ]
        md += [_curve_table(cv, ["rules", "rf", "ml_served", "hybrid_gate"], "detect"), ""]
        md += [_curve_table(cv, ["rules", "ml_served", "hybrid_gate"], "detect_deb"), ""]
    md += [ctx["curve_finding"], ""]
    md += [
        "## 6. What this does NOT show",
        "",
        "- **No real households, sensors or plumbing.** Everything is simulated. Zero real flats were tested. Real water use has "
        "appliance cycles, pressure transients, tank refills, vacations, guests and sensor faults this generator does not contain.",
        "- **Generator circularity.** The default fault magnitudes/durations were written next to the rule thresholds "
        "(`night_min_flow >= 0.15`, `mean >= 10`...), and faults are additive constant floors on a world where normal nights are "
        "~0. Rules winning or tying there says the faults are easy, not that rules are the right model. The `shifted` profile "
        "weakens this (different ranges, more night use) but is written by the same author with the same functional form: it is "
        "a stress test, not independent evidence.",
        "- **Labels are the simulator's, not an operator's.** Window labels are the true fault state at the window end; real labels "
        "(plumber confirmed / false alarm) are noisy, delayed and biased toward alerts that were seen.",
        "- **Episode counts are small** (see the support columns): CIs describe resampling uncertainty over this synthetic cohort "
        "only, not uncertainty about the real world.",
        "- **The YF-S201 model is an assumption** (threshold, calibration, failure rates). The qualitative finding (sub-threshold leaks "
        "are invisible) is physics; the exact magnitude depends on the device. Measure on the bench.",
        "- **Not tested:** valve actuation, alert state machine and escalation latency, MQTT loss/reordering, timezone/DST, multi-"
        "occupancy changes, seasonal drift of baselines, adversarial inputs.",
        "- Multiple comparisons: the verdict uses Holm correction across the 8 metrics per model; the 'best baseline' selection is "
        "done on the test set (optimistic for baselines).",
        "",
        "## 7. Recommendation",
        "",
        ctx["recommendation"],
        "",
        "### Data needed before any stronger claim",
        "",
        "1. **Operator feedback as labels.** Log every alert with its type/confidence/features and the operator outcome: "
        "acknowledged, overridden/dismissed, escalated, valve closed, plumber confirmed (cause, location, start time). Treat "
        "'overridden' as a weak negative and 'plumber confirmed' as the gold label; keep unalerted-time as unlabeled, not normal.",
        "2. **Ground-truth episodes**: at least 30-50 independent confirmed real leak/burst episodes across >= 20 households before "
        "quoting a detection rate with a CI narrower than +-15 points; hundreds of flat-weeks of normal operation for the false-"
        "alarm rate.",
        "3. **Bench calibration of the sensor**: measured start-up threshold and pulse count at 0.1-2 L/min for the units actually "
        "deployed, per-device K-factor, so the 'minimum detectable leak' stops being an assumption; consider a more sensitive meter "
        "for slow-leak coverage, or a night-time pressure-decay / valve-closed test.",
        "4. **Shadow mode** for any model change: run the candidate beside the production policy, log both decisions, compare "
        "on the same real windows against operator outcomes, and only promote if non-inferior (see `ml/README.md`, "
        "`train_from_export.py`).",
        "5. Re-run this benchmark on the real export (the feature/windows code is shared) so the same CIs and paired tests apply.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(md))


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def _counts(rows: list[dict]) -> tuple[int, int, int]:
    v = [r["verdict"] for r in rows]
    return v.count("better"), v.count("worse"), v.count("not demonstrated")


def build_recommendation(results: dict, matrix: dict, curves: dict) -> str:
    """Evidence-driven text: every number is read from the results computed in this run."""
    D, B = COND_DEFAULT.name, COND_BOTH.name
    res = results[D]

    def val(tr: str, te: str, pol: str, metric: str) -> float:
        cell = matrix[tr][te]["summary"][pol]["metrics"][metric]
        return float("nan") if cell is None else cell["value"]

    def row(key: str, c: str, metric: str, r: dict = res) -> dict:
        return next(x for x in r[key][c] if x["metric"] == metric)

    bo = res["best_overall"]
    nb, nw, nn = _counts(res["compare_rules"]["rf"])
    bb, bw, bn = _counts(res["compare_best"]["rf"])
    lb = row("compare_best", "rf", "lb_detect")
    fa = row("compare_best", "rf", "fa_week")
    lines = [
        "Evidence on the **synthetic** benchmark (nothing here is a field result):",
        "",
        f"- **Full RandomForest vs the production rules** (default generator): significantly better on {nb} of "
        f"{nb + nw + nn} metrics, worse on {nw}; macro-F1 {val(D, D, 'rf', 'macro_f1'):.3f} vs "
        f"{val(D, D, 'rules', 'macro_f1'):.3f}, false alarms/flat-week {val(D, D, 'rf', 'fa_week'):.2f} vs "
        f"{val(D, D, 'rules', 'fa_week'):.2f}, leak+burst episode detection {val(D, D, 'rf', 'lb_detect'):.3f} vs "
        f"{val(D, D, 'rules', 'lb_detect'):.3f}.",
        f"- **Full RandomForest vs the single best simple baseline ({POLICY_DESC[bo]})**: better on {bb}, worse on {bw}, "
        f"**not demonstrated** on {bn} of {bb + bw + bn} metrics. Leak+burst episode detection: {lb['model']:.3f} vs "
        f"{lb['base']:.3f} (diff CI [{lb['diff_ci'][0]:.3f}, {lb['diff_ci'][1]:.3f}], verdict **{lb['verdict']}**); "
        f"false alarms/flat-week {fa['model']:.2f} vs {fa['base']:.2f} (**{fa['verdict']}**). The ML advantage is in "
        "class typing, mostly `overuse` (a class a min_flow rule cannot express), not in finding leaks and bursts.",
    ]
    if B in matrix:
        lines += [
            f"- **Distribution shift + assumed YF-S201** (trained on default, tested on `{B}`): leak+burst detection "
            f"rules {val(D, B, 'rules', 'lb_detect'):.3f}, RF {val(D, B, 'rf', 'lb_detect'):.3f}, hybrid gate "
            f"{val(D, B, 'hybrid_gate', 'lb_detect'):.3f}; false alarms/flat-week rules "
            f"{val(D, B, 'rules', 'fa_week'):.1f}, RF {val(D, B, 'rf', 'fa_week'):.1f}, hybrid gate "
            f"{val(D, B, 'hybrid_gate', 'fa_week'):.1f} (in-distribution: rules {val(D, D, 'rules', 'fa_week'):.1f}, "
            f"RF {val(D, D, 'rf', 'fa_week'):.1f}, hybrid gate {val(D, D, 'hybrid_gate', 'fa_week'):.1f}). "
            "See the shift tables for which policies degrade and by how much.",
            f"- **Retraining on sensor-model data without calibrating the operating point floods alarms**: RF trained on "
            f"`{COND_SENSOR.name}` gives {val(COND_SENSOR.name, COND_SENSOR.name, 'rf', 'fa_week'):.1f} false "
            "alarms/flat-week (undebounced). Hypothesis (not tested here): sensor-invisible leaks are still labelled 'leak', "
            "so a class-balanced classifier learns to call normal-looking windows leaks. A probability threshold tuned to an "
            "explicit false-alarm budget on real data is required before any ML-only alerting.",
        ]
    hyb_fa = val(D, D, "hybrid_gate", "fa_week")
    lines += [
        "",
        "### Recommended default policy",
        "",
        "**HYBRID: rules gate + ML class/confidence (`hybrid_gate`), with the rules classifier always active as the fail-safe.** "
        f"On the default generator it keeps the rules' leak+burst episode detection ({val(D, D, 'hybrid_gate', 'lb_detect'):.3f} vs "
        f"{val(D, D, 'rules', 'lb_detect'):.3f}) while cutting false alarms/flat-week from {val(D, D, 'rules', 'fa_week'):.2f} to "
        f"{hyb_fa:.2f} (it can only suppress or re-type a rule alert, never add one, so by construction it raises no more alarms "
        "than the shipped rules). It needs no model artefact to work, every alert names a threshold, and the model only removes "
        "or re-types alerts.",
        "",
        "Why not the alternatives, on this evidence:",
        "",
        f"- *Rules alone*: simple and robust, but {val(D, D, 'rules', 'fa_week'):.1f} false alarms/flat-week "
        f"({val(D, D, 'rules', 'fa_week_deb'):.1f} debounced) in the default world and no typing of `overuse` "
        f"(F1 {res['summary']['rules']['per_class']['overuse']['f1']:.2f}); keep as the fail-safe, not the target.",
        f"- *Full model alone (RF / RF + IsolationForest)*: macro-F1 {val(D, D, 'rf', 'macro_f1'):.3f} in the default world, "
        f"but false alarms/flat-week {val(D, D, 'rf', 'fa_week'):.1f} (RF) / {val(D, D, 'ml_served', 'fa_week'):.1f} "
        "(RF + IsolationForest fallback, as served)"
        + (
            f" growing to {val(D, B, 'rf', 'fa_week'):.1f} / {val(D, B, 'ml_served', 'fa_week'):.1f} under shift + sensor"
            if B in matrix
            else ""
        )
        + "; 'ML beats rules' holds against the rules in the synthetic default world and is **not demonstrated** against a "
        "one-feature tree for leak/burst detection. Run it in **shadow / log-only** until real feedback exists.",
        f"- *IsolationForest alone*: not an alert source ({val(D, D, 'iso', 'fa_week'):.1f} false alarms/flat-week, no class).",
        f"- *Hybrid union (rules OR ML)*: leak+burst detection {val(D, D, 'hybrid_union', 'lb_detect'):.3f}"
        + (
            f" ({val(D, B, 'hybrid_union', 'lb_detect'):.3f} under shift + sensor)"
            if B in matrix
            else ""
        )
        + f" but {val(D, D, 'hybrid_union', 'fa_week'):.1f} false alarms/flat-week.",
        "",
        "Known weakness of the recommendation: under shift + the assumed sensor the gate inherits the rules' blind spots and "
        "may veto some of what is left (see the degradation table); that is a reason to collect real data, not a reason to "
        "prefer the ungated model without an operating-point calibration.",
    ]
    sc = next((k for k in curves if "NOT sensor-aware" in k), None)
    si = next((k for k in curves if k.startswith("ideal")), None)
    if sc and si:
        md = min_detectable(curves[sc]["rows"], "rules", "night")
        mh = min_detectable(curves[sc]["rows"], "hybrid_gate", "night")
        mi = min_detectable(curves[si]["rows"], "hybrid_gate", "night")

        def fmt(v):
            return f"{v:g} L/min" if v is not None else "beyond 3 L/min"

        lines += [
            "",
            "**Hardware (independent of policy):** with the assumed 1.0 L/min start-up threshold the 90%-excess night "
            f"detection point is {fmt(md['m90'])} for the rules and {fmt(mh['m90'])} for the hybrid, versus "
            f"{fmt(mi['m90'])} (the smallest tested magnitude) for the same hybrid with an ideal sensor on this synthetic world: "
            "the sensor, not the algorithm, sets the limit. Drips and most running toilets are invisible to this sensor "
            "whatever the software; state that limit in product claims, bench-test the real threshold, and consider a "
            "more sensitive meter or a night-time pressure-decay / valve-closed test for slow leaks.",
        ]
    lines += [
        "",
        "Keep README claims at: 'rules + ML pipeline validated on synthetic data only; ML advantage over rules not demonstrated "
        "for leak/burst detection; sub-1 L/min leaks are undetectable with an assumed YF-S201'. **Config wiring is left to the "
        "maintainers; no backend setting was changed.**",
    ]
    return "\n".join(lines)


def build_curve_finding(curves: dict) -> str:
    out = ["**Finding.**"]
    for sc, cv in curves.items():
        best = []
        for p in ("rules", "ml_served", "hybrid_gate"):
            for t in ("night", "day"):
                md = min_detectable(cv["rows"], p, t)
                best.append(
                    f"{p}/{t}: 90% at "
                    + (f"{md['m90']:g} L/min" if md["m90"] is not None else "never (tested range)")
                )
        out.append(f"- *{sc}*: " + "; ".join(best) + ".")
    out.append(
        "- Physical reading: with a ~1 L/min stall threshold the sensor reports exactly 0 for a standalone leak below it, so "
        "dripping taps (0.05-0.2 L/min), most running toilets (0.2-0.55 L/min) and slow leaks under ~1 L/min are invisible at "
        "night when nothing else is flowing; by day they are only visible through the other draws they ride on, which a "
        "min_flow/night_min_flow feature cannot see. Compare the 'ideal sensor' scenario to see how much detectability the sensor "
        "(not the algorithm) removes."
    )
    return "\n".join(out)


# ----------------------------------------------------------------------------- main
def run(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--flats", type=int, default=40)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--train-stride", type=int, default=15)
    ap.add_argument(
        "--normal-flats", type=int, default=None, help="fault-free flats (default: --flats)"
    )
    ap.add_argument(
        "--boot", type=int, default=2000, help="bootstrap resamples (>=1000 for real runs)"
    )
    ap.add_argument("--debounce", type=int, default=3)
    ap.add_argument("--quick", action="store_true", help="smaller RandomForest")
    ap.add_argument("--skip-shift", action="store_true", help="only the default condition")
    ap.add_argument("--skip-curve", action="store_true")
    ap.add_argument("--curve-flats", type=int, default=24)
    ap.add_argument("--curve-reps", type=int, default=3)
    ap.add_argument("--curve-hours", type=float, default=4.0)
    ap.add_argument("--rules-path", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=ML_DIR / "reports")
    a = ap.parse_args(argv)
    a.normal_flats = a.normal_flats or a.flats
    t0 = time.time()
    rules_fn, prov = load_production_rules(a.rules_path)
    mags = [0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    conds = [COND_DEFAULT] if a.skip_shift else ALL_CONDS
    prep: dict[str, Prepared] = {}
    models: dict[str, Models] = {}
    for c in conds:
        print(f"[bench] preparing {c.name} ({c.synthetic_label})", flush=True)
        prep[c.name] = prepare_condition(
            c, a.flats, a.days, a.seed, a.stride, a.normal_flats, a.train_stride
        )
        print(
            f"[bench] fitting models on {c.name}: {len(prep[c.name].train)} train windows",
            flush=True,
        )
        models[c.name] = fit_models(prep[c.name].train, a.seed, a.quick)
    matrix: dict[str, dict[str, dict]] = {}
    for tr in conds:
        matrix[tr.name] = {}
        for te in conds:
            print(f"[bench] eval train={tr.name} test={te.name}", flush=True)
            matrix[tr.name][te.name] = evaluate(
                models[tr.name], prep[te.name], rules_fn, a.debounce, a.boot, a.seed + 3
            )
    results = {c.name: matrix[c.name][c.name] for c in conds}

    deg = []
    if COND_BOTH.name in matrix:
        A, B = COND_DEFAULT.name, COND_BOTH.name
        for p in ("rules", "tree_min_flow", "night_rules", "rf", "ml_served", "hybrid_gate"):
            for m in ("macro_f1", "lb_detect", "fa_week"):
                d = 2 if m.startswith("fa") else 3
                g = lambda tr, te, p=p, m=m, d=d: _ci(matrix[tr][te]["summary"][p]["metrics"][m], d)  # noqa: E731
                deg.append(
                    {
                        "policy": p,
                        "metric": m,
                        "a_a": g(A, A),
                        "a_b": g(A, B),
                        "b_b": g(B, B),
                        "b_a": g(B, A),
                    }
                )

    curves: dict[str, dict] = {}
    if not a.skip_curve:
        ck = dict(
            flats=a.curve_flats,
            days=a.days,
            seed=a.seed + 2000,
            mags=mags,
            hours=a.curve_hours,
            reps=a.curve_reps,
            stride=a.stride,
            debounce=a.debounce,
        )
        half = Cond("default+yfs201(min 0.5)", "default", "yfs201", 0.5)
        scen = [
            (
                "ideal sensor (no sensor model), models trained on default",
                COND_DEFAULT,
                models[COND_DEFAULT.name],
            ),
        ]
        if COND_SENSOR.name in models:
            scen += [
                (
                    "YF-S201 (1.0 L/min threshold), models trained on default (NOT sensor-aware)",
                    COND_SENSOR,
                    models[COND_DEFAULT.name],
                ),
                (
                    "YF-S201 (1.0 L/min threshold), models trained on YF-S201 data (sensor-aware)",
                    COND_SENSOR,
                    models[COND_SENSOR.name],
                ),
            ]
            hp = prepare_condition(half, a.flats, a.days, a.seed, a.stride, 2, a.train_stride)
            scen.append(
                (
                    "YF-S201 with a 0.5 L/min threshold (sensitivity), models trained on that sensor",
                    half,
                    fit_models(hp.train, a.seed, a.quick),
                )
            )
        for name, cond, mdl in scen:
            print(f"[bench] leak curve: {name}", flush=True)
            curves[name] = leak_curve(mdl, cond, rules_fn, **ck)

    ctx = {
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()},
        "when": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "rules_prov": prov,
        "results": results,
        "matrix": matrix,
        "degradation": deg,
        "curves": curves,
        "curve_flats": a.curve_flats,
        "curve_reps": a.curve_reps,
        "curve_hours": a.curve_hours,
        "elapsed": time.time() - t0,
        "recommendation": build_recommendation(results, matrix, curves),
        "curve_finding": build_curve_finding(curves) if curves else "(leak curve skipped)",
    }
    write_report(a.out_dir / "benchmark.md", ctx)
    dump = {
        "args": {k: v for k, v in ctx["args"].items() if k != "args"},
        "rules_provenance": prov,
        "results": {
            n: {
                "summary": r["summary"],
                "compare": r["compare"],
                "compare_rules": r["compare_rules"],
            }
            for n, r in results.items()
        },
        "matrix": {tr: {te: v["summary"] for te, v in row.items()} for tr, row in matrix.items()},
        "curves": curves,
    }
    (a.out_dir / "benchmark_results.json").write_text(json.dumps(_jsonable(dump), indent=1))
    print(f"[bench] wrote {a.out_dir / 'benchmark.md'} in {time.time() - t0:.0f}s")
    return ctx


if __name__ == "__main__":
    run()

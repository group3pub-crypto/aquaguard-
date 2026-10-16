"""Anomaly detection: feature extraction, ML classification with rule fallback, SHAP, severity."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Anomaly, Flat
from app.repositories import AnomalyRepository, BaselineRepository, ReadingRepository
from app.services.baseline import MAD_SCALE, local, slot_stats

log = structlog.get_logger(__name__)

FEATURE_NAMES = [
    "mean_flow", "max_flow", "min_flow", "std_flow", "baseline_deviation", "night_min_flow",
    "duration_above_base", "rate_of_change", "hour_of_day", "day_of_week", "is_night",
    "rolling_volume_1h", "pct_above_p95", "zero_flow_pct",
]
WINDOW = timedelta(minutes=15)
WINDOW_MIN = 15
MIN_READINGS = 3
MAX_GAP_S = 300.0
ZERO_FLOW = 0.01
TYPE_WEIGHT = {
    "burst": 1.0, "leak": 0.7, "running_toilet": 0.7, "dripping_tap": 0.5, "overuse": 0.3,
    "unknown": 0.5,
}
ML_TYPES = {"leak", "burst", "overuse"}
RULES_VERSION = "rules-v1"


# ----------------------------------------------------------------------------- features
def is_night_hour(hour: int) -> bool:
    return hour < 5 or hour >= 23


def _durations(ts: list[datetime]) -> list[float]:
    """Seconds each sample 'covers' (gap to next sample, capped; last = median gap)."""
    if not ts:
        return []
    gaps = [min((b - a).total_seconds(), MAX_GAP_S) for a, b in zip(ts, ts[1:], strict=False)]
    last = float(np.median(gaps)) if gaps else 60.0
    return [*gaps, last]


def extract_features(readings, baseline_slots: list[dict] | None, now: datetime) -> dict[str, float]:
    """14 model features from a <=1h reading history; stats use the trailing 15 minutes."""
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    pts = sorted(
        ((r.timestamp, float(r.flow_rate)) for r in readings if r.timestamp <= now),
        key=lambda p: p[0],
    )
    win = [p for p in pts if p[0] >= now - WINDOW]
    hour_pts = [p for p in pts if p[0] >= now - timedelta(hours=1)]
    d = local(now)
    f = dict.fromkeys(FEATURE_NAMES, 0.0)
    f.update(hour_of_day=float(d.hour), day_of_week=float(d.weekday()), is_night=float(is_night_hour(d.hour)))
    if not win:
        return f
    ts = [p[0] for p in win]
    flows = np.asarray([p[1] for p in win])
    dur = np.asarray(_durations(ts))
    stats = slot_stats(baseline_slots, now)
    f.update(
        mean_flow=float(flows.mean()), max_flow=float(flows.max()), min_flow=float(flows.min()),
        std_flow=float(flows.std()), zero_flow_pct=float((flows <= ZERO_FLOW).mean()),
    )
    night = [fl for t, fl in win if is_night_hour(local(t).hour)]
    f["night_min_flow"] = float(min(night)) if night else f["min_flow"]
    if stats:
        scale = max(MAD_SCALE * stats["mad"], 0.25)
        f["baseline_deviation"] = float(np.clip((f["mean_flow"] - stats["median"]) / scale, -100, 100))
        above = flows > stats["median"] + 3 * scale
        f["duration_above_base"] = float(dur[above].sum() / 60.0)
        f["pct_above_p95"] = float((flows > stats["p95"]).mean())
    span_min = (ts[-1] - ts[0]).total_seconds() / 60.0
    if len(win) >= 2 and span_min > 0:
        x = np.asarray([(t - ts[0]).total_seconds() / 60.0 for t in ts])
        f["rate_of_change"] = float(np.polyfit(x, flows, 1)[0])
    h_ts = [p[0] for p in hour_pts]
    f["rolling_volume_1h"] = float((np.asarray([p[1] for p in hour_pts]) * np.asarray(_durations(h_ts))).sum() / 60.0)
    return {k: float(v) for k, v in f.items()}


# ----------------------------------------------------------------------------- results
@dataclass
class Detection:
    anomaly_type: str
    confidence: float
    est_loss_lpm: float
    source: str  # "ml" | "rules"
    reasons: list[dict[str, Any]] = field(default_factory=list)
    shap_values: list[dict[str, Any]] | None = None
    class_probabilities: dict[str, float] | None = None
    iso_score: float | None = None


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def classify_anomaly_rules(features: dict[str, float], baseline_median: float = 0.0) -> Detection | None:
    """Deterministic fallback classifier. Returns None for normal behaviour."""
    g = features.get
    mean, mx, mn, std = g("mean_flow", 0), g("max_flow", 0), g("min_flow", 0), g("std_flow", 0)
    dev, zero, night_min = g("baseline_deviation", 0), g("zero_flow_pct", 0), g("night_min_flow", 0)
    night = bool(g("is_night", 0))
    if mx <= 0:
        return None
    continuous = zero <= 0.05

    def det(t: str, conf: float, loss: float, *reasons: tuple[str, str]) -> Detection:
        return Detection(
            t, round(_clip(conf), 3), round(max(loss, 0.0), 3), "rules",
            [{"feature": k, "value": round(float(g(k, 0)), 3), "note": n} for k, n in reasons],
        )

    if continuous and mx >= 15 and mean >= 10 and (dev >= 5 or dev == 0):
        return det("burst", 0.7 + 0.2 * min(mean / 30, 1) + 0.1 * min(abs(dev) / 10, 1), mean,
                   ("mean_flow", "sustained very high flow"), ("zero_flow_pct", "flow never stops"))
    if night and continuous and night_min >= 0.15:
        return det("leak", 0.6 + 0.35 * min(night_min / 1.0, 1), night_min,
                   ("night_min_flow", "flow does not drop to zero at night"))
    if not night and continuous and 0.15 <= mn <= 4 and std <= 0.35 * max(mean, 1e-9):
        return det("running_toilet", 0.55 + 0.3 * min(mn / 2.0, 1), mn,
                   ("min_flow", "steady low flow without pauses"), ("std_flow", "very low variability"))
    if 0.02 <= mean < 0.15 and mx < 0.6:
        return det("dripping_tap", 0.5 + 0.3 * min(mean / 0.15, 1), mean,
                   ("mean_flow", "persistent trickle"))
    if dev >= 4 and mean >= 2.0:
        return det("overuse", 0.5 + 0.4 * min((dev - 4) / 8, 1), mean - baseline_median,
                   ("baseline_deviation", "usage far above this hour's normal"))
    return None


def estimate_loss(anomaly_type: str, features: dict[str, float], baseline_median: float) -> float:
    g = features.get
    if anomaly_type == "burst":
        return g("mean_flow", 0.0)
    if anomaly_type in ("leak", "running_toilet", "dripping_tap"):
        return g("night_min_flow", 0.0) or g("min_flow", 0.0)
    return max(g("mean_flow", 0.0) - baseline_median, 0.0)


# ----------------------------------------------------------------------------- models
class ModelStore:
    """Lazily loads ml/models artefacts; safe for concurrent use and hot reload."""

    def __init__(self, model_dir: Path | str) -> None:
        self.model_dir = Path(model_dir)
        self.iso: Any = None
        self.clf: Any = None
        self.forecaster: Any = None
        self.feature_names: list[str] = list(FEATURE_NAMES)
        self.metadata: dict[str, dict] = {}
        self.version = RULES_VERSION
        self.forecaster_version = "baseline-median"
        self._loaded = False
        self._alock: asyncio.Lock | None = None
        self._tlock = threading.RLock()
        self._explainer: Any = None

    @property
    def ml_ready(self) -> bool:
        return self.iso is not None and self.clf is not None

    def load_sync(self) -> None:
        import joblib

        with self._tlock:
            def _load(name: str) -> Any:
                p = self.model_dir / f"{name}.joblib"
                if not p.is_file():
                    return None
                try:
                    return joblib.load(p)
                except Exception as exc:
                    log.warning("model_load_failed", file=str(p), error=str(exc))
                    return None

            self.iso, self.clf, self.forecaster = (
                _load("isolation_forest"), _load("classifier"), _load("forecaster"),
            )
            names = _load("feature_names")
            self.feature_names = list(names) if names else list(FEATURE_NAMES)
            self.metadata = {}
            for n in ("isolation_forest", "classifier", "forecaster"):
                mp = self.model_dir / f"{n}_metadata.json"
                try:
                    self.metadata[n] = json.loads(mp.read_text()) if mp.is_file() else {}
                except (OSError, ValueError):
                    self.metadata[n] = {}
            self._explainer = None
            self.version = self._compute_version()
            self.forecaster_version = self._forecaster_version()
            self._loaded = True
            log.info("models_loaded", version=self.version, ml_ready=self.ml_ready,
                     forecaster=self.forecaster is not None)

    def _compute_version(self) -> str:
        files = [self.model_dir / f"{n}.joblib" for n in ("isolation_forest", "classifier")]
        present = [p for p in files if p.is_file()]
        if not (self.ml_ready and present):
            return RULES_VERSION
        h = hashlib.sha256()
        for p in present:
            h.update(p.read_bytes())
        meta = self.metadata.get("classifier") or self.metadata.get("isolation_forest") or {}
        trained = meta.get("trained_at") or datetime.fromtimestamp(
            present[0].stat().st_mtime, UTC
        ).isoformat()
        return f"{trained}+{h.hexdigest()[:8]}"

    def _forecaster_version(self) -> str:
        p = self.model_dir / "forecaster.joblib"
        if self.forecaster is None or not p.is_file():
            return "baseline-median"
        trained = self.metadata.get("forecaster", {}).get("trained_at") or datetime.fromtimestamp(
            p.stat().st_mtime, UTC
        ).isoformat()
        return f"{trained}+{hashlib.sha256(p.read_bytes()).hexdigest()[:8]}"

    def _lock(self) -> asyncio.Lock:
        if self._alock is None:
            self._alock = asyncio.Lock()
        return self._alock

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock():
            if not self._loaded:
                await asyncio.to_thread(self.load_sync)

    async def reload(self) -> None:
        async with self._lock():
            await asyncio.to_thread(self.load_sync)

    def shap_explainer(self) -> Any:
        with self._tlock:
            if self._explainer is None:
                import shap  # optional dependency

                self._explainer = shap.TreeExplainer(self.clf)
            return self._explainer


def _shap_top3(store: ModelStore, x: np.ndarray, class_index: int) -> list[dict[str, Any]] | None:
    """Top-3 SHAP contributions for the predicted class, or None if SHAP is unavailable."""
    try:
        sv = store.shap_explainer().shap_values(x)
        arr = np.asarray(sv[class_index] if isinstance(sv, list) else sv)
        vals = arr[0, :, class_index] if arr.ndim == 3 else arr[0]
        top = np.argsort(-np.abs(vals))[:3]
        return [
            {"feature": store.feature_names[i], "value": float(x[0, i]), "shap": float(vals[i])}
            for i in top
        ]
    except Exception as exc:  # ImportError or any shap/shape problem: be honest, fall back
        log.info("shap_unavailable", reason=type(exc).__name__)
        return None


def _vectorize(features: dict[str, float], names: list[str]) -> np.ndarray | None:
    try:
        vec = [float(features[n]) for n in names]
    except (KeyError, TypeError, ValueError):
        return None
    arr = np.asarray(vec, dtype=float).reshape(1, -1)
    return arr if np.isfinite(arr).all() else None


async def classify_anomaly_ml(
    features: dict[str, float], store: ModelStore | None = None, *, baseline_median: float = 0.0
) -> Detection | None:
    """IsolationForest + RandomForest when available and the feature vector is valid;
    otherwise the rule classifier. Never raises on model problems."""
    store = store or ModelStore(Path("."))
    await store.ensure_loaded()
    rules = classify_anomaly_rules(features, baseline_median)
    if not store.ml_ready:
        return rules
    x = _vectorize(features, store.feature_names)
    if x is None:
        log.warning("invalid_feature_vector_fallback_to_rules")
        return rules
    try:
        clf = store.clf
        classes = [str(c) for c in clf.classes_]
        proba = np.asarray(clf.predict_proba(x))[0]
        ci = int(np.argmax(proba))
        label = classes[ci]
        iso_anom = int(store.iso.predict(x)[0]) == -1
        iso_score = float(-store.iso.score_samples(x)[0])
    except Exception as exc:
        log.warning("ml_inference_failed_fallback_to_rules", error=str(exc))
        return rules
    probs = {c: round(float(p), 4) for c, p in zip(classes, proba, strict=True)}
    if label == "normal":
        if not iso_anom:
            return None
        atype, conf = "unknown", round(0.4 + 0.2 * min(iso_score, 1.0), 3)
    else:
        atype, conf = (label if label in ML_TYPES else "unknown"), round(float(proba[ci]), 3)
        if atype == "leak" and rules and rules.anomaly_type in ("running_toilet", "dripping_tap"):
            atype = rules.anomaly_type  # ML sees "leak"; rules know the sub-type
    shap_vals = await asyncio.to_thread(_shap_top3, store, x, ci) if label != "normal" else None
    return Detection(
        atype, conf, round(max(estimate_loss(atype, features, baseline_median), 0.0), 3), "ml",
        rules.reasons if rules else [], shap_vals, probs, iso_score,
    )


def build_explanation(det: Detection, features: dict[str, float]) -> dict[str, Any]:
    """`generated_by` is 'shap_tree_explainer' only if SHAP values were really computed."""
    shap_ok = bool(det.shap_values)
    return {
        "generated_by": "shap_tree_explainer" if shap_ok else "rule_heuristic",
        "classifier": det.source,
        "top_factors": det.shap_values if shap_ok else det.reasons[:3],
        "class_probabilities": det.class_probabilities,
        "iso_score": det.iso_score,
    }


# ----------------------------------------------------------------------------- severity
def compute_severity(
    confidence: float, est_loss_lpm: float, duration_min: float, night: bool, anomaly_type: str
) -> float:
    score = (
        0.30 * _clip(confidence)
        + 0.25 * min(max(est_loss_lpm, 0.0) / 10, 1)
        + 0.20 * min(max(duration_min, 0.0) / 360, 1)
        + 0.15 * (1.0 if night else 0.5)
        + 0.10 * TYPE_WEIGHT.get(anomaly_type, 0.5)
    )
    return round(_clip(score), 4)


# ----------------------------------------------------------------------------- orchestration
@dataclass
class DetectionOutcome:
    anomaly: Anomaly | None
    created: bool
    resolved: list[Anomaly]
    features: dict[str, float]
    detection: Detection | None


async def run_detection(
    session: AsyncSession, store: ModelStore, flat: Flat, now: datetime | None = None
) -> DetectionOutcome | None:
    """Evaluate the trailing window for one flat; upsert/resolve anomaly rows."""
    now = now or datetime.now(UTC)
    readings = await ReadingRepository(session).window(flat.id, now - timedelta(hours=1), now)
    if sum(1 for r in readings if r.timestamp >= now - WINDOW) < MIN_READINGS:
        return None
    baseline = await BaselineRepository(session).active(flat.id)
    slots = baseline.slots if baseline else None
    features = extract_features(readings, slots, now)
    stats = slot_stats(slots, now)
    det = await classify_anomaly_ml(features, store, baseline_median=stats["median"] if stats else 0.0)
    repo = AnomalyRepository(session)
    if det is None:
        resolved = await repo.open_for_flat(flat.id)
        for a in resolved:
            a.resolved_at = now
        await repo.flush()
        return DetectionOutcome(None, False, resolved, features, None)

    existing = await repo.find_open(flat.id, det.anomaly_type)
    duration = WINDOW_MIN + ((now - existing.detected_at).total_seconds() / 60 if existing else 0)
    severity = compute_severity(
        det.confidence, det.est_loss_lpm, duration, bool(features["is_night"]), det.anomaly_type
    )
    fields = dict(
        confidence=det.confidence, severity=severity, features=features,
        shap_values=det.shap_values, explanation=build_explanation(det, features),
        baseline_version=baseline.version if baseline else None,
        window_start=now - WINDOW, window_end=now, est_loss_lpm=det.est_loss_lpm,
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        await repo.flush()
        return DetectionOutcome(existing, False, [], features, det)
    anomaly = await repo.add(Anomaly(flat_id=flat.id, detected_at=now, anomaly_type=det.anomaly_type, **fields))
    return DetectionOutcome(anomaly, True, [], features, det)



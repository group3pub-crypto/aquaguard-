"""Anomaly detection: feature extraction, ML classification with rule fallback, SHAP, severity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from aquaguard_features import (
    FEATURE_NAMES,
    FORECAST_FEATURES,
    HISTORY,
    ZERO_EPS,
    SlotBaseline,
    bin_to_minutes,
    extract_window_features_one,
    is_night_hour,
    slot_index,
)
from aquaguard_features import (
    WINDOW as WINDOW_MIN,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Anomaly, Flat
from app.repositories import AnomalyRepository, BaselineRepository, ReadingRepository
from app.services.baseline import local, slot_stats

log = structlog.get_logger(__name__)

WINDOW = timedelta(minutes=WINDOW_MIN)
MIN_READINGS = 3
ZERO_FLOW = ZERO_EPS
TYPE_WEIGHT = {
    "burst": 1.0,
    "leak": 0.7,
    "running_toilet": 0.7,
    "dripping_tap": 0.5,
    "overuse": 0.3,
    "unknown": 0.5,
}
ML_TYPES = {"leak", "burst", "overuse"}
RULES_VERSION = "rules-v1"
HISTORY_TD = timedelta(minutes=HISTORY)


# ----------------------------------------------------------------------------- features
def _epoch(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def extract_features(
    readings, baseline_slots: list[dict] | None, now: datetime
) -> dict[str, float]:
    """The 14 model features for the 15-minute window ending at ``now``.

    Thin adapter: readings -> per-minute grid (bins anchored at ``now``) -> the SAME shared code
    (``aquaguard_features``) that builds the training features. Needs up to 2 h of history
    (trailing minimum); shorter history is handled by the shared code.
    """
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    d = local(now)
    pts = [(_epoch(r.timestamp), float(r.flow_rate)) for r in readings if r.timestamp <= now]
    end = now.timestamp()
    recent = [p for p in pts if p[0] > end - WINDOW_MIN * 60]
    if not recent:
        f = dict.fromkeys(FEATURE_NAMES, 0.0)
        f.update(
            hour_of_day=float(d.hour),
            day_of_week=float(d.weekday()),
            is_night=float(is_night_hour(d.hour)),
        )
        return f
    hist = bin_to_minutes(
        np.asarray([p[0] for p in pts]), np.asarray([p[1] for p in pts]), end, HISTORY
    )
    bin_ends = (local(datetime.fromtimestamp(end - 60 * j, UTC)) for j in range(HISTORY - 1, -1, -1))
    slots = np.asarray([slot_index(ld.weekday(), ld.hour) for ld in bin_ends])
    return extract_window_features_one(
        hist, slots, d.hour, d.weekday(), SlotBaseline.from_slots(baseline_slots)
    )


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


def classify_anomaly_rules(
    features: dict[str, float], baseline_median: float = 0.0
) -> Detection | None:
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
            t,
            round(_clip(conf), 3),
            round(max(loss, 0.0), 3),
            "rules",
            [{"feature": k, "value": round(float(g(k, 0)), 3), "note": n} for k, n in reasons],
        )

    if continuous and mx >= 15 and mean >= 10 and (dev >= 5 or dev == 0):
        return det(
            "burst",
            0.7 + 0.2 * min(mean / 30, 1) + 0.1 * min(abs(dev) / 10, 1),
            mean,
            ("mean_flow", "sustained very high flow"),
            ("zero_flow_pct", "flow never stops"),
        )
    if night and continuous and night_min >= 0.15:
        return det(
            "leak",
            0.6 + 0.35 * min(night_min / 1.0, 1),
            night_min,
            ("night_min_flow", "flow does not drop to zero at night"),
        )
    if not night and continuous and 0.15 <= mn <= 4 and std <= 0.35 * max(mean, 1e-9):
        return det(
            "running_toilet",
            0.55 + 0.3 * min(mn / 2.0, 1),
            mn,
            ("min_flow", "steady low flow without pauses"),
            ("std_flow", "very low variability"),
        )
    if 0.02 <= mean < 0.15 and mx < 0.6:
        return det(
            "dripping_tap",
            0.5 + 0.3 * min(mean / 0.15, 1),
            mean,
            ("mean_flow", "persistent trickle"),
        )
    if dev >= 4 and mean >= 2.0:
        return det(
            "overuse",
            0.5 + 0.4 * min((dev - 4) / 8, 1),
            mean - baseline_median,
            ("baseline_deviation", "usage far above this hour's normal"),
        )
    return None


def estimate_loss(anomaly_type: str, features: dict[str, float], baseline_median: float) -> float:
    g = features.get
    if anomaly_type == "burst":
        return g("mean_flow", 0.0)
    if anomaly_type in ("leak", "running_toilet", "dripping_tap"):
        return g("night_min_flow", 0.0) or g("min_flow", 0.0)
    return max(g("mean_flow", 0.0) - baseline_median, 0.0)


# ----------------------------------------------------------------------------- models
MANIFEST_NAME = "manifest.json"
ARTIFACT_FILES = ("isolation_forest", "classifier", "feature_names", "forecaster")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_required_default() -> bool:
    """MODEL_MANIFEST_REQUIRED env var; default: required when ENVIRONMENT=production."""
    raw = os.environ.get("MODEL_MANIFEST_REQUIRED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("ENVIRONMENT", "").strip().lower() == "production"


def verify_manifest(model_dir: Path, filename: str) -> str:
    """Check ``filename`` against ``<model_dir>/manifest.json`` (written by training).

    Returns "ok", "no_manifest", "not_in_manifest" or "hash_mismatch". joblib files are pickles
    and therefore TRUSTED artifacts: the manifest only detects tampering/corruption after training,
    it does not make an untrusted pickle safe to load.
    """
    mp = model_dir / MANIFEST_NAME
    try:
        files = json.loads(mp.read_text())["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return "no_manifest"
    want = files.get(filename) if isinstance(files, dict) else None
    if not want:
        return "not_in_manifest"
    return "ok" if sha256_file(model_dir / filename) == want else "hash_mismatch"


def _major(version: str | None) -> str | None:
    return str(version).split(".")[0] if version else None


class ModelStore:
    """Lazily loads ml/models artefacts; safe for concurrent use and hot reload.

    ``model_status`` always says what happened (per artefact: ``ok`` / ``missing`` / a failure
    reason) so a silent fallback to rules or the baseline forecaster is never invisible.
    """

    def __init__(self, model_dir: Path | str, manifest_required: bool | None = None) -> None:
        self.model_dir = Path(model_dir)
        self.manifest_required = (
            manifest_required_default() if manifest_required is None else manifest_required
        )
        self.iso: Any = None
        self.clf: Any = None
        self.forecaster: Any = None
        self.feature_names: list[str] = list(FEATURE_NAMES)
        self.metadata: dict[str, dict] = {}
        self.version = RULES_VERSION
        self.forecaster_version = "baseline-median"
        self.model_status: dict[str, Any] = {"detector": "not_loaded", "forecaster": "not_loaded"}
        self._loaded = False
        self._alock: asyncio.Lock | None = None
        self._tlock = threading.RLock()
        self._explainer: Any = None

    @property
    def ml_ready(self) -> bool:
        return self.iso is not None and self.clf is not None

    def _read_json(self, name: str) -> dict:
        mp = self.model_dir / name
        try:
            return json.loads(mp.read_text()) if mp.is_file() else {}
        except (OSError, ValueError):
            return {}

    def _load_artifact(self, name: str, status: dict[str, str]) -> Any:
        import joblib

        fname = f"{name}.joblib"
        p = self.model_dir / fname
        if not p.is_file():
            status[name] = "missing"
            return None
        check = verify_manifest(self.model_dir, fname)
        if check != "ok":
            if self.manifest_required:
                log.error("model_rejected_manifest", file=str(p), manifest=check)
                status[name] = f"rejected:{check}"
                return None
            log.warning("model_manifest_unverified", file=str(p), manifest=check)
        try:
            obj = joblib.load(p)
        except Exception as exc:
            log.warning("model_load_failed", file=str(p), error=str(exc))
            status[name] = f"load_failed:{type(exc).__name__}"
            return None
        status[name] = "ok" if check == "ok" else f"ok_unverified:{check}"
        return obj

    def _sklearn_warning(self, meta: dict, what: str, warnings: list[str]) -> None:
        import sklearn

        trained = meta.get("sklearn_version")
        if trained and _major(trained) != _major(sklearn.__version__):
            msg = f"{what}: trained with sklearn {trained}, runtime {sklearn.__version__}"
            log.warning("sklearn_major_mismatch", detail=msg)
            warnings.append(msg)

    def load_sync(self) -> None:
        with self._tlock:
            status: dict[str, str] = {}
            warnings: list[str] = []
            iso = self._load_artifact("isolation_forest", status)
            clf = self._load_artifact("classifier", status)
            names = self._load_artifact("feature_names", status)
            forecaster = self._load_artifact("forecaster", status)
            det_meta = self._read_json("detector_metadata.json")
            fc_meta = self._read_json("forecaster_metadata.json")

            if iso is not None and clf is not None:
                problem = None
                if names is None or list(names) != FEATURE_NAMES:
                    problem = "feature_names.joblib does not equal the shared FEATURE_NAMES"
                elif det_meta.get("feature_names", FEATURE_NAMES) != FEATURE_NAMES:
                    problem = "detector_metadata feature_names differ from shared FEATURE_NAMES"
                elif not {str(c) for c in getattr(clf, "classes_", [])} <= {
                    "normal", "leak", "burst", "overuse"
                }:
                    problem = "classifier has unexpected classes"
                if problem:
                    log.error("detector_rejected", reason=problem)
                    status["detector"] = f"rejected:{problem}"
                    iso = clf = None
                else:
                    self._sklearn_warning(det_meta, "detector", warnings)
            if iso is None or clf is None:
                status.setdefault("detector", "unavailable:rules_fallback")
                if status.get("detector", "").startswith("unavailable"):
                    log.warning("detector_models_unavailable_using_rules", status=dict(status))
                iso = clf = None
            else:
                status["detector"] = "ok"

            if forecaster is not None:
                if fc_meta.get("feature_names", FORECAST_FEATURES) != FORECAST_FEATURES:
                    log.error("forecaster_rejected", reason="feature_names mismatch")
                    status["forecaster"] = "rejected:feature_names"
                    forecaster = None
                else:
                    self._sklearn_warning(fc_meta, "forecaster", warnings)
            if forecaster is None:
                reason = status.get("forecaster", "missing")
                status["forecaster"] = f"baseline_fallback:{reason}"
                log.warning("forecaster_unavailable_using_baseline", reason=reason)
            elif status["forecaster"].startswith("ok"):
                status["forecaster"] = status["forecaster"]

            self.iso, self.clf, self.forecaster = iso, clf, forecaster
            self.feature_names = list(FEATURE_NAMES)
            self.metadata = {"detector": det_meta, "forecaster": fc_meta}
            self._explainer = None
            self.model_status = {**status, "warnings": warnings}
            self.version = self._compute_version()
            self.forecaster_version = self._forecaster_version()
            self._loaded = True
            log.info(
                "models_loaded",
                version=self.version,
                ml_ready=self.ml_ready,
                forecaster=self.forecaster is not None,
                status=self.model_status,
            )

    def _version_for(self, files: list[Path], meta: dict) -> str:
        h = hashlib.sha256()
        for p in files:
            h.update(p.read_bytes())
        trained = (
            meta.get("trained_at")
            or datetime.fromtimestamp(files[0].stat().st_mtime, UTC).isoformat()
        )
        data = str(meta.get("data_hash") or "")[:8]
        return "+".join(x for x in (trained, data, h.hexdigest()[:8]) if x)

    def _compute_version(self) -> str:
        files = [self.model_dir / f"{n}.joblib" for n in ("isolation_forest", "classifier")]
        if not (self.ml_ready and all(p.is_file() for p in files)):
            return RULES_VERSION
        return self._version_for(files, self.metadata.get("detector", {}))

    def _forecaster_version(self) -> str:
        p = self.model_dir / "forecaster.joblib"
        if self.forecaster is None or not p.is_file():
            return "baseline-median"
        return self._version_for([p], self.metadata.get("forecaster", {}))

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
    def _infer() -> tuple[list[str], np.ndarray, bool, float]:
        clf = store.clf
        classes = [str(c) for c in clf.classes_]
        proba = np.asarray(clf.predict_proba(x))[0]
        iso_anom = int(store.iso.predict(x)[0]) == -1
        iso_score = float(-store.iso.score_samples(x)[0])
        return classes, proba, iso_anom, iso_score

    try:  # sklearn inference is CPU-bound (~50 ms): keep it off the event loop
        classes, proba, iso_anom, iso_score = await asyncio.to_thread(_infer)
        ci = int(np.argmax(proba))
        label = classes[ci]
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
        atype,
        conf,
        round(max(estimate_loss(atype, features, baseline_median), 0.0), 3),
        "ml",
        rules.reasons if rules else [],
        shap_vals,
        probs,
        iso_score,
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
    readings = await ReadingRepository(session).window(flat.id, now - HISTORY_TD, now)
    if sum(1 for r in readings if r.timestamp >= now - WINDOW) < MIN_READINGS:
        return None
    baseline = await BaselineRepository(session).active(flat.id)
    slots = baseline.slots if baseline else None
    features = extract_features(readings, slots, now)
    stats = slot_stats(slots, now)
    det = await classify_anomaly_ml(
        features, store, baseline_median=stats["median"] if stats else 0.0
    )
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
        confidence=det.confidence,
        severity=severity,
        features=features,
        shap_values=det.shap_values,
        explanation=build_explanation(det, features),
        baseline_version=baseline.version if baseline else None,
        window_start=now - WINDOW,
        window_end=now,
        est_loss_lpm=det.est_loss_lpm,
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        await repo.flush()
        return DetectionOutcome(existing, False, [], features, det)
    anomaly = await repo.add(
        Anomaly(flat_id=flat.id, detected_at=now, anomaly_type=det.anomaly_type, **fields)
    )
    return DetectionOutcome(anomaly, True, [], features, det)

"""The strictly structured FACTS object sent to the model (no usernames, PII or UUIDs)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

ANOMALY_TYPES = ("leak", "burst", "overuse", "running_toilet", "dripping_tap", "unknown")
SEVERITIES = ("low", "medium", "high", "critical")
# Fixed table: the model may only paraphrase the action chosen here, never invent one.
RECOMMENDED_ACTION: dict[str, dict[int, str]] = {
    "en": {
        1: "Take a quick look at taps, toilets and appliances when convenient.",
        2: "Check taps, toilets and appliances now.",
        3: "Check taps, toilets and appliances now; maintenance has been asked to follow up.",
        4: "Contact maintenance so they can inspect the flat before restoring water.",
    },
    "hi": {
        1: "सुविधा अनुसार नल, टॉयलेट और उपकरण देख लें।",
        2: "अभी नल, टॉयलेट और उपकरण जांच लें।",
        3: "अभी नल, टॉयलेट और उपकरण जांच लें; मेंटेनेंस को फॉलो-अप के लिए कहा गया है।",
        4: "पानी चालू करने से पहले निरीक्षण के लिए मेंटेनेंस से संपर्क करें।",
    },
}
_UNIT_OK = re.compile(r"[^A-Za-z0-9 \-_/]")
_NAME_OK = re.compile(r"[^A-Za-z0-9 \-_.]")


def sanitize(value: Any, *, max_len: int, pattern: re.Pattern[str] = _NAME_OK) -> str:
    """Prompt-injection hygiene for DB strings: NFKC, drop control chars/markup, cap length."""
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return " ".join(pattern.sub("", text).split())[:max_len]


def _num(v: Any, ndigits: int = 2) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or v != v or abs(v) == float("inf"):
        return None
    return round(float(v), ndigits)


def severity_label(score: float | None) -> str:
    s = score or 0.0
    return "critical" if s >= 0.85 else "high" if s >= 0.6 else "medium" if s >= 0.35 else "low"


@dataclass(frozen=True)
class Facts:
    unit: str
    anomaly_type: str
    tier: int
    severity: str
    confidence: float | None = None  # 0..1
    est_loss_lpm: float | None = None
    duration_min: float | None = None
    time_of_day: str | None = None  # "night" | "day"
    top_factors: tuple[dict[str, Any], ...] = ()
    recommended_action: str = ""
    valve_state_confirmed: bool = False  # True only if the shut-off was acknowledged by the valve
    language: str = "en"
    extra_numbers: tuple[float, ...] = field(default=(), compare=False)

    def to_json_obj(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra_numbers")
        d["top_factors"] = list(self.top_factors)
        return {k: v for k, v in d.items() if v is not None}

    def allowed_numbers(self) -> set[float]:
        """Every number the text may legitimately contain, including exact derivations."""
        nums: set[float] = {float(self.tier)}
        for tok in re.findall(r"\d+", self.unit):  # "A-101" -> 101
            nums.add(float(tok))
        if self.est_loss_lpm is not None:
            nums |= {self.est_loss_lpm, self.est_loss_lpm * 60}
        if self.confidence is not None:
            nums |= {self.confidence, self.confidence * 100}
        if self.duration_min is not None:
            nums |= {self.duration_min, self.duration_min / 60}
        for f in self.top_factors:
            for v in f.values():
                if isinstance(v, int | float) and not isinstance(v, bool):
                    nums.add(float(v))
        for n in re.findall(r"\d+(?:\.\d+)?", " ".join(RECOMMENDED_ACTION[self.language].values())):
            nums.add(float(n))
        return nums | set(self.extra_numbers)


def build_facts(ctx: dict[str, Any], language: str = "en") -> Facts:
    """Whitelist-build Facts from the explainer ctx. Unknown keys are dropped, never forwarded."""
    atype = ctx.get("anomaly_type")
    atype = atype if atype in ANOMALY_TYPES else "unknown"
    try:
        tier = min(max(int(ctx.get("tier", 1)), 1), 4)
    except (TypeError, ValueError):
        tier = 1
    lang = language if language in RECOMMENDED_ACTION else "en"
    factors: list[dict[str, Any]] = []
    for f in list(ctx.get("top_factors") or [])[:3]:
        if not isinstance(f, dict):
            f = {"feature": f}
        name = sanitize(f.get("feature") or f.get("name") or f.get("reason") or "", max_len=40)
        if not name:
            continue
        item: dict[str, Any] = {"factor": name}
        impact = _num(f.get("shap") if "shap" in f else f.get("impact", f.get("value")), 3)
        if impact is not None:
            item["impact"] = impact
        item["generated_by"] = (
            "shap_tree_explainer"
            if f.get("generated_by") == "shap_tree_explainer"
            else "rule_heuristic"
        )
        factors.append(item)
    tod = ctx.get("night")
    sev = ctx.get("severity")
    return Facts(
        unit=sanitize(ctx.get("unit", "?"), max_len=16, pattern=_UNIT_OK) or "?",
        anomaly_type=atype,
        tier=tier,
        severity=sev if sev in SEVERITIES else severity_label(_num(sev)),
        confidence=_num(ctx.get("confidence")),
        est_loss_lpm=_num(ctx.get("est_loss_lpm"), 1),
        duration_min=_num(ctx.get("duration_min"), 0),
        time_of_day=None if tod is None else ("night" if tod else "day"),
        top_factors=tuple(factors),
        recommended_action=RECOMMENDED_ACTION[lang][tier],
        valve_state_confirmed=ctx.get("valve_state_confirmed") is True,
        language=lang,
    )


def facts_from_models(
    flat: Any,
    anomaly: Any,
    alert: Any,
    *,
    night: bool | None = None,
    valve_state_confirmed: bool = False,
) -> dict[str, Any]:
    """Build the explainer ctx from ORM rows (duck-typed). Reads only whitelisted columns."""
    expl = getattr(anomaly, "explanation", None) or {}
    gen = expl.get("generated_by")
    factors = [
        {**(f if isinstance(f, dict) else {"feature": f}), "generated_by": gen}
        for f in (expl.get("top_factors") or [])[:3]
    ]
    ws, we = getattr(anomaly, "window_start", None), getattr(anomaly, "window_end", None)
    return {
        "unit": flat.unit_number,
        "anomaly_type": anomaly.anomaly_type,
        "tier": alert.tier,
        "severity": alert.severity_score,
        "confidence": anomaly.confidence,
        "est_loss_lpm": anomaly.est_loss_lpm,
        "duration_min": (we - ws).total_seconds() / 60 if ws and we else None,
        "night": night,
        "top_factors": factors,
        "valve_state_confirmed": valve_state_confirmed,
    }

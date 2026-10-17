"""Human-readable alert messages. Template-based by default; LLM backends plug in via Explainer."""

from __future__ import annotations

from typing import Any, Protocol

TYPE_TEXT = {
    "leak": "a continuous leak",
    "burst": "a possible pipe burst",
    "overuse": "unusually high water use",
    "running_toilet": "a running toilet",
    "dripping_tap": "a dripping tap",
    "unknown": "unusual water flow",
}
TIER_ACTION = {
    1: "Please take a quick look when convenient.",
    2: "Please check your taps, toilets and appliances now.",
    3: "A maintenance ticket has been raised for this issue.",
    4: "The water supply to your flat has been shut off automatically to prevent damage.",
}


class Explainer(Protocol):
    source: str

    def message(self, ctx: dict[str, Any]) -> str: ...


class TemplateExplainer:
    source = "template"

    def message(self, ctx: dict[str, Any]) -> str:
        what = TYPE_TEXT.get(ctx.get("anomaly_type", "unknown"), TYPE_TEXT["unknown"])
        loss = ctx.get("est_loss_lpm")
        parts = [f"Flat {ctx.get('unit', '?')}: we detected {what}"]
        if loss:
            parts[0] += f" (about {loss:.1f} L/min, roughly {loss * 60:.0f} L/hour)"
        parts[0] += "."
        parts.append(TIER_ACTION.get(int(ctx.get("tier", 1)), TIER_ACTION[1]))
        return " ".join(parts)

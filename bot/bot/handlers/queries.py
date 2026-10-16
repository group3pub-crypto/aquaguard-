"""Simple keyword intent matching routed through the tool registry."""

from __future__ import annotations

import re
from typing import Any

from bot.handlers.commands import run_tool

_RULES: list[tuple[re.Pattern[str], str, dict]] = [
    (re.compile(r"\b(leak|leaking|leaks)\b", re.I), "list_active_alerts", {}),
    (re.compile(r"\b(top|biggest|worst|highest)\b.*\b(loss|losses|wastage)\b", re.I), "top_losses", {}),
    (re.compile(r"\b(alert|alerts)\b", re.I), "list_active_alerts", {}),
    (re.compile(r"\b(report|summary)\b", re.I), "generate_report", {}),
    (re.compile(r"\b(usage|consumption|used)\b", re.I), "get_usage_summary", {}),
]


def match_intent(text: str) -> tuple[str, dict] | None:
    """Return (tool_name, args) or None. Order matters: first rule wins."""
    text = text[:300]
    # 'top losses' must beat the generic 'leak' rule
    for pat, tool, args in sorted(_RULES, key=lambda r: r[1] != "top_losses"):
        if pat.search(text):
            return tool, dict(args)
    return None


async def on_text(update: Any, context: Any) -> None:
    intent = match_intent(update.effective_message.text or "")
    if intent is None:
        from bot.deps import get_deps, reply

        await reply(update, get_deps(context), "Sorry, I did not understand. Try /start for help.")
        return
    await run_tool(update, context, *intent)

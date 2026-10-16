"""Simple keyword intent matching routed through the tool registry."""

from __future__ import annotations

import re
from typing import Any

from bot.handlers.commands import run_tool

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(top|biggest|worst|highest)\b.*\b(loss|losses|wastage)\b", re.I), "top_losses"),
    (re.compile(r"\b(leak|leaking|leaks)\b", re.I), "list_active_alerts"),
    (re.compile(r"\b(alert|alerts)\b", re.I), "list_active_alerts"),
    (re.compile(r"\b(report|summary)\b", re.I), "generate_report"),
    (re.compile(r"\b(usage|consumption|used)\b", re.I), "get_usage_summary"),
]

_UNIT = re.compile(r"\b([A-Za-z]{0,3}-?\d{2,4})\b")


def match_intent(text: str) -> tuple[str, dict] | None:
    """Return (tool_name, args) or None. First matching rule wins ('top losses' beats 'leak')."""
    text = text[:300]
    for pat, tool in _RULES:
        if pat.search(text):
            args: dict = {}
            if tool == "get_usage_summary":
                m = _UNIT.search(text)
                if m:  # residents may omit the unit: the bot uses their bound flat
                    args["unit"] = m.group(1).upper()
            return tool, args
    return None


async def on_text(update: Any, context: Any) -> None:
    intent = match_intent(update.effective_message.text or "")
    if intent is None:
        from bot.deps import get_deps, reply

        await reply(update, get_deps(context), "Sorry, I did not understand. Try /start for help.")
        return
    await run_tool(update, context, *intent)

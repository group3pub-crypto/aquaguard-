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
    from bot.deps import get_deps, reply

    deps = get_deps(context)
    text = update.effective_message.text or ""
    nlu = getattr(deps, "nlu", None)
    principal = deps.store.principal_of(update.effective_user.id) if nlu is not None else None
    if nlu is not None and principal is not None:
        from bot.nlu import NluUnavailable

        try:
            result = await nlu.handle(text, principal, update.effective_chat.id, deps.api)
        except NluUnavailable as exc:
            await reply(update, deps, f"Smart mode is unavailable ({exc.public}); using basic keyword matching.")
        else:
            await deliver(update, deps, result)
            return
    intent = match_intent(text)
    if intent is None:
        await reply(update, deps, "Sorry, I did not understand. Try /start for help.")
        return
    await run_tool(update, context, *intent)


async def deliver(update: Any, deps: Any, result: Any) -> None:
    """Send the model's answer, then one confirmation-button message per proposed action."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from bot.deps import reply

    uid, chat_id = update.effective_user.id, update.effective_chat.id
    if result.text:
        await reply(update, deps, result.text)
    for prop in result.proposals:
        token = deps.confirm.create(uid, chat_id, prop.confirm_payload())
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(prop.confirm_label, callback_data=f"{prop.callback_kind}:yes:{token}"),
                    InlineKeyboardButton("Cancel", callback_data=f"{prop.callback_kind}:no:{token}"),
                ]
            ]
        )
        sent = await reply(update, deps, prop.describe(), reply_markup=kb)
        mid = getattr(sent, "message_id", None)
        if isinstance(mid, int):
            deps.confirm.bind_message(token, mid)

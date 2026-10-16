"""Inline-button callbacks. Every action re-checks the clicker's role; callback data is untrusted."""

from __future__ import annotations

from typing import Any

from bot.deps import Deps, explain, get_deps

_ALERT_TOOLS = {"ack": "acknowledge_alert", "snooze": "snooze_alert", "resolve": "resolve_alert"}


async def on_callback(update: Any, context: Any) -> None:
    q = update.callback_query
    deps = get_deps(context)
    await q.answer()
    msg = q.message
    text = await dispatch(
        deps,
        q.from_user.id,
        (q.data or "").split(":"),
        chat_id=getattr(msg, "chat_id", None),
        message_id=getattr(msg, "message_id", None),
    )
    await q.edit_message_text(text)  # plain text: no parse mode, nothing to escape


async def dispatch(
    deps: Deps, uid: int, parts: list[str], chat_id: int | None = None, message_id: int | None = None
) -> str:
    principal = deps.store.principal_of(uid)
    kind = parts[0]
    try:
        if kind == "valve" and len(parts) == 3:
            payload = deps.confirm.consume(parts[2], uid, chat_id if chat_id is not None else 0, message_id)
            if payload is None:
                return "This confirmation is no longer valid."
            if parts[1] == "no":
                return "Cancelled."
            if parts[1] == "yes":
                text = await deps.registry.call("set_valve", payload, principal, deps.api)
                deps.audit.record("valve", principal.actor if principal else f"telegram:{uid}", **payload)
                return text
        elif kind == "alert" and len(parts) == 3 and parts[1] in _ALERT_TOOLS:
            args = {"alert_id": parts[2]}
            if parts[1] == "snooze":
                args["minutes"] = 60
            text = await deps.registry.call(_ALERT_TOOLS[parts[1]], args, principal, deps.api)
            deps.audit.record(f"alert_{parts[1]}", principal.actor if principal else f"telegram:{uid}", alert_id=parts[2])
            return text
    except Exception as exc:  # noqa: BLE001
        return explain(exc)
    return "Unknown action."

"""Inline-button callbacks. Every action re-checks the clicker's role."""

from __future__ import annotations

from typing import Any

from bot.deps import get_deps, send
from bot.tools.registry import PermissionDenied, ToolError


async def on_callback(update: Any, context: Any) -> None:
    q = update.callback_query
    deps = get_deps(context)
    uid = q.from_user.id
    role = deps.store.role_of(uid)
    parts = (q.data or "").split(":")
    await q.answer()
    text = await dispatch(deps, context, uid, role, parts)
    await q.edit_message_text(text)  # plain text: no parse mode, nothing to escape


async def dispatch(deps: Any, context: Any, uid: int, role: Any, parts: list[str]) -> str:
    kind = parts[0]
    try:
        if kind == "link" and len(parts) == 3 and parts[2].isdigit():
            target = int(parts[2])
            if parts[1] == "approve":
                req = deps.store.approve(uid, target)
                await send(context.bot, deps, req["chat_id"], "Your account link was approved.")
                return f"Approved {req['username']} ({target})."
            if parts[1] == "reject":
                deps.store.reject(uid, target)
                return f"Rejected {target}."
        elif kind == "valve":
            if parts[1] == "no":
                return "Cancelled."
            if parts[1] == "yes" and len(parts) == 4:
                return await deps.registry.call("set_valve", {"unit": parts[2], "action": parts[3]}, role, deps.api)
        elif kind == "alert" and len(parts) == 3:
            tool = {"ack": "acknowledge_alert", "snooze": "snooze_alert"}.get(parts[1])
            if tool:
                return await deps.registry.call(tool, {"alert_id": parts[2]}, role, deps.api)
    except (PermissionError, PermissionDenied):
        return "You are not allowed to do that."
    except KeyError:
        return "Request no longer pending."
    except ToolError as exc:
        return f"Invalid request: {exc}"
    except Exception:  # noqa: BLE001
        return "Backend is unavailable, try again shortly."
    return "Unknown action."

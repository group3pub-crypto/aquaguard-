from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.auth import Role, role_allows
from bot.deps import get_deps, reply, send
from bot.tools.registry import PermissionDenied, ToolError

HELP = (
    "AquaGuard bot\n"
    "/link <username> - request linking to your account\n"
    "/status - system status\n/alerts - active alerts\n/report - summary report\n"
    "/valve <unit> open|close - admin only\n"
    "Or ask: 'which flats are leaking', 'top losses', 'usage'."
)


async def run_tool(update: Any, context: Any, name: str, args: dict | None = None) -> None:
    deps = get_deps(context)
    role = deps.store.role_of(update.effective_user.id)
    if role is None:
        await reply(update, deps, "Not linked. Use /link <username> first.")
        return
    try:
        text = await deps.registry.call(name, args or {}, role, deps.api)
    except PermissionDenied:
        text = "You are not allowed to do that."
    except ToolError as exc:
        text = f"Invalid request: {exc}"
    except Exception:  # noqa: BLE001 - never leak backend details to chat
        text = "Backend is unavailable, try again shortly."
    await reply(update, deps, text)


async def start(update: Any, context: Any) -> None:
    await reply(update, get_deps(context), HELP)


async def link(update: Any, context: Any) -> None:
    deps = get_deps(context)
    uid = update.effective_user.id
    if deps.store.role_of(uid) is not None:
        await reply(update, deps, "Already linked.")
        return
    if not context.args or len(context.args[0]) > 64:
        await reply(update, deps, "Usage: /link <username>")
        return
    username = context.args[0]
    deps.store.request_link(uid, update.effective_chat.id, username)
    await reply(update, deps, "Request sent. An admin must approve it.")
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"link:approve:{uid}"),
                InlineKeyboardButton("Reject", callback_data=f"link:reject:{uid}"),
            ]
        ]
    )
    for admin in deps.store.admin_ids:
        await send(
            context.bot, deps, admin, f"Link request: telegram {uid} wants account '{username}'.", reply_markup=kb
        )


async def status(update: Any, context: Any) -> None:
    await run_tool(update, context, "list_active_alerts", {"state": "alert"})


async def alerts(update: Any, context: Any) -> None:
    await run_tool(update, context, "list_active_alerts")


async def report(update: Any, context: Any) -> None:
    await run_tool(update, context, "generate_report")


async def valve(update: Any, context: Any) -> None:
    """Never acts directly: asks for inline-button confirmation first."""
    deps = get_deps(context)
    role = deps.store.role_of(update.effective_user.id)
    if not role_allows(role, Role.ADMIN):
        await reply(update, deps, "You are not allowed to do that.")
        return
    args = list(context.args or [])
    try:
        deps.registry.validate(
            deps.registry.get("set_valve"),
            {"unit": args[0], "action": args[1]} if len(args) == 2 else {},
        )
    except ToolError:
        await reply(update, deps, "Usage: /valve <unit> open|close")
        return
    unit, action = args
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"Confirm {action}", callback_data=f"valve:yes:{unit}:{action}"),
                InlineKeyboardButton("Cancel", callback_data="valve:no"),
            ]
        ]
    )
    await reply(update, deps, f"Really {action} the valve of {unit}?", reply_markup=kb)

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.api_client import ApiError
from bot.auth import LinkError, Role, role_allows
from bot.deps import explain, get_deps, reply, send
from bot.tools.registry import ToolError

log = logging.getLogger("bot.commands")

HELP = (
    "AquaGuard bot\n"
    "/link <backend_username> - request access (an admin must approve and bind your flat)\n"
    "/status - your flat's alerts (staff: society status)\n"
    "/alerts - open alerts\n"
    "/usage [unit] [hours] - water usage (residents: own flat)\n"
    "Staff: /report, /losses [days]\n"
    "Admin: /valve <unit> open|close, /approve <request_id> <flat_unit|maintenance>, "
    "/reject <request_id>, /revoke <telegram_id>"
)


async def run_tool(update: Any, context: Any, name: str, args: dict | None = None) -> None:
    deps = get_deps(context)
    principal = deps.store.principal_of(update.effective_user.id)
    if principal is None:
        await reply(update, deps, "Not linked. Use /link <backend_username> first.")
        return
    try:
        text = await deps.registry.call(name, args or {}, principal, deps.api)
    except Exception as exc:  # noqa: BLE001 - explain() maps and never leaks backend details
        text = explain(exc)
    await reply(update, deps, text)


async def start(update: Any, context: Any) -> None:
    await reply(update, get_deps(context), HELP)


async def link(update: Any, context: Any) -> None:
    deps = get_deps(context)
    uid = update.effective_user.id
    if deps.store.principal_of(uid) is not None:
        await reply(update, deps, "Already linked.")
        return
    if not context.args:
        await reply(update, deps, "Usage: /link <backend_username>")
        return
    username = context.args[0]
    try:
        rid = deps.store.request_link(uid, update.effective_chat.id, username)
    except LinkError as exc:
        await reply(update, deps, f"Cannot create request: {exc}.")
        return
    deps.audit.record("link_requested", f"telegram:{uid}", username=username, request_id=rid)
    await reply(update, deps, "Request sent. An admin must approve it and bind your flat.")
    for admin in deps.store.admin_ids:
        try:
            await send(
                context.bot,
                deps,
                admin,
                f"Link request {rid}: telegram {uid} claims backend user '{username}' "
                "(not verified by the bot).\n"
                f"/approve {rid} <flat_unit>  or  /approve {rid} maintenance  or  /reject {rid}",
            )
        except Exception:  # noqa: BLE001
            log.warning("could not notify admin %s", admin, exc_info=True)


async def approve(update: Any, context: Any) -> None:
    deps = get_deps(context)
    uid = update.effective_user.id
    if uid not in deps.store.admin_ids:
        await reply(update, deps, "You are not allowed to do that.")
        return
    args = list(context.args or [])
    if len(args) != 2:
        await reply(update, deps, "Usage: /approve <request_id> <flat_unit|maintenance>")
        return
    rid, what = args
    try:
        if what.lower() == "maintenance":
            rec = deps.store.approve(uid, rid, role=Role.MAINTENANCE)
        else:
            flat = await deps.api.find_flat(what)
            rec = deps.store.approve(
                uid, rid, role=Role.RESIDENT, flat_id=str(flat["id"]), unit=str(flat["unit_number"])
            )
    except KeyError:
        await reply(update, deps, "No such pending request (it may have expired).")
        return
    except ValueError as exc:
        await reply(update, deps, f"Not approved: {exc}.")
        return
    except ApiError as exc:
        await reply(update, deps, explain(exc))
        return
    deps.audit.record(
        "link_approved", f"telegram:{uid}", target=rec["telegram_id"], username=rec["username"],
        role=rec["role"], unit=rec["unit"],
    )
    scope = f"flat {rec['unit']}" if rec["role"] == "resident" else "maintenance"
    await reply(update, deps, f"Approved {rec['username']} ({rec['telegram_id']}) as {scope}.")
    try:
        await send(context.bot, deps, rec["chat_id"], f"Your account link was approved ({scope}).")
    except Exception:  # noqa: BLE001
        log.warning("could not notify %s", rec["telegram_id"], exc_info=True)


async def reject(update: Any, context: Any) -> None:
    deps = get_deps(context)
    uid = update.effective_user.id
    if uid not in deps.store.admin_ids:
        await reply(update, deps, "You are not allowed to do that.")
        return
    if not context.args:
        await reply(update, deps, "Usage: /reject <request_id>")
        return
    req = deps.store.reject(uid, context.args[0])
    if req:
        deps.audit.record("link_rejected", f"telegram:{uid}", target=req["telegram_id"])
    await reply(update, deps, "Rejected." if req else "No such pending request.")


async def revoke(update: Any, context: Any) -> None:
    deps = get_deps(context)
    uid = update.effective_user.id
    if uid not in deps.store.admin_ids:
        await reply(update, deps, "You are not allowed to do that.")
        return
    arg = (context.args or [""])[0]
    if not arg.isdigit():
        await reply(update, deps, "Usage: /revoke <telegram_id>")
        return
    ok = deps.store.revoke(uid, int(arg))
    if ok:
        deps.audit.record("link_revoked", f"telegram:{uid}", target=int(arg))
    await reply(update, deps, "Revoked." if ok else "That user is not linked.")


async def status(update: Any, context: Any) -> None:
    deps = get_deps(context)
    role = deps.store.role_of(update.effective_user.id)
    await run_tool(update, context, "system_status" if role_allows(role, Role.MAINTENANCE) else "list_active_alerts")


async def alerts(update: Any, context: Any) -> None:
    await run_tool(update, context, "list_active_alerts")


async def usage(update: Any, context: Any) -> None:
    args: dict = {}
    for tok in context.args or []:
        if tok.isdigit():
            args["hours"] = int(tok)
        else:
            args["unit"] = tok
    await run_tool(update, context, "get_usage_summary", args)


async def losses(update: Any, context: Any) -> None:
    arg = (context.args or [""])[0]
    await run_tool(update, context, "top_losses", {"days": int(arg)} if arg.isdigit() else {})


async def report(update: Any, context: Any) -> None:
    await run_tool(update, context, "generate_report")


async def valve(update: Any, context: Any) -> None:
    """Never acts directly: asks for an inline confirmation bound to this chat/user/message."""
    deps = get_deps(context)
    uid = update.effective_user.id
    if not role_allows(deps.store.role_of(uid), Role.ADMIN):
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
    chat_id = update.effective_chat.id
    token = deps.confirm.create(uid, chat_id, {"unit": unit, "action": action})
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"Confirm {action}", callback_data=f"valve:yes:{token}"),
                InlineKeyboardButton("Cancel", callback_data=f"valve:no:{token}"),
            ]
        ]
    )
    sent = await reply(update, deps, f"Really {action} the valve of {unit}? (valid 60 s)", reply_markup=kb)
    mid = getattr(sent, "message_id", None)
    if isinstance(mid, int):
        deps.confirm.bind_message(token, mid)

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.api_client import ApiError
from bot.auth import USERNAME_RE, LinkError, Role, role_allows
from bot.deps import explain, get_deps, reply, send
from bot.tools.registry import ToolError

log = logging.getLogger("bot.commands")

HELP = (
    "AquaGuard bot\n"
    "/link <backend_username> - request access (checked against the backend; an admin must approve)\n"
    "/status - your flat's alerts (staff: society status)\n"
    "/alerts - open alerts\n"
    "/usage [unit] [hours] - water usage (residents: own flat)\n"
    "Staff: /report, /losses [days]\n"
    "Admin: /valve <unit> open|close, /approve <request_id>, "
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


_DENIED_LINK = "Cannot create request: that username could not be verified."


def _link_ineligible(user: dict, telegram_id: int) -> str | None:
    """Why a verified backend account may not be bound to a telegram user (None = eligible)."""
    if not user.get("is_active", True):
        return "account is inactive"
    if user.get("is_service"):
        return "service accounts cannot be linked"
    role = user.get("role")
    if role == "admin":
        return "admin accounts cannot be linked (admins come from TELEGRAM_ADMIN_IDS)"
    if role not in ("resident", "maintenance"):
        return f"unsupported role {role!r}"
    if role == "resident" and not user.get("flat_id"):
        return "resident account has no flat binding in the backend"
    claimed = user.get("telegram_id")
    if claimed is not None and claimed != telegram_id:
        return "account is already linked to another telegram user"
    return None


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
    if not USERNAME_RE.match(username):
        await reply(update, deps, "Cannot create request: invalid username.")
        return
    try:
        deps.store.check_rate(uid)  # bound username probing before any backend lookup
        user = await deps.api.user_by_username(username)
    except LinkError as exc:
        await reply(update, deps, f"Cannot create request: {exc}.")
        return
    except ApiError as exc:
        if exc.status == 404:
            deps.audit.record("link_rejected_unknown", f"telegram:{uid}", username=username)
            await reply(update, deps, _DENIED_LINK)
        else:
            await reply(update, deps, explain(exc))
        return
    why = _link_ineligible(user, uid)
    if why is not None:
        deps.audit.record("link_rejected_ineligible", f"telegram:{uid}", username=username, reason=why)
        await reply(update, deps, _DENIED_LINK)
        return
    backend = {k: user.get(k) for k in ("id", "role", "flat_id", "society_id")}
    try:
        rid = deps.store.request_link(uid, update.effective_chat.id, str(user["username"]), backend, rate_checked=True)
    except LinkError as exc:
        await reply(update, deps, f"Cannot create request: {exc}.")
        return
    unit = await deps.api.unit_label(user["flat_id"]) if user.get("flat_id") else None
    scope = f"flat {unit}" if unit else "maintenance (all flats)"
    deps.audit.record("link_requested", f"telegram:{uid}", username=username, request_id=rid, role=user["role"])
    await reply(update, deps, "Request sent. An admin must approve it.")
    for admin in deps.store.admin_ids:
        try:
            await send(
                context.bot,
                deps,
                admin,
                f"Link request {rid}: telegram {uid} asks to be linked to backend user "
                f"'{user['username']}' ({user['role']}, {scope}). The account exists, but the bot cannot "
                "verify that this telegram user owns it.\n"
                f"/approve {rid}  or  /reject {rid}",
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
    if len(args) != 1:
        await reply(update, deps, "Usage: /approve <request_id> (role and flat come from the backend account)")
        return
    rid = args[0]
    req = deps.store.get_pending(rid)
    if req is None:
        await reply(update, deps, "No such pending request (it may have expired).")
        return
    try:
        # re-read the account: it may have changed (role, flat, deactivated) since /link
        user = await deps.api.user_by_username(req["username"])
        why = _link_ineligible(user, req["telegram_id"])
        known = (req.get("backend") or {}).get("id")
        if why is None and known and str(user.get("id")) != str(known):
            why = "backend account changed since the request"
        if why is not None:
            await reply(update, deps, f"Not approved: {why}.")
            return
        role = Role.MAINTENANCE if user["role"] == "maintenance" else Role.RESIDENT
        flat_id = unit = None
        if role == Role.RESIDENT:
            flat_id = str(user["flat_id"])
            unit = await deps.api.unit_label(flat_id)
        rec = deps.store.approve(
            uid,
            rid,
            role=role,
            flat_id=flat_id,
            unit=unit,
            user_id=str(user["id"]),
            society_id=str(user["society_id"]) if user.get("society_id") else None,
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
        "link_approved",
        f"telegram:{uid}",
        target=rec["telegram_id"],
        username=rec["username"],
        role=rec["role"],
        unit=rec["unit"],
    )
    scope = f"flat {rec['unit']}" if rec["role"] == "resident" else "maintenance"
    note = ""
    try:  # best effort: needs an admin service account; the local mapping is already persisted
        await deps.api.set_telegram_id(rec["user_id"], rec["telegram_id"])
    except ApiError as exc:
        log.warning("could not set telegram_id on the backend user (status %s)", exc.status)
        note = " (could not record the telegram id on the backend account)"
    await reply(update, deps, f"Approved {rec['username']} ({rec['telegram_id']}) as {scope}.{note}")
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

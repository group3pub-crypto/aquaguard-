from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bot.api_client import ApiError
from bot.audit import AuditLog
from bot.auth import LinkStore
from bot.confirm import ConfirmStore
from bot.formatting import escape_md
from bot.ratelimit import RateLimiter
from bot.tools.registry import PermissionDenied, ToolError, ToolRegistry

log = logging.getLogger("bot")
MAX_TEXT = 3800


@dataclass
class Deps:
    api: Any
    registry: ToolRegistry
    store: LinkStore
    limiter: RateLimiter
    audit: AuditLog = field(default_factory=AuditLog)
    confirm: ConfirmStore = field(default_factory=ConfirmStore)


def get_deps(context: Any) -> Deps:
    return context.application.bot_data["deps"]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n..."


def explain(exc: Exception) -> str:
    """User-facing text for a failed tool call; never leaks backend details."""
    if isinstance(exc, PermissionDenied):
        return "You are not allowed to do that."
    if isinstance(exc, ToolError):
        return f"Invalid request: {exc}"
    if isinstance(exc, ApiError):
        if exc.status == 404:
            return "Not found (check the unit number or alert id)."
        if exc.status == 409:
            return "That is not possible in the alert's current state."
        if exc.status in (400, 422):
            return "The backend rejected the request."
        if exc.status in (401, 403):
            log.error("backend refused the bot service account (%s)", exc.status)
    log.error("tool call failed", exc_info=exc)
    return "Backend is unavailable, try again shortly."


async def send(bot: Any, deps: Deps, chat_id: int, text: str, **kw: Any) -> Any:
    """Rate-limited, MarkdownV2-escaped outbound message."""
    await deps.limiter.acquire()
    return await bot.send_message(chat_id=chat_id, text=escape_md(_clip(text)), parse_mode="MarkdownV2", **kw)


async def reply(update: Any, deps: Deps, text: str, **kw: Any) -> Any:
    await deps.limiter.acquire()
    return await update.effective_message.reply_text(escape_md(_clip(text)), parse_mode="MarkdownV2", **kw)

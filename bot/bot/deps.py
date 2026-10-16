from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.auth import LinkStore
from bot.formatting import escape_md
from bot.ratelimit import RateLimiter
from bot.tools.registry import ToolRegistry


@dataclass
class Deps:
    api: Any
    registry: ToolRegistry
    store: LinkStore
    limiter: RateLimiter


def get_deps(context: Any) -> Deps:
    return context.application.bot_data["deps"]


async def send(bot: Any, deps: Deps, chat_id: int, text: str, **kw: Any) -> None:
    """Rate-limited, MarkdownV2-escaped outbound message."""
    await deps.limiter.acquire()
    await bot.send_message(chat_id=chat_id, text=escape_md(text), parse_mode="MarkdownV2", **kw)


async def reply(update: Any, deps: Deps, text: str, **kw: Any) -> None:
    await deps.limiter.acquire()
    await update.effective_message.reply_text(escape_md(text), parse_mode="MarkdownV2", **kw)

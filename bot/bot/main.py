from __future__ import annotations

import asyncio
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.api_client import ApiClient
from bot.auth import LinkStore
from bot.config import Settings
from bot.deps import Deps
from bot.handlers import callbacks, commands, queries
from bot.poller import run_poller
from bot.ratelimit import RateLimiter
from bot.tools import build_default_registry


def build_app(settings: Settings) -> Application:
    api = ApiClient(
        settings.bot_api_base_url,
        settings.bot_api_username,
        settings.bot_api_password.get_secret_value(),
        timeout=settings.bot_api_timeout,
    )
    deps = Deps(
        api=api,
        registry=build_default_registry(),
        store=LinkStore(settings.admin_ids, settings.bot_state_file or None),
        limiter=RateLimiter(settings.bot_send_rate),
    )

    async def post_init(app: Application) -> None:
        app.bot_data["poller"] = asyncio.create_task(run_poller(app.bot, deps, settings.bot_poll_interval))

    async def post_shutdown(app: Application) -> None:
        app.bot_data["poller"].cancel()
        await api.aclose()

    app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["deps"] = deps
    for name, fn in [
        ("start", commands.start),
        ("help", commands.start),
        ("link", commands.link),
        ("status", commands.status),
        ("alerts", commands.alerts),
        ("report", commands.report),
        ("valve", commands.valve),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callbacks.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queries.on_text))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs full request URLs, which include the bot token: silence it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    build_app(Settings()).run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.api_client import ApiClient
from bot.audit import AuditLog
from bot.auth import LinkStore
from bot.config import Settings
from bot.confirm import ConfirmStore
from bot.deps import Deps
from bot.handlers import callbacks, commands, queries
from bot.poller import run_poller
from bot.ratelimit import RateLimiter
from bot.tools import build_default_registry
from bot.tools.alerts import set_warning_seconds

log = logging.getLogger("bot")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB global error handler: never swallow handler/network errors silently."""
    log.error("unhandled error while processing update", exc_info=context.error)


def build_app(settings: Settings) -> Application:
    api = ApiClient(
        settings.bot_api_base_url,
        settings.bot_api_username,
        settings.bot_api_password.get_secret_value(),
        timeout=settings.bot_api_timeout,
    )
    set_warning_seconds(settings.bot_shutoff_warning_seconds)
    deps = Deps(
        api=api,
        registry=build_default_registry(),
        store=LinkStore(settings.admin_ids, settings.bot_state_file or None),
        limiter=RateLimiter(settings.bot_send_rate),
        audit=AuditLog(settings.audit_path),
        confirm=ConfirmStore(),
    )

    if settings.bot_nlu_enabled:
        from bot.nlu import ChatMemory, NluLimiter, NluRouter, build_client

        client = build_client(settings.nlu_api_key, settings.llm_timeout_s)
        if client is None:
            log.warning("BOT_NLU_ENABLED is set but smart mode cannot start (no key or no anthropic package)")
        deps.nlu = NluRouter(
            deps.registry,
            client,
            deps.audit,
            model=settings.llm_model,
            timeout_s=settings.llm_timeout_s,
            max_rounds=settings.bot_nlu_max_rounds,
            limiter=NluLimiter(
                settings.bot_nlu_user_per_min, settings.bot_nlu_user_per_day, settings.bot_nlu_global_per_day
            ),
            memory=ChatMemory(),
        )

    async def post_init(app: Application) -> None:
        app.bot_data["poller"] = asyncio.create_task(
            run_poller(app.bot, deps, settings.bot_poll_interval, settings.seen_path)
        )

    async def post_shutdown(app: Application) -> None:
        task = app.bot_data["poller"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
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
        ("usage", commands.usage),
        ("losses", commands.losses),
        ("valve", commands.valve),
        ("approve", commands.approve),
        ("reject", commands.reject),
        ("revoke", commands.revoke),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callbacks.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queries.on_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs full request URLs, which include the bot token: silence it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    build_app(Settings()).run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

"""Proactive alerts: poll the backend and push new ones to linked users."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.deps import Deps, send
from bot.tools.alerts import fmt_alert

log = logging.getLogger("bot.poller")


def alert_keyboard(alert_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Acknowledge", callback_data=f"alert:ack:{alert_id}"),
                InlineKeyboardButton("Snooze 1h", callback_data=f"alert:snooze:{alert_id}"),
            ]
        ]
    )


async def poll_once(bot: Any, deps: Deps, seen: set[str]) -> int:
    pushed = 0
    for state in ("alert", "ticket"):
        for a in await deps.api.alerts(state=state):
            aid = str(a.get("id"))
            if aid in seen:
                continue
            seen.add(aid)
            for chat_id in deps.store.recipients().values():
                await send(bot, deps, chat_id, fmt_alert(a), reply_markup=alert_keyboard(aid))
                pushed += 1
    return pushed


async def run_poller(bot: Any, deps: Deps, interval: float) -> None:
    seen: set[str] = set()
    first = True
    while True:
        try:
            if first:  # do not replay the existing backlog on startup
                for state in ("alert", "ticket"):
                    seen.update(str(a.get("id")) for a in await deps.api.alerts(state=state))
                first = False
            else:
                await poll_once(bot, deps, seen)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("alert poll failed", exc_info=False)
        await asyncio.sleep(interval)

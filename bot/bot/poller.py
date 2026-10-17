"""Proactive alerts: poll the backend and push new ones to linked users.

Policy (backend states in backend/app/models/alerts.py):
- `alert`, `ticket`, `auto_shutoff` are pushed individually, with action buttons.
  `auto_shutoff` (valve closed automatically) is the most critical and is pushed.
- `nudge` is low priority: pushed only as one digest message per recipient per poll.
- Dedupe key is `<alert_id>:<state>`, so an escalation (alert -> ticket -> auto_shutoff) is
  pushed again; a key is marked seen only after at least one successful delivery and is
  persisted so a restart does not re-spam.
- The shut-off protocol is visible on the alert row (the bot has no websocket): a `ticket` with
  `shutoff_warned_at` is a `shutoff_warning` (countdown; Ack/Override buttons for staff, text
  only for residents), one with `shutoff_gave_up_at` is `shutoff_failed` (staff only). Keys
  are `<id>:shutoff_warning:<warned_at>` (a re-armed warning is a new event) and
  `<id>:shutoff_failed`. Water is only reported shut off for state `auto_shutoff`.
- Residents only receive alerts of their bound flat; staff receive everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.deps import Deps, send
from bot.fsutil import atomic_write_json
from bot.auth import Principal, Role
from bot.tools.alerts import fmt_alert, fmt_shutoff_failed, fmt_shutoff_warning
from bot.tools.scope import can_see_alert

log = logging.getLogger("bot.poller")

PUSH_STATES = ("auto_shutoff", "ticket", "alert")
DIGEST_STATES = ("nudge",)
MAX_SEEN = 5000
MAX_BACKOFF = 300.0


class SeenStore:
    """Insertion-ordered, bounded set of delivered alert keys, optionally persisted."""

    def __init__(self, path: str | None = None, cap: int = MAX_SEEN) -> None:
        self._path = Path(path) if path else None
        self._cap = cap
        self._keys: dict[str, None] = {}
        self.loaded = False
        self._dirty = False
        if self._path and self._path.exists():
            try:
                self._keys = dict.fromkeys(str(k) for k in json.loads(self._path.read_text())["seen"])
                self.loaded = True
            except (OSError, ValueError, KeyError, TypeError):
                log.error("could not read seen file %s; starting fresh", self._path, exc_info=True)

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str) -> None:
        if key not in self._keys:
            self._keys[key] = None
            self._dirty = True
            while len(self._keys) > self._cap:
                del self._keys[next(iter(self._keys))]

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        try:
            atomic_write_json(self._path, {"seen": list(self._keys)})
            self._dirty = False
        except OSError:
            log.error("could not persist seen file %s", self._path, exc_info=True)


def alert_keyboard(alert_id: str, state: str = "alert") -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("Acknowledge", callback_data=f"alert:ack:{alert_id}")]
    if state != "auto_shutoff":  # the backend refuses to snooze an auto shut-off
        row.append(InlineKeyboardButton("Snooze 1h", callback_data=f"alert:snooze:{alert_id}"))
    row.append(InlineKeyboardButton("Resolve", callback_data=f"alert:resolve:{alert_id}"))
    return InlineKeyboardMarkup([row])


def warning_keyboard(alert_id: str, role: Role) -> InlineKeyboardMarkup | None:
    """Staff can cancel the pending shut-off: acknowledge (staff) or override (admin only)."""
    row = [InlineKeyboardButton("Acknowledge (cancel shut-off)", callback_data=f"alert:ack:{alert_id}")]
    if role >= Role.ADMIN:
        row.append(InlineKeyboardButton("Override", callback_data=f"alert:override:{alert_id}"))
    return InlineKeyboardMarkup([row]) if role >= Role.MAINTENANCE else None


def _key(a: dict) -> str:
    return f"{a.get('id')}:{a.get('state')}"


def _warning_key(a: dict) -> str | None:
    if (
        a.get("shutoff_warned_at")
        and not a.get("shutoff_gave_up_at")
        and a.get("state") in ("nudge", "alert", "ticket")
    ):
        return f"{a.get('id')}:shutoff_warning:{a['shutoff_warned_at']}"
    return None


def _failed_key(a: dict) -> str | None:
    if a.get("shutoff_gave_up_at") and a.get("state") in ("nudge", "alert", "ticket"):
        return f"{a.get('id')}:shutoff_failed"
    return None


def event_keys(a: dict) -> list[str]:
    return [k for k in (_key(a), _warning_key(a), _failed_key(a)) if k]


async def _fetch(deps: Deps, states: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    for state in states:
        out.extend(await deps.api.alerts(state=state, limit=500))
    return out


async def _deliver(bot: Any, deps: Deps, chat_id: int, text: str, **kw: Any) -> bool:
    try:
        await send(bot, deps, chat_id, text, **kw)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - blocked bot, network, flood: one chat must not stop others
        log.warning("push to chat %s failed: %s: %s", chat_id, type(exc).__name__, exc)
        return False


Keyboard = Callable[[Principal], InlineKeyboardMarkup | None]


async def _push(
    bot: Any,
    deps: Deps,
    seen: SeenStore,
    key: str,
    recipients: list[tuple[Principal, int]],
    a: dict,
    text: str,
    keyboard: Keyboard,
    staff_only: bool = False,
) -> int:
    """Deliver one event to everyone who may see it; mark it seen after >= 1 success."""
    ok = 0
    for principal, chat_id in recipients:
        if (staff_only and principal.role < Role.MAINTENANCE) or not can_see_alert(principal, a):
            continue
        kb = keyboard(principal)
        extra = {"reply_markup": kb} if kb is not None else {}
        if await _deliver(bot, deps, chat_id, text, **extra):
            ok += 1
    if ok:
        seen.add(key)
    else:
        log.error("alert event %s not delivered to anyone; will retry", key)
    return ok


async def poll_once(bot: Any, deps: Deps, seen: SeenStore) -> int:
    """Push new alerts and shut-off warning / failure events; returns successful deliveries."""
    recipients = deps.store.recipients()
    delivered = 0
    for a in await _fetch(deps, PUSH_STATES):
        aid, state = str(a["id"]), a["state"]
        unit: str | None = None
        key, wkey, fkey = _key(a), _warning_key(a), _failed_key(a)
        if key not in seen:
            unit = await deps.api.unit_label(a["flat_id"])
            delivered += await _push(
                bot, deps, seen, key, recipients, a, fmt_alert(a, unit), lambda _p: alert_keyboard(aid, state)
            )
        if wkey and wkey not in seen:
            unit = unit or await deps.api.unit_label(a["flat_id"])
            text = fmt_shutoff_warning(a, unit)
            delivered += await _push(
                bot, deps, seen, wkey, recipients, a, text, lambda p: warning_keyboard(aid, p.role)
            )
        if fkey and fkey not in seen:
            unit = unit or await deps.api.unit_label(a["flat_id"])
            text = fmt_shutoff_failed(a, unit)
            delivered += await _push(
                bot, deps, seen, fkey, recipients, a, text, lambda _p: alert_keyboard(aid, "ticket"), staff_only=True
            )
    delivered += await _digest(bot, deps, seen, recipients)
    seen.save()
    return delivered


async def _digest(bot: Any, deps: Deps, seen: SeenStore, recipients: list) -> int:
    new = [a for a in await _fetch(deps, DIGEST_STATES) if _key(a) not in seen]
    if not new:
        return 0
    delivered = 0
    done: set[str] = set()
    for principal, chat_id in recipients:
        mine = [a for a in new if can_see_alert(principal, a)]
        if not mine:
            continue
        units = [await deps.api.unit_label(a["flat_id"]) for a in mine]
        text = f"{len(mine)} new nudge(s): " + ", ".join(units[:30])
        if await _deliver(bot, deps, chat_id, text):
            delivered += 1
            done.update(_key(a) for a in mine)
    for k in done:
        seen.add(k)
    return delivered


async def run_poller(
    bot: Any,
    deps: Deps,
    interval: float,
    seen_path: str | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    seen = SeenStore(seen_path)
    delay = interval
    first = not seen.loaded
    while True:
        try:
            if first:  # no history: do not replay the existing backlog
                for a in await _fetch(deps, PUSH_STATES + DIGEST_STATES):
                    for k in event_keys(a):
                        seen.add(k)
                seen.save()
                first = False
            else:
                await poll_once(bot, deps, seen)
            delay = interval
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.error("alert poll failed; retrying in %.0fs", min(delay * 2, MAX_BACKOFF), exc_info=True)
            delay = min(delay * 2, MAX_BACKOFF)
        await sleep(delay)

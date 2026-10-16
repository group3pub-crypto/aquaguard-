"""Single-use, expiring confirmations bound to chat + user + message."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TTL_SECONDS = 60.0
MAX_PENDING = 200


@dataclass
class _Pending:
    user_id: int
    chat_id: int
    payload: dict[str, Any]
    expires: float
    message_id: int | None = None


class ConfirmStore:
    def __init__(self, ttl: float = TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._items: dict[str, _Pending] = {}

    def _purge(self) -> None:
        now = self._clock()
        for k in [k for k, v in self._items.items() if v.expires <= now]:
            del self._items[k]

    def create(self, user_id: int, chat_id: int, payload: dict[str, Any]) -> str:
        self._purge()
        while len(self._items) >= MAX_PENDING:  # drop the oldest
            del self._items[next(iter(self._items))]
        token = secrets.token_urlsafe(8)
        self._items[token] = _Pending(user_id, chat_id, payload, self._clock() + self._ttl)
        return token

    def bind_message(self, token: str, message_id: int) -> None:
        item = self._items.get(token)
        if item is not None:
            item.message_id = message_id

    def consume(self, token: str, user_id: int, chat_id: int, message_id: int | None) -> dict[str, Any] | None:
        """Return the payload once, only to the originating user/chat/message before expiry.

        A press from someone else (or on a copied message) does not burn the token.
        """
        self._purge()
        item = self._items.get(token)
        if item is None:
            return None
        if item.user_id != user_id or item.chat_id != chat_id:
            return None
        if item.message_id is not None and item.message_id != message_id:
            return None
        del self._items[token]
        return item.payload

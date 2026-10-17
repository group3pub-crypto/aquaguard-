"""WebSocket connection manager: per-connection scope, per-user cap, global cap."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog
from fastapi import WebSocket

from app.scope import Scope

log = structlog.get_logger(__name__)
SEND_TIMEOUT_S = 5.0


@dataclass(eq=False)
class Client:
    ws: WebSocket
    user_id: str = ""
    scope: Scope = field(default_factory=lambda: Scope("none"))
    # optional client-supplied narrowing; can only REDUCE what the scope already allows
    filters: set[str] = field(default_factory=set)


class ConnectionManager:
    def __init__(self, max_connections: int = 200, max_per_user: int = 5) -> None:
        self.max_connections = max_connections
        self.max_per_user = max_per_user
        self._clients: list[Client] = []

    @property
    def count(self) -> int:
        return len(self._clients)

    @property
    def full(self) -> bool:
        return self.count >= self.max_connections

    def count_for(self, user_id: str) -> int:
        return sum(1 for c in self._clients if c.user_id == user_id)

    def user_full(self, user_id: str) -> bool:
        return self.count_for(user_id) >= self.max_per_user

    def add(
        self, ws: WebSocket, user_id: str, scope: Scope, filters: Iterable[str] = ()
    ) -> Client:
        client = Client(ws, user_id, scope, {f for f in filters if f})
        self._clients.append(client)
        return client

    def disconnect(self, client: Client) -> None:
        if client in self._clients:
            self._clients.remove(client)

    async def broadcast(self, message: dict, key: str | Iterable[str] | None = None) -> int:
        """Deliver to clients whose SCOPE contains one of `key` (flat / society ids) and whose
        optional filters match too. Unkeyed messages reach only unfiltered all-scope clients.
        Returns the number of successful sends."""
        keys = {key} if isinstance(key, str) else set(key or ())
        targets = [
            c
            for c in list(self._clients)
            if c.scope.matches_keys(keys) and (not c.filters or (c.filters & keys))
        ]

        async def _send(c: Client) -> bool:
            try:
                await asyncio.wait_for(c.ws.send_json(message), SEND_TIMEOUT_S)
                return True
            except Exception:
                log.info("ws_send_failed_dropping_client")
                self.disconnect(c)
                return False

        results = await asyncio.gather(*(_send(c) for c in targets))
        return sum(results)

    async def close_all(self, code: int = 1001) -> None:
        for c in list(self._clients):
            try:
                await c.ws.close(code=code)
            except Exception:
                log.debug("ws_close_failed")
            self.disconnect(c)

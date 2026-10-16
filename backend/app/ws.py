"""WebSocket connection manager with optional flat/society filters and a connection cap."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog
from fastapi import WebSocket

log = structlog.get_logger(__name__)
SEND_TIMEOUT_S = 5.0


@dataclass(eq=False)
class Client:
    ws: WebSocket
    filters: set[str] = field(default_factory=set)


class ConnectionManager:
    def __init__(self, max_connections: int = 200) -> None:
        self.max_connections = max_connections
        self._clients: list[Client] = []

    @property
    def count(self) -> int:
        return len(self._clients)

    @property
    def full(self) -> bool:
        return self.count >= self.max_connections

    async def connect(self, ws: WebSocket, filters: Iterable[str] = ()) -> Client:
        await ws.accept()
        client = Client(ws, {f for f in filters if f})
        self._clients.append(client)
        return client

    def disconnect(self, client: Client) -> None:
        if client in self._clients:
            self._clients.remove(client)

    async def broadcast(self, message: dict, key: str | Iterable[str] | None = None) -> int:
        """Send to clients with no filters or a filter matching `key`. Returns sent count."""
        keys = {key} if isinstance(key, str) else set(key or ())
        targets = [c for c in list(self._clients) if not c.filters or (c.filters & keys)]

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

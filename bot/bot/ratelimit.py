from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Spaces calls so the average rate never exceeds `rate` per second."""

    def __init__(self, rate: float = 25.0) -> None:
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)

"""Cost/abuse controls: daily call budget, circuit breaker, small LRU cache."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime


class DailyBudget:
    """Counts provider calls per UTC day; `try_take()` is False once the budget is spent."""

    def __init__(self, limit: int, today: Callable[[], str] | None = None) -> None:
        self.limit = limit
        self._today = today or (lambda: datetime.now(UTC).date().isoformat())
        self._day = self._today()
        self.used = 0

    def try_take(self) -> bool:
        day = self._today()
        if day != self._day:
            self._day, self.used = day, 0
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


class CircuitBreaker:
    """Opens after `threshold` consecutive provider failures; one trial call after `cooldown_s`."""

    def __init__(
        self, threshold: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.threshold, self.cooldown_s, self._clock = threshold, cooldown_s, clock
        self.failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None and self._clock() - self._opened_at < self.cooldown_s

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self.cooldown_s:  # half-open: let one probe through
            self._opened_at = self._clock()
            return True
        return False

    def success(self) -> None:
        self.failures, self._opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self._opened_at = self._clock()


class LRUCache:
    def __init__(self, size: int) -> None:
        self.size = size
        self._d: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return None

    def put(self, key: str, value: str) -> None:
        if self.size <= 0:
            return
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.size:
            self._d.popitem(last=False)

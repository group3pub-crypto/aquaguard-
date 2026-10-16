"""Rate limiting on top of the `limits` library with a configurable shared store.

``RATE_LIMIT_STORAGE_URI=memory://`` (default) is per process; use ``redis://...`` when running
several workers/instances. Keys are built from a rule name, the client IP and optional extra
identifiers (the login limit uses IP + username).
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from limits import parse
from limits.storage import MemoryStorage, Storage, storage_from_string
from limits.strategies import MovingWindowRateLimiter

from app.config import Settings

LOGIN_LIMIT = "5/minute"  # per IP + username
LOGIN_IP_LIMIT = "30/minute"  # per IP over all usernames
REGISTER_LIMIT = "3/minute"
REFRESH_LIMIT = "30/minute"
PASSWORD_LIMIT = "5/minute"  # per user
TICKET_LIMIT = "30/minute"  # per user


class RateLimiter:
    def __init__(self) -> None:
        self.enabled = True
        self._storage: Storage = MemoryStorage()
        self._strategy = MovingWindowRateLimiter(self._storage)

    def configure(self, settings: Settings) -> None:
        self.enabled = settings.rate_limit_enabled
        self._storage = storage_from_string(settings.rate_limit_storage_uri)
        self._strategy = MovingWindowRateLimiter(self._storage)

    def reset(self) -> None:
        self._storage.reset()

    def allow(self, rule: str, *identifiers: str) -> bool:
        if not self.enabled:
            return True
        return self._strategy.hit(parse(rule), *identifiers)

    def check(self, request: Request, name: str, rule: str, *extra: str) -> None:
        """Raise 429 when the (name, client IP, extra...) bucket is exhausted."""
        ip = request.client.host if request.client else "unknown"
        if not self.allow(rule, name, ip, *(e.lower() for e in extra)):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests",
                headers={"Retry-After": "60"},
            )


limiter = RateLimiter()

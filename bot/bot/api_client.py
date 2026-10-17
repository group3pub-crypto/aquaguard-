"""Async client for the AquaGuard backend using a dedicated SERVICE account.

Auth model (backend: 15-minute access tokens + rotating refresh tokens):
- log in once with the service credentials; keep BOTH tokens in memory only;
- refresh proactively shortly before expiry and reactively on a 401 (retry the request once);
- refresh is single-flight: concurrent callers share one `POST /auth/refresh`;
- every refresh response replaces BOTH tokens (the presented refresh token is spent; presenting
  it again would make the backend revoke the whole family);
- if a refresh fails for any reason (expired, reused, revoked, network) fall back to a fresh login
  with the credentials;
- tokens and the password are never logged or put into exception messages.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("bot.api")

FLAT_CACHE_TTL = 300.0
EXPIRY_SKEW = 30.0  # refresh this many seconds before the access token expires
BACKOFF_BASE = 0.3
BACKOFF_CAP = 5.0
LOGIN_COOLDOWN = 5.0  # after a failed login, do not hit the backend again for a moment
MAX_ON_BEHALF_OF = 100
RETRY_STATUSES = frozenset({429, 502, 503, 504})


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"backend returned {status}: {message}")
        self.status = status


def on_behalf_of_value(value: str | None) -> str | None:
    """Clamp the asserted end user to the backend's limit (100 chars); empty -> None."""
    if value is None:
        return None
    value = " ".join(value.split())[:MAX_ON_BEHALF_OF]
    return value or None


class ApiClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._retries = retries
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter only
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self._auth_lock = asyncio.Lock()
        self._login_blocked_until = 0.0
        self._flats: tuple[float, list[dict]] | None = None
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def __repr__(self) -> str:  # never expose credentials / tokens
        return f"<ApiClient user={self._username!r} authenticated={self._access is not None}>"

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- auth --------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full-ish jitter (0.5x - 1.5x)."""
        return min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt) * (0.5 + self._rng.random())

    def _store_tokens(self, data: dict) -> None:
        # always keep the NEW refresh token: the old one is spent
        self._access = str(data["access_token"])
        self._refresh = str(data["refresh_token"])
        self._expires_at = self._clock() + float(data.get("expires_in", 0))

    def _clear_tokens(self) -> None:
        self._access = self._refresh = None
        self._expires_at = 0.0

    def _fresh(self) -> bool:
        return self._access is not None and self._clock() < self._expires_at - EXPIRY_SKEW

    async def _auth_post(self, path: str, body: dict) -> httpx.Response:
        """POST to an auth endpoint; transport errors retried with jittered backoff."""
        attempt = 0
        while True:
            try:
                return await self._http.post(path, json=body)
            except httpx.TransportError as exc:
                if attempt >= self._retries:
                    raise ApiError(0, f"backend unreachable ({type(exc).__name__})") from exc
                attempt += 1
                await self._sleep(self._backoff(attempt))

    async def _login(self) -> None:
        if self._clock() < self._login_blocked_until:
            raise ApiError(401, "service account login failed (cooling down)")
        resp = await self._auth_post("/api/v1/auth/login", {"username": self._username, "password": self._password})
        if resp.status_code != 200:
            self._clear_tokens()
            self._login_blocked_until = self._clock() + LOGIN_COOLDOWN * (0.5 + self._rng.random())
            raise ApiError(resp.status_code, "service account login failed")
        self._store_tokens(resp.json())

    async def _refresh_tokens(self) -> bool:
        """Rotate the refresh token. True on success; False means 're-login with credentials'."""
        token, self._refresh = self._refresh, None  # a spent token must never be sent twice
        if not token:
            return False
        try:
            resp = await self._http.post("/api/v1/auth/refresh", json={"refresh_token": token})
        except httpx.TransportError:
            # the response may have been lost after the backend rotated: the old token is spent
            return False
        if resp.status_code != 200:
            return False
        try:
            self._store_tokens(resp.json())
        except (KeyError, ValueError, TypeError):
            return False
        return True

    async def _ensure_token(self) -> str:
        if self._fresh():
            return str(self._access)
        async with self._auth_lock:
            if not self._fresh():
                await self._renew_locked()
            return str(self._access)

    async def _renew_locked(self) -> None:
        if self._refresh and await self._refresh_tokens():
            return
        log.info("refresh unavailable or rejected; logging in again")
        self._clear_tokens()
        await self._login()

    async def _renew_after_401(self, rejected: str) -> None:
        """Single-flight: if another task already replaced the rejected token, just reuse it."""
        async with self._auth_lock:
            if self._access == rejected or self._access is None:
                await self._renew_locked()

    # --- transport ---------------------------------------------------------
    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        renewed = False
        attempt = 0
        while True:
            token = await self._ensure_token()
            try:
                resp = await self._http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
            except httpx.TransportError as exc:  # includes timeouts
                if attempt >= self._retries:
                    raise ApiError(0, f"backend unreachable ({type(exc).__name__})") from exc
                attempt += 1
                await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code == 401 and not renewed:
                renewed = True
                await self._renew_after_401(token)
                continue
            if (resp.status_code >= 500 or resp.status_code in RETRY_STATUSES) and attempt < self._retries:
                attempt += 1
                await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, resp.reason_phrase)
            return resp.json() if resp.content else None

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def post(self, path: str, json: dict | None = None) -> Any:
        return await self._request("POST", path, json=json)

    async def patch(self, path: str, json: dict | None = None) -> Any:
        return await self._request("PATCH", path, json=json)

    # --- endpoint wrappers: the only place that knows backend paths ---------
    async def societies(self) -> list[dict]:
        return await self.get("/api/v1/societies")

    async def default_society_id(self) -> str:
        """The bot serves the first society returned by the backend."""
        socs = await self.societies()
        if not socs:
            raise ApiError(404, "no society configured")
        return str(socs[0]["id"])

    async def _flat_index(self) -> list[dict]:
        now = self._clock()
        if self._flats is not None and now - self._flats[0] < FLAT_CACHE_TTL:
            return self._flats[1]
        sid = await self.default_society_id()
        flats: list[dict] = []
        offset = 0
        while True:
            page = await self.get("/api/v1/flats", society_id=sid, limit=500, offset=offset)
            flats.extend(page)
            if len(page) < 500:
                break
            offset += 500
        self._flats = (now, flats)
        return flats

    async def find_flat(self, unit: str) -> dict:
        """Map a unit number such as 'A-101' to the backend flat ({id, unit_number, ...})."""
        for f in await self._flat_index():
            if str(f.get("unit_number")).lower() == unit.strip().lower():
                return f
        raise ApiError(404, f"unknown unit {unit}")

    async def unit_label(self, flat_id: str) -> str:
        """Human unit number for a flat id (falls back to a short id if unknown)."""
        try:
            for f in await self._flat_index():
                if str(f["id"]) == str(flat_id):
                    return str(f["unit_number"])
        except ApiError:
            pass
        return str(flat_id)[:8]

    async def alerts(self, state: str | None = None, flat_id: str | None = None, limit: int = 100) -> list[dict]:
        """GET /alerts (a plain list, one `state` per call)."""
        return await self.get("/api/v1/alerts", state=state, flat_id=flat_id, limit=limit)

    async def active_alerts(self, flat_id: str | None = None, limit: int = 50) -> list[dict]:
        """POST /tools/active-alerts: all open states, ordered by severity (backend max 100)."""
        body: dict[str, Any] = {"limit": min(limit, 100)}
        if flat_id:
            body["flat_id"] = flat_id
        else:
            body["society_id"] = await self.default_society_id()
        data = await self.post("/api/v1/tools/active-alerts", body)
        return data["alerts"]

    async def flat_usage(self, flat_id: str, hours: int = 24) -> dict:
        return await self.post("/api/v1/tools/flat-usage", {"flat_id": flat_id, "hours": hours})

    async def top_losses(self, days: int = 7, limit: int = 5) -> dict:
        sid = await self.default_society_id()
        return await self.post("/api/v1/tools/top-losses", {"society_id": sid, "days": days, "limit": limit})

    async def weekly_report(self) -> str:
        sid = await self.default_society_id()
        data = await self.post("/api/v1/tools/weekly-report", {"society_id": sid})
        return data["markdown"]

    async def society_stats(self) -> dict:
        sid = await self.default_society_id()
        return await self.get(f"/api/v1/societies/{sid}/stats")

    # The backend records the authenticated service account as the actor and ignores body actor
    # fields (acknowledged_by, snoozed_by, resolved_by, overridden_by, issued_by). The only
    # attribution it accepts is `on_behalf_of`: stored and echoed as ASSERTED, not verified.
    @staticmethod
    def _obo(body: dict[str, Any], on_behalf_of: str | None) -> dict[str, Any]:
        value = on_behalf_of_value(on_behalf_of)
        if value is not None:
            body["on_behalf_of"] = value
        return body

    async def ack_alert(self, alert_id: str, on_behalf_of: str | None = None) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/acknowledge", self._obo({}, on_behalf_of))

    async def snooze_alert(self, alert_id: str, minutes: int = 60, on_behalf_of: str | None = None) -> dict:
        return await self.post(
            f"/api/v1/alerts/{alert_id}/snooze", self._obo({"duration_minutes": minutes}, on_behalf_of)
        )

    async def resolve_alert(self, alert_id: str, on_behalf_of: str | None = None) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/resolve", self._obo({}, on_behalf_of))

    async def override_alert(self, alert_id: str, reason: str, on_behalf_of: str | None = None) -> dict:
        """Cancel a pending auto shut-off (admin service account only)."""
        return await self.post(f"/api/v1/alerts/{alert_id}/override", self._obo({"reason": reason[:500]}, on_behalf_of))

    async def set_valve(self, unit: str, action: str, on_behalf_of: str | None = None) -> dict:
        flat = await self.find_flat(unit)
        body = {"flat_id": flat["id"], "action": action, "reason": "telegram bot"}
        return await self.post("/api/v1/tools/valve-control", self._obo(body, on_behalf_of))

    # --- users (link flow) -------------------------------------------------
    async def user_by_username(self, username: str) -> dict:
        """GET /users/by-username/{u} (service/admin only); 404 -> ApiError(404)."""
        return await self.get(f"/api/v1/users/by-username/{quote(username, safe='')}")

    async def set_telegram_id(self, user_id: str, telegram_id: int) -> dict:
        """PATCH /users/{id} {telegram_id}; admin only on the backend."""
        return await self.patch(f"/api/v1/users/{user_id}", {"telegram_id": telegram_id})

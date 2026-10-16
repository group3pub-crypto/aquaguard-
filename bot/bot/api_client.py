"""Async client for the AquaGuard backend using a dedicated service account."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"backend returned {status}: {message}")
        self.status = status


class ApiClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._retries = retries
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        resp = await self._http.post(
            "/api/v1/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise ApiError(resp.status_code, "service account login failed")
        self._token = resp.json()["access_token"]

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        relogged = False
        attempt = 0
        while True:
            if self._token is None:
                async with self._lock:
                    if self._token is None:
                        await self._login()
            try:
                resp = await self._http.request(method, path, headers={"Authorization": f"Bearer {self._token}"}, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self._retries:
                    raise ApiError(0, f"backend unreachable ({type(exc).__name__})") from exc
                attempt += 1
                await asyncio.sleep(0.2 * 2**attempt)
                continue
            if resp.status_code == 401 and not relogged:
                relogged = True
                self._token = None  # expired: log in again once
                continue
            if resp.status_code >= 500 and attempt < self._retries:
                attempt += 1
                await asyncio.sleep(0.2 * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, resp.reason_phrase)
            return resp.json() if resp.content else None

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def post(self, path: str, json: dict | None = None) -> Any:
        return await self._request("POST", path, json=json)

    # --- endpoint wrappers: the only place that knows backend paths ---------
    async def alerts(self, state: str | None = None) -> list[dict]:
        data = await self.get("/api/v1/alerts", state=state)
        return data["items"] if isinstance(data, dict) and "items" in data else data

    async def usage_summary(self, unit: str | None = None, days: int = 1) -> dict:
        return await self.get("/api/v1/usage/summary", unit=unit, days=days)

    async def top_losses(self, limit: int = 5) -> list[dict]:
        return await self.get("/api/v1/losses/top", limit=limit)

    async def report(self) -> dict:
        return await self.get("/api/v1/reports/summary")

    async def ack_alert(self, alert_id: str) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/ack")

    async def snooze_alert(self, alert_id: str, minutes: int = 60) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/snooze", {"minutes": minutes})

    async def set_valve(self, unit: str, action: str) -> dict:
        return await self.post(f"/api/v1/units/{unit}/valve", {"action": action})

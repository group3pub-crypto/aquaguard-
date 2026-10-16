"""Async client for the AquaGuard backend using a dedicated service account."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


FLAT_CACHE_TTL = 300.0


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
        self._flats: tuple[float, list[dict]] | None = None
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
    async def societies(self) -> list[dict]:
        return await self.get("/api/v1/societies")

    async def default_society_id(self) -> str:
        """The bot serves the first society returned by the backend."""
        socs = await self.societies()
        if not socs:
            raise ApiError(404, "no society configured")
        return str(socs[0]["id"])

    async def _flat_index(self) -> list[dict]:
        now = time.monotonic()
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
        """POST /tools/active-alerts: all open states, ordered by severity."""
        body: dict[str, Any] = {"limit": limit}
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

    # The backend ignores any actor in these bodies and records the service account.
    async def ack_alert(self, alert_id: str) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/acknowledge", {})

    async def snooze_alert(self, alert_id: str, minutes: int = 60) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/snooze", {"duration_minutes": minutes})

    async def resolve_alert(self, alert_id: str) -> dict:
        return await self.post(f"/api/v1/alerts/{alert_id}/resolve", {})

    async def set_valve(self, unit: str, action: str, issued_by: str) -> dict:
        """`issued_by` IS stored by the backend (ValveCommand.issued_by)."""
        flat = await self.find_flat(unit)
        return await self.post(
            "/api/v1/tools/valve-control",
            {"flat_id": flat["id"], "action": action, "issued_by": issued_by[:100], "reason": "telegram bot"},
        )

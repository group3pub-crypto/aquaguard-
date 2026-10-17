"""Shared fixtures: real-shaped backend payloads + a schema-checking backend simulator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx

from bot.api_client import ApiClient
from bot.audit import AuditLog
from bot.auth import LinkStore, Role
from bot.confirm import ConfirmStore
from bot.deps import Deps
from bot.ratelimit import RateLimiter
from bot.tools import build_default_registry

SNAPSHOT = json.loads((Path(__file__).parent / "openapi.snapshot.json").read_text())

SOC = "11111111-1111-4111-8111-111111111111"
F101 = "aaaaaaaa-0000-4000-8000-000000000101"
F102 = "aaaaaaaa-0000-4000-8000-000000000102"
AL1 = "bbbbbbbb-0000-4000-8000-000000000001"
AL2 = "bbbbbbbb-0000-4000-8000-000000000002"
AL3 = "bbbbbbbb-0000-4000-8000-000000000003"
AN = "cccccccc-0000-4000-8000-000000000001"
ADMIN, RES, MAINT, STRANGER = 1, 2, 3, 4

FLATS = [
    {
        "id": F101,
        "society_id": SOC,
        "unit_number": "A-101",
        "floor": 1,
        "residents": 3,
        "sensor_id": "esp-101",
        "valve_state": "open",
        "baseline_status": "ready",
    },
    {
        "id": F102,
        "society_id": SOC,
        "unit_number": "A-102",
        "floor": 1,
        "residents": 2,
        "sensor_id": "esp-102",
        "valve_state": "open",
        "baseline_status": "ready",
    },
]
SOCIETIES = [
    {
        "id": SOC,
        "name": "Towers",
        "slug": "towers",
        "address": None,
        "total_flats": 2,
        "created_at": "2026-01-01T00:00:00Z",
    }
]


def alert(aid: str, flat: str, state: str, tier: int = 2, msg: str | None = "Possible leak") -> dict:
    return {
        "id": aid,
        "flat_id": flat,
        "anomaly_id": AN,
        "state": state,
        "tier": tier,
        "severity_score": 0.72,
        "message": msg,
        "message_source": "template",
        "escalated_at": [],
        "snoozed_until": None,
        "acknowledged_by": None,
        "acknowledged_at": None,
        "resolved_at": None,
        "shutoff_warned_at": None,
        "shutoff_gave_up_at": None,
        "created_at": "2026-10-16T10:00:00Z",
        "updated_at": "2026-10-16T10:00:00Z",
    }


USAGE = {"flat_id": F101, "hours": 24, "volume_l": 312.46, "avg_lpm": 0.217, "expected_l": 280.0}
LOSSES = {
    "society_id": SOC,
    "days": 7,
    "losses": [
        {"flat_id": F101, "unit": "A-101", "estimated_loss_l": 40.04},
        {"flat_id": F102, "unit": "A-102", "estimated_loss_l": 12.0},
    ],
}
REPORT_MD = "# Weekly water report: Towers\n_2026-10-09 to 2026-10-16_\n\n## Summary\n- Flats monitored: 2\n"
STATS = {"society_id": SOC, "flats": 2, "active_alerts": 3, "volume_24h_l": 910.5, "sensors_offline": 1}
USER_RES = {
    "id": "dddddddd-0000-4000-8000-000000000001",
    "username": "carol",
    "name": "Carol",
    "role": "resident",
    "flat_id": F102,
    "society_id": SOC,
    "is_active": True,
    "is_service": False,
    "telegram_id": None,
    "created_at": "2026-01-01T00:00:00Z",
}
USERS = {
    "carol": USER_RES,
    "mike": {
        **USER_RES,
        "id": "dddddddd-0000-4000-8000-000000000002",
        "username": "mike",
        "role": "maintenance",
        "flat_id": None,
    },
    "root": {
        **USER_RES,
        "id": "dddddddd-0000-4000-8000-000000000003",
        "username": "root",
        "role": "admin",
        "flat_id": None,
    },
    "svc": {
        **USER_RES,
        "id": "dddddddd-0000-4000-8000-000000000004",
        "username": "svc",
        "role": "maintenance",
        "flat_id": None,
        "is_service": True,
    },
    "nofl": {**USER_RES, "id": "dddddddd-0000-4000-8000-000000000005", "username": "nofl", "flat_id": None},
}
VALVE_CMD = {
    "id": AL1,
    "flat_id": F101,
    "alert_id": None,
    "command": "close",
    "source": "manual_bot",
    "issued_by": "bot_service",
    "on_behalf_of": None,
    "on_behalf_of_verified": None,
    "issued_at": "2026-10-16T10:00:00Z",
    "confirmed_at": None,
    "status": "pending",
}


# --- openapi helpers ---------------------------------------------------------
def _resolve(schema: dict) -> dict:
    ref = schema.get("$ref")
    if ref:
        return SNAPSHOT["components"]["schemas"][ref.rsplit("/", 1)[1]]
    for key in ("anyOf", "oneOf"):
        for sub in schema.get(key, []):
            r = _resolve(sub)
            if r.get("type") == "object" or "properties" in r:
                return r
    return schema


def operation(method: str, path: str) -> dict:
    """Find the snapshot operation matching a concrete request path."""
    for tmpl, ops in SNAPSHOT["paths"].items():
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", tmpl), path) and method.lower() in ops:
            return ops[method.lower()]
    raise AssertionError(f"{method} {path} is not a backend endpoint")


def check_request(method: str, path: str, query: dict[str, str], body: Any) -> None:
    op = operation(method, path.removeprefix(""))
    declared = {p["name"]: p for p in op.get("parameters", []) if p["in"] == "query"}
    assert set(query) <= set(declared), f"unknown query params {set(query) - set(declared)}"
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if body is None or schema is None:
        assert body is None or schema is not None, f"{path} takes no body"
        return
    s = _resolve(schema)
    props = s.get("properties", {})
    assert set(body) <= set(props), f"{path}: unknown body keys {set(body) - set(props)}"
    assert set(s.get("required", [])) <= set(body), f"{path}: missing required keys"
    dead = {k for k, v in props.items() if v.get("deprecated")}
    assert not dead & set(body), f"{path}: sends dead (deprecated, ignored) fields {dead & set(body)}"
    for k, v in body.items():
        spec = props[k]
        if "maximum" in spec:
            assert v <= spec["maximum"], f"{k} above maximum"
        if "minimum" in spec:
            assert v >= spec["minimum"], f"{k} below minimum"
        if "enum" in spec:
            assert v in spec["enum"]
        if "pattern" in spec:
            assert re.search(spec["pattern"], v)


def check_response(method: str, path: str, payload: Any) -> None:
    """Fixture keys must be a subset of the response model's properties (when it has one)."""
    op = operation(method, path)
    schema = (
        op["responses"]
        .get("200", op["responses"].get("201", {}))
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if not schema or schema == {}:
        return  # tools endpoints declare no response model
    if schema.get("type") == "array":
        rows, s = payload, _resolve(schema["items"])
    else:
        rows, s = [payload], _resolve(schema)
    for row in rows if isinstance(rows, list) else [rows]:
        extra = set(row) - set(s.get("properties", {}))
        assert not extra, f"{path}: fixture keys not in backend schema: {extra}"


class AuthSim:
    """Backend token endpoints: 15-min access tokens, rotating refresh tokens, family revocation."""

    def __init__(self) -> None:
        self.n = 0
        self.access: dict[str, str] = {}  # valid access token -> family
        self.refresh: dict[str, str] = {}  # live refresh token -> family
        self.spent: dict[str, str] = {}  # spent refresh token -> family
        self.logins = 0
        self.refreshes = 0
        self.reuse_detected = 0
        self.fail_login = False

    def _issue(self, family: str) -> dict:
        self.n += 1
        at, rt = f"access-{self.n}", f"refresh-token-{self.n:08d}-padding"
        self.access[at] = family
        self.refresh[rt] = family
        return {
            "access_token": at,
            "refresh_token": rt,
            "token_type": "bearer",
            "role": "admin",
            "expires_in": 900,
            "refresh_expires_in": 86400,
        }

    def login(self) -> httpx.Response:
        self.logins += 1
        if self.fail_login:
            return httpx.Response(401, json={"detail": "Invalid credentials"})
        return httpx.Response(200, json=self._issue(f"fam-{self.logins}"))

    def do_refresh(self, token: str) -> httpx.Response:
        self.refreshes += 1
        if token in self.spent:  # reuse: the whole family is assumed stolen
            self.reuse_detected += 1
            fam = self.spent[token]
            self.refresh = {t: f for t, f in self.refresh.items() if f != fam}
            self.access = {t: f for t, f in self.access.items() if f != fam}
            return httpx.Response(401, json={"detail": "Invalid refresh token"})
        fam = self.refresh.pop(token, None)
        if fam is None:
            return httpx.Response(401, json={"detail": "Invalid refresh token"})
        self.spent[token] = fam
        return httpx.Response(200, json=self._issue(fam))

    def expire_access(self) -> None:
        self.access.clear()  # what 15 minutes do to every access token

    def authorised(self, req: httpx.Request) -> bool:
        return req.headers.get("authorization", "").removeprefix("Bearer ") in self.access


class BackendSim:
    """httpx transport that serves real-shaped payloads and validates every request."""

    def __init__(self, alerts: list[dict] | None = None) -> None:
        self.alerts = alerts if alerts is not None else []
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.auth = AuthSim()
        self.telegram_ids: dict[str, int] = {}
        self.patch_status = 200

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        query = dict(req.url.params)
        body = json.loads(req.content) if req.content else None
        if p.endswith("/auth/login"):
            check_request(m, p, query, body)
            return self.auth.login()
        if p.endswith("/auth/refresh"):
            check_request(m, p, query, body)
            return self.auth.do_refresh(body["refresh_token"])
        if not self.auth.authorised(req):
            return httpx.Response(401, json={"detail": "Not authenticated"})
        check_request(m, p, query, body)
        self.calls.append((m, p, query, body))
        out: Any
        if p == "/api/v1/societies":
            out = SOCIETIES
        elif p == "/api/v1/flats":
            out = FLATS
        elif p.startswith("/api/v1/users/by-username/"):
            user = USERS.get(p.rsplit("/", 1)[1])
            if user is None:
                return httpx.Response(404, json={"detail": "User not found"})
            out = user
        elif m == "PATCH" and p.startswith("/api/v1/users/"):
            if self.patch_status != 200:
                return httpx.Response(self.patch_status, json={"detail": "x"})
            uid = p.rsplit("/", 1)[1]
            self.telegram_ids[uid] = body["telegram_id"]
            out = next(u for u in USERS.values() if u["id"] == uid) | {"telegram_id": body["telegram_id"]}
        elif p == "/api/v1/alerts":
            out = [
                a
                for a in self.alerts
                if (not query.get("state") or a["state"] == query["state"])
                and (not query.get("flat_id") or a["flat_id"] == query["flat_id"])
            ]
        elif p == "/api/v1/tools/active-alerts":
            ids = [body["flat_id"]] if body.get("flat_id") else None
            al = [
                a
                for a in self.alerts
                if a["state"] in ("nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed")
                and (ids is None or a["flat_id"] in ids)
            ]
            out = {"count": len(al), "alerts": al}
        elif p == "/api/v1/tools/flat-usage":
            out = USAGE
        elif p == "/api/v1/tools/top-losses":
            out = LOSSES
        elif p == "/api/v1/tools/weekly-report":
            out = {"society_id": SOC, "markdown": REPORT_MD}
        elif p == "/api/v1/tools/valve-control":
            obo = body.get("on_behalf_of")
            out = {**VALVE_CMD, "on_behalf_of": obo, "on_behalf_of_verified": False if obo else None}
        elif p.endswith("/stats"):
            out = STATS
        else:  # alert actions: AlertActionOut echoes the asserted on_behalf_of
            obo = (body or {}).get("on_behalf_of")
            out = alert(AL1, F101, "acknowledged") | {
                "on_behalf_of": obo,
                "on_behalf_of_verified": False if obo else None,
            }
        check_response(m, p, out)
        return httpx.Response(200, json=out)


async def _no_sleep(_: float) -> None:
    return None


def make_api(sim: BackendSim, clock: Any = None) -> ApiClient:
    kw: dict[str, Any] = {"clock": clock} if clock else {}
    return ApiClient("http://backend", "bot", "pw", transport=httpx.MockTransport(sim), sleep=_no_sleep, **kw)


def make_deps(api: Any, tmp_path: Path | None = None) -> Deps:
    store = LinkStore({ADMIN}, str(tmp_path / "links.json") if tmp_path else None)
    store.links[RES] = {"username": "res", "chat_id": 20, "role": "resident", "flat_id": F101, "unit": "A-101"}
    store.links[MAINT] = {"username": "maint", "chat_id": 30, "role": "maintenance", "flat_id": None, "unit": None}
    return Deps(
        api=api,
        registry=build_default_registry(),
        store=store,
        limiter=RateLimiter(1000),
        audit=AuditLog(str(tmp_path / "audit.jsonl") if tmp_path else None),
        confirm=ConfirmStore(),
    )


def ctx(deps: Deps, args: list[str] | None = None) -> SimpleNamespace:
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}), args=args, bot=bot)


def upd(uid: int, text: str = "") -> SimpleNamespace:
    msg = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=500)), text=text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=uid), effective_message=msg
    )


__all__ = ["Role"]

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.api_client import ApiClient, ApiError
from bot.auth import LinkStore, Role, role_allows
from bot.deps import Deps
from bot.formatting import escape_md
from bot.handlers import callbacks, commands
from bot.handlers.queries import match_intent
from bot.poller import poll_once
from bot.ratelimit import RateLimiter
from bot.tools import PermissionDenied, ToolError, build_default_registry

ADMIN, RES, STRANGER = 1, 2, 3


class FakeApi:
    def __init__(self):
        self.valve_calls = []
        self.acked = []

    async def set_valve(self, unit, action):
        self.valve_calls.append((unit, action))

    async def ack_alert(self, aid):
        self.acked.append(aid)

    async def alerts(self, state=None):
        return [{"id": "a1", "state": state, "unit": "A-101", "message": "leak"}]

    async def top_losses(self, limit=5):
        return [{"unit": "A-101", "litres_lost": 40}]


@pytest.fixture
def deps():
    store = LinkStore({ADMIN})
    store.links[RES] = {"username": "r", "chat_id": 20}
    return Deps(api=FakeApi(), registry=build_default_registry(), store=store, limiter=RateLimiter(1000))


def ctx(deps, args=None):
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}), args=args, bot=bot)


def upd(uid, text=""):
    msg = SimpleNamespace(reply_text=AsyncMock(), text=text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=uid), effective_message=msg
    )


# --- roles & registry --------------------------------------------------------
def test_role_ordering():
    assert role_allows(Role.ADMIN, Role.MAINTENANCE)
    assert not role_allows(Role.RESIDENT, Role.ADMIN)
    assert not role_allows(None, Role.RESIDENT)


async def test_resident_cannot_close_valve(deps):
    with pytest.raises(PermissionDenied):
        await deps.registry.call("set_valve", {"unit": "A-1", "action": "close"}, Role.RESIDENT, deps.api)
    assert deps.api.valve_calls == []


async def test_admin_valve_via_registry(deps):
    await deps.registry.call("set_valve", {"unit": "A-1", "action": "close"}, Role.ADMIN, deps.api)
    assert deps.api.valve_calls == [("A-1", "close")]


def test_schemas_valid():
    reg = build_default_registry()
    schemas = reg.get_tool_schemas()
    assert len(schemas) >= 6
    for s in schemas:
        fn = s["function"]
        assert fn["name"] and fn["description"]
        p = fn["parameters"]
        assert p["type"] == "object"
        assert set(p.get("required", [])) <= set(p["properties"])
        json.dumps(s)
    assert "set_valve" not in [s["function"]["name"] for s in reg.get_tool_schemas(Role.RESIDENT)]


def test_argument_validation(deps):
    reg = deps.registry
    tool = reg.get("set_valve")
    with pytest.raises(ToolError):
        reg.validate(tool, {"unit": "A-1", "action": "explode"})
    with pytest.raises(ToolError):
        reg.validate(tool, {"unit": "A-1"})
    with pytest.raises(ToolError):
        reg.validate(tool, {"unit": "A-1", "action": "open", "x": 1})


def test_duplicate_registration_rejected(deps):
    with pytest.raises(ValueError):
        deps.registry.register(deps.registry.get("top_losses"))


# --- intents -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text,tool",
    [
        ("which flats are leaking", "list_active_alerts"),
        ("show me the top losses", "top_losses"),
        ("top leak losses please", "top_losses"),
        ("water usage today", "get_usage_summary"),
        ("send report", "generate_report"),
    ],
)
def test_intents(text, tool):
    assert match_intent(text)[0] == tool


def test_intent_none():
    assert match_intent("hello there") is None


# --- commands & callbacks -----------------------------------------------------
async def test_valve_command_requires_confirmation(deps):
    u, c = upd(ADMIN), ctx(deps, ["A-1", "close"])
    await commands.valve(u, c)
    assert deps.api.valve_calls == []  # nothing executed yet
    assert u.effective_message.reply_text.call_args.kwargs["reply_markup"]


async def test_valve_command_denied_for_resident(deps):
    u, c = upd(RES), ctx(deps, ["A-1", "close"])
    await commands.valve(u, c)
    assert "not allowed" in u.effective_message.reply_text.call_args.args[0]


async def test_callback_valve_confirm_resident_denied(deps):
    out = await callbacks.dispatch(deps, ctx(deps), RES, Role.RESIDENT, ["valve", "yes", "A-1", "close"])
    assert "not allowed" in out and deps.api.valve_calls == []


async def test_callback_valve_confirm_admin(deps):
    await callbacks.dispatch(deps, ctx(deps), ADMIN, Role.ADMIN, ["valve", "yes", "A-1", "open"])
    assert deps.api.valve_calls == [("A-1", "open")]


async def test_link_approval_requires_admin(deps):
    deps.store.request_link(STRANGER, 30, "s")
    out = await callbacks.dispatch(deps, ctx(deps), RES, Role.RESIDENT, ["link", "approve", str(STRANGER)])
    assert "not allowed" in out
    assert deps.store.role_of(STRANGER) is None
    await callbacks.dispatch(deps, ctx(deps), ADMIN, Role.ADMIN, ["link", "approve", str(STRANGER)])
    assert deps.store.role_of(STRANGER) == Role.RESIDENT  # never elevated


async def test_resident_cannot_ack_alert(deps):
    out = await callbacks.dispatch(deps, ctx(deps), RES, Role.RESIDENT, ["alert", "ack", "a1"])
    assert "not allowed" in out and deps.api.acked == []


async def test_unlinked_user_rejected(deps):
    u, c = upd(STRANGER), ctx(deps)
    await commands.alerts(u, c)
    assert "Not linked" in u.effective_message.reply_text.call_args.args[0]


async def test_poller_pushes_only_new(deps):
    bot = SimpleNamespace(send_message=AsyncMock())
    seen: set[str] = set()
    n1 = await poll_once(bot, deps, seen)
    n2 = await poll_once(bot, deps, seen)
    assert n1 > 0 and n2 == 0


# --- markdown ----------------------------------------------------------------
def test_escape_md():
    assert escape_md("a_b*c[d](e).f-g!") == r"a\_b\*c\[d\]\(e\)\.f\-g\!"
    assert escape_md(r"back\slash") == r"back\\slash"
    assert escape_md(12.5) == r"12\.5"


# --- api client --------------------------------------------------------------
async def test_api_client_refreshes_token_on_401():
    logins = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/auth/login":
            logins["n"] += 1
            return httpx.Response(200, json={"access_token": f"tok{logins['n']}"})
        if req.headers["authorization"] == "Bearer tok1":
            return httpx.Response(401)
        return httpx.Response(200, json=[{"id": "a1"}])

    api = ApiClient("http://x", "u", "p", transport=httpx.MockTransport(handler))
    assert await api.alerts("alert") == [{"id": "a1"}]
    assert logins["n"] == 2
    await api.aclose()


async def test_api_client_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        if req.url.path.endswith("login"):
            return httpx.Response(200, json={"access_token": "t"})
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, json={"ok": 1})

    api = ApiClient("http://x", "u", "p", transport=httpx.MockTransport(handler))
    assert await api.get("/api/v1/thing") == {"ok": 1}


async def test_api_client_raises_on_403():
    def handler(req):
        if req.url.path.endswith("login"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(403)

    api = ApiClient("http://x", "u", "p", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        await api.get("/api/v1/thing")

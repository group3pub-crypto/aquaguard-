import pytest

from bot.auth import Principal, Role
from bot.handlers import callbacks, commands
from bot.handlers.queries import match_intent
from bot.tools import PermissionDenied, ToolError, build_default_registry
from bot.tools.alerts import STATES, fmt_alert
from bot.tools.losses import fmt_losses
from bot.tools.report import fmt_report
from bot.tools.usage import fmt_usage
from tests.support import (
    ADMIN,
    AL1,
    AL2,
    AL3,
    F101,
    F102,
    LOSSES,
    MAINT,
    RES,
    STRANGER,
    USAGE,
    BackendSim,
    alert,
    ctx,
    make_api,
    make_deps,
    upd,
)


@pytest.fixture
def sim():
    return BackendSim([alert(AL1, F101, "alert"), alert(AL2, F102, "auto_shutoff", 4), alert(AL3, F102, "nudge", 1)])


@pytest.fixture
def deps(sim, tmp_path):
    return make_deps(make_api(sim), tmp_path)


def P(uid, deps):
    return deps.store.principal_of(uid)


def test_states_match_backend():
    from tests.support import SNAPSHOT

    enum = next(p for p in SNAPSHOT["paths"]["/api/v1/alerts"]["get"]["parameters"] if p["name"] == "state")
    import re

    assert set(re.findall(r"[a-z_]+", enum["schema"]["anyOf"][0]["pattern"])) == set(STATES)


def test_formatters_real_shapes():
    out = fmt_alert(alert(AL1, F101, "auto_shutoff", 4), "A-101")
    assert out.startswith("[AUTO-SHUTOFF] A-101") and F101 not in out and "tier 4" in out
    u = fmt_usage("A-101", USAGE)
    assert "312.5 L" in u and "280.0 L" in u and "0.217" in u
    assert "n/a" in fmt_usage("A-101", {**USAGE, "expected_l": None})
    assert "1. A-101: 40.0 L" in fmt_losses(LOSSES)
    assert "?" not in fmt_losses(LOSSES)
    r = fmt_report("# Weekly water report: Towers\n_2026-10-09 to 2026-10-16_\n- x")
    assert "#" not in r and "_" not in r


def test_schemas_valid():
    reg = build_default_registry()
    for s in reg.get_tool_schemas():
        p = s["function"]["parameters"]
        assert set(p.get("required", [])) <= set(p["properties"])
    names = [s["function"]["name"] for s in reg.get_tool_schemas(Role.RESIDENT)]
    assert set(names) == {"list_active_alerts", "get_usage_summary"}


def test_validation(deps):
    reg = deps.registry
    with pytest.raises(ToolError):
        reg.validate(reg.get("set_valve"), {"unit": "A-1", "action": "explode"})
    with pytest.raises(ToolError):
        reg.validate(reg.get("acknowledge_alert"), {"alert_id": "not-a-uuid"})
    with pytest.raises(ToolError):
        reg.validate(reg.get("list_active_alerts"), {"state": "watch"})


async def test_resident_sees_only_own_flat(deps):
    out = await deps.registry.call("list_active_alerts", {}, P(RES, deps), deps.api)
    assert "A-101" in out and "A-102" not in out
    with pytest.raises(PermissionDenied):
        await deps.registry.call("list_active_alerts", {"unit": "A-102"}, P(RES, deps), deps.api)
    with pytest.raises(PermissionDenied):
        await deps.registry.call("get_usage_summary", {"unit": "A-102"}, P(RES, deps), deps.api)
    out = await deps.registry.call("get_usage_summary", {}, P(RES, deps), deps.api)
    assert out.startswith("A-101")
    # state-filtered path is also scoped
    out = await deps.registry.call("list_active_alerts", {"state": "auto_shutoff"}, P(RES, deps), deps.api)
    assert "A-102" not in out


async def test_resident_without_flat_binding_denied(deps):
    with pytest.raises(PermissionDenied):
        await deps.registry.call("get_usage_summary", {}, Principal(9, Role.RESIDENT), deps.api)


async def test_resident_cannot_use_staff_tools(deps):
    for name, args in [
        ("top_losses", {}),
        ("generate_report", {}),
        ("system_status", {}),
        ("acknowledge_alert", {"alert_id": AL1}),
        ("set_valve", {"unit": "A-101", "action": "close"}),
    ]:
        with pytest.raises(PermissionDenied):
            await deps.registry.call(name, args, P(RES, deps), deps.api)


async def test_stranger_gets_nothing(deps):
    u, c = upd(STRANGER), ctx(deps)
    await commands.alerts(u, c)
    assert "Not linked" in u.effective_message.reply_text.call_args.args[0]
    with pytest.raises(PermissionDenied):
        await deps.registry.call("list_active_alerts", {}, None, deps.api)
    out = await callbacks.dispatch(deps, STRANGER, ["alert", "ack", AL1])
    assert "not allowed" in out


async def test_maintenance_can_act_but_not_valve(deps, sim):
    m = P(MAINT, deps)
    assert "acknowledged" in await deps.registry.call("acknowledge_alert", {"alert_id": AL1}, m, deps.api)
    await deps.registry.call("snooze_alert", {"alert_id": AL1, "minutes": 30}, m, deps.api)
    await deps.registry.call("resolve_alert", {"alert_id": AL1}, m, deps.api)
    with pytest.raises(PermissionDenied):
        await deps.registry.call("set_valve", {"unit": "A-101", "action": "close"}, m, deps.api)
    out = await deps.registry.call("top_losses", {"days": 7, "limit": 2}, m, deps.api)
    assert "A-101" in out
    out = await deps.registry.call("list_active_alerts", {}, m, deps.api)
    assert "A-102" in out and "[AUTO-SHUTOFF]" in out
    assert "Active alerts: 3" in await deps.registry.call("system_status", {}, m, deps.api)


async def test_actions_send_on_behalf_of_and_no_dead_fields(deps, sim):
    m = P(MAINT, deps)
    out = await deps.registry.call("acknowledge_alert", {"alert_id": AL1}, m, deps.api)
    await deps.registry.call("snooze_alert", {"alert_id": AL1}, m, deps.api)
    await deps.registry.call("resolve_alert", {"alert_id": AL1}, m, deps.api)
    bodies = [b for _, p, _, b in sim.calls if "/alerts/" in p]
    assert bodies == [
        {"on_behalf_of": "telegram:3"},
        {"duration_minutes": 60, "on_behalf_of": "telegram:3"},
        {"on_behalf_of": "telegram:3"},
    ]
    dead = {"acknowledged_by", "snoozed_by", "resolved_by", "overridden_by", "issued_by"}
    assert not any(dead & set(b) for b in bodies)
    # the backend's echo is surfaced, labelled as asserted
    assert "telegram:3" in out and "not verified" in out


async def test_on_behalf_of_is_clamped_to_100_chars(sim):
    api = make_api(sim)
    await api.ack_alert(AL1, "telegram:" + "9" * 200)
    body = next(b for _, p, _, b in sim.calls if p.endswith("/acknowledge"))
    assert len(body["on_behalf_of"]) == 100
    await api.ack_alert(AL1)
    assert [b for _, p, _, b in sim.calls if p.endswith("/acknowledge")][-1] == {}


async def test_alert_callback_writes_bot_audit(deps, tmp_path):
    out = await callbacks.dispatch(deps, MAINT, ["alert", "ack", AL1])
    assert "acknowledged" in out
    line = (tmp_path / "audit.jsonl").read_text()
    assert "telegram:3/maint" in line and AL1 in line
    assert oct((tmp_path / "audit.jsonl").stat().st_mode & 0o777) == "0o600"


# --- valve confirmation --------------------------------------------------------
async def _start_valve(deps, uid=ADMIN):
    u, c = upd(uid), ctx(deps, ["A-101", "close"])
    await commands.valve(u, c)
    kb = u.effective_message.reply_text.call_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":")


async def test_valve_confirm_flow_single_use(deps, sim):
    parts = await _start_valve(deps)
    assert not any(p.endswith("valve-control") for _, p, _, _ in sim.calls)
    out = await callbacks.dispatch(deps, ADMIN, parts, chat_id=ADMIN, message_id=500)
    assert "close" in out
    call = next(b for _, p, _, b in sim.calls if p.endswith("valve-control"))
    assert call["on_behalf_of"] == "telegram:1" and call["flat_id"] == F101
    assert "issued_by" not in call
    assert "telegram:1" in out and "NOT confirmed" in out  # pending command: never "closed"
    again = await callbacks.dispatch(deps, ADMIN, parts, chat_id=ADMIN, message_id=500)
    assert "no longer valid" in again
    assert sum(p.endswith("valve-control") for _, p, _, _ in sim.calls) == 1


async def test_valve_confirm_bound_to_user_chat_message(deps, sim):
    parts = await _start_valve(deps)
    for kw in ({"chat_id": 999, "message_id": 500}, {"chat_id": ADMIN, "message_id": 501}):
        assert "no longer valid" in await callbacks.dispatch(deps, ADMIN, parts, **kw)
    # another user pressing (even a resident) neither executes nor burns it
    assert "no longer valid" in await callbacks.dispatch(deps, RES, parts, chat_id=ADMIN, message_id=500)
    assert not any(p.endswith("valve-control") for _, p, _, _ in sim.calls)
    assert "close" in await callbacks.dispatch(deps, ADMIN, parts, chat_id=ADMIN, message_id=500)


async def test_valve_confirm_expires_after_60s(deps, sim):
    from bot.confirm import ConfirmStore

    now = [0.0]
    deps.confirm = ConfirmStore(clock=lambda: now[0])
    parts = await _start_valve(deps)
    now[0] = 61
    assert "no longer valid" in await callbacks.dispatch(deps, ADMIN, parts, chat_id=ADMIN, message_id=500)
    assert not any(p.endswith("valve-control") for _, p, _, _ in sim.calls)


async def test_valve_command_denied_for_non_admin(deps):
    for uid in (RES, MAINT):
        u = upd(uid)
        await commands.valve(u, ctx(deps, ["A-101", "close"]))
        assert "not allowed" in u.effective_message.reply_text.call_args.args[0]


def test_intents():
    assert match_intent("which flats are leaking")[0] == "list_active_alerts"
    assert match_intent("top leak losses")[0] == "top_losses"
    assert match_intent("water usage of a-101") == ("get_usage_summary", {"unit": "A-101"})
    assert match_intent("my usage") == ("get_usage_summary", {})
    assert match_intent("hello") is None


async def test_override_button_admin_only_and_sends_on_behalf_of(deps, sim):
    assert await callbacks.dispatch(deps, MAINT, ["alert", "override", AL1]) == "You are not allowed to do that."
    assert await callbacks.dispatch(deps, RES, ["alert", "override", AL1]) == "You are not allowed to do that."
    assert not any(p.endswith("/override") for _, p, _, _ in sim.calls)
    out = await callbacks.dispatch(deps, ADMIN, ["alert", "override", AL1])
    assert "overridden" in out and "telegram:1" in out
    body = next(b for _, p, _, b in sim.calls if p.endswith("/override"))
    assert body["on_behalf_of"] == "telegram:1" and body["reason"] and "overridden_by" not in body


async def test_valve_confirmed_status_wording(deps):
    from bot.tools.valve import set_valve

    class Api:
        async def set_valve(self, unit, action, obo):
            return {"status": "confirmed", "on_behalf_of": obo}

    out = await set_valve(Api(), {"unit": "A-101", "action": "close"}, P(ADMIN, deps))
    assert "confirmed by the device" in out and "telegram:1" in out

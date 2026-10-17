import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.poller import SeenStore, poll_once, run_poller
from tests.support import ADMIN, AL1, AL2, AL3, F101, F102, BackendSim, alert, make_api, make_deps

MAINT_CHAT = 30


def bot_mock(fail_for=()):
    async def send_message(chat_id, **kw):
        if chat_id in fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")

    return SimpleNamespace(send_message=AsyncMock(side_effect=send_message))


def sent(bot):
    return [(c.kwargs["chat_id"], c.kwargs["text"]) for c in bot.send_message.call_args_list]


@pytest.fixture
def sim():
    return BackendSim([alert(AL1, F101, "alert"), alert(AL2, F102, "auto_shutoff", 4), alert(AL3, F102, "nudge", 1)])


async def test_auto_shutoff_pushed_with_unit_and_scoped(sim, tmp_path):
    deps = make_deps(make_api(sim), tmp_path)
    bot, seen = bot_mock(), SeenStore()
    await poll_once(bot, deps, seen)
    msgs = sent(bot)
    shutoff = [m for m in msgs if "AUTO\\-SHUTOFF" in m[1]]
    assert {c for c, _ in shutoff} == {ADMIN, MAINT_CHAT}  # resident of A-101 does not get A-102
    assert all("A\\-102" in t and F102 not in t for _, t in shutoff)
    to_res = [t for c, t in msgs if c == 20]
    assert len(to_res) == 1 and "ALERT" in to_res[0] and "A\\-101" in to_res[0]
    # nudge: digest only (no per-alert keyboard), staff only here since it is A-102's
    digests = [(c, t) for c, t in msgs if "nudge" in t]
    assert {c for c, _ in digests} == {ADMIN, MAINT_CHAT}
    n = len(msgs)
    await poll_once(bot, deps, seen)
    assert len(sent(bot)) == n  # nothing re-sent


async def test_escalation_is_pushed_again(sim, tmp_path):
    deps = make_deps(make_api(sim), tmp_path)
    bot, seen = bot_mock(), SeenStore()
    await poll_once(bot, deps, seen)
    sim.alerts[0]["state"] = "ticket"
    before = len(sent(bot))
    await poll_once(bot, deps, seen)
    assert len(sent(bot)) > before


async def test_failed_send_not_marked_seen_then_retried(sim, tmp_path):
    deps = make_deps(make_api(sim), tmp_path)
    deps.store.admin_ids = {ADMIN}
    deps.store.links.clear()
    seen = SeenStore()
    down = bot_mock(fail_for={ADMIN})
    assert await poll_once(down, deps, seen) == 0
    assert f"{AL2}:auto_shutoff" not in seen
    up = bot_mock()
    assert await poll_once(up, deps, seen) > 0
    assert f"{AL2}:auto_shutoff" in seen


async def test_one_blocked_chat_does_not_stop_others(sim, tmp_path):
    deps = make_deps(make_api(sim), tmp_path)
    bot, seen = bot_mock(fail_for={ADMIN}), SeenStore()
    await poll_once(bot, deps, seen)
    assert f"{AL2}:auto_shutoff" in seen
    assert any(c == MAINT_CHAT and "AUTO" in t for c, t in sent(bot))


async def test_seen_persists_across_restart(sim, tmp_path):
    deps = make_deps(make_api(sim), tmp_path)
    path = str(tmp_path / "seen.json")
    await poll_once(bot_mock(), deps, SeenStore(path))
    assert oct((tmp_path / "seen.json").stat().st_mode & 0o777) == "0o600"
    restarted = SeenStore(path)
    assert restarted.loaded and f"{AL2}:auto_shutoff" in restarted
    bot2 = bot_mock()
    await poll_once(bot2, deps, restarted)
    assert sent(bot2) == []


def test_seen_bounded_and_corrupt_file(tmp_path):
    s = SeenStore(cap=3)
    for i in range(5):
        s.add(str(i))
    assert "0" not in s and "4" in s
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert not SeenStore(str(bad)).loaded


async def test_run_poller_seeds_backlog_backs_off_and_survives(sim, tmp_path):
    deps = make_deps(make_api(sim), tmp_path)
    bot = bot_mock()
    delays: list[float] = []
    calls = {"n": 0}

    async def fake_sleep(d):
        delays.append(d)
        calls["n"] += 1
        if calls["n"] == 1:
            deps.api.alerts = AsyncMock(side_effect=RuntimeError("backend down"))
        if calls["n"] == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run_poller(bot, deps, 10, str(tmp_path / "s.json"), sleep=fake_sleep)
    assert sent(bot) == []  # backlog seeded, not replayed
    assert delays == [10, 20, 40]  # exponential backoff after failures
    assert json.loads((tmp_path / "s.json").read_text())["seen"]


# --- shut-off protocol: warning / failed / wording ---------------------------------------------
WARNED = "2026-10-16T10:00:00Z"


def _ticket(aid, flat, **kw):
    return alert(aid, flat, "ticket", 3) | kw


async def test_shutoff_warning_pushed_with_countdown_and_role_buttons(tmp_path):
    from datetime import UTC, datetime

    from bot.tools.alerts import set_warning_seconds

    set_warning_seconds(300)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sim = BackendSim([_ticket(AL1, F101, shutoff_warned_at=now)])
    deps = make_deps(make_api(sim), tmp_path)
    bot, seen = bot_mock(), SeenStore()
    await poll_once(bot, deps, seen)
    warns = {
        c: k
        for c, k in [(c.kwargs["chat_id"], c.kwargs) for c in bot.send_message.call_args_list]
        if "SHUT\\-OFF WARNING" in k["text"]
    }
    assert set(warns) == {ADMIN, MAINT_CHAT, 20}  # resident of A-101 is told too
    assert "NOT been shut off" in warns[20]["text"].replace("\\", "") and "about" in warns[20]["text"]
    labels = lambda c: [b.text for b in warns[c]["reply_markup"].inline_keyboard[0]]  # noqa: E731
    assert "Override" in labels(ADMIN) and any(t.startswith("Acknowledge") for t in labels(ADMIN))
    assert "Override" not in labels(MAINT_CHAT) and labels(MAINT_CHAT)  # override is admin-only
    assert "reply_markup" not in warns[20]  # residents get text only
    n = len(sent(bot))
    await poll_once(bot, deps, seen)
    assert len(sent(bot)) == n  # deduped per warning


async def test_rearmed_warning_is_a_new_event(tmp_path):
    sim = BackendSim([_ticket(AL1, F101, shutoff_warned_at=WARNED)])
    deps = make_deps(make_api(sim), tmp_path)
    bot, seen = bot_mock(), SeenStore()
    await poll_once(bot, deps, seen)
    before = len(sent(bot))
    sim.alerts[0]["shutoff_warned_at"] = "2026-10-16T11:00:00Z"
    await poll_once(bot, deps, seen)
    assert len(sent(bot)) > before


async def test_shutoff_failed_pushed_to_staff_only_and_never_claims_shutoff(tmp_path):
    sim = BackendSim([_ticket(AL1, F101, shutoff_warned_at=WARNED, shutoff_gave_up_at="2026-10-16T10:20:00Z")])
    deps = make_deps(make_api(sim), tmp_path)
    bot, seen = bot_mock(), SeenStore()
    await poll_once(bot, deps, seen)
    failed = [(c, t) for c, t in sent(bot) if "SHUT\\-OFF FAILED" in t]
    assert {c for c, _ in failed} == {ADMIN, MAINT_CHAT}
    assert all("NOT confirmed" in t for _, t in failed)
    assert not any("SHUT\\-OFF WARNING" in t for _, t in sent(bot))  # gave up: no stale countdown
    n = len(sent(bot))
    await poll_once(bot, deps, seen)
    assert len(sent(bot)) == n


async def test_first_run_seeds_warning_keys_without_replay(tmp_path):
    sim = BackendSim([_ticket(AL1, F101, shutoff_warned_at=WARNED)])
    deps = make_deps(make_api(sim), tmp_path)
    bot = bot_mock()

    async def stop(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run_poller(bot, deps, 10, str(tmp_path / "s.json"), sleep=stop)
    assert sent(bot) == []
    assert f"{AL1}:shutoff_warning:{WARNED}" in SeenStore(str(tmp_path / "s.json"))


def test_wording_water_is_only_shut_off_when_state_is_auto_shutoff():
    from bot.tools.alerts import WITHHELD, fmt_alert, safe_message

    claim = "The water supply to your flat has been shut off."
    for state in ("nudge", "alert", "ticket", "acknowledged", "snoozed", "overridden"):
        assert safe_message(state, claim) == WITHHELD
        assert "shut off" not in fmt_alert(alert(AL1, F101, state, msg=claim), "A-101").lower().replace(
            "has not been shut off", ""
        )
    assert safe_message("auto_shutoff", claim) == claim
    assert "confirmed by the device" in fmt_alert(alert(AL1, F101, "auto_shutoff", 4, msg=claim), "A-101")
    for ok in ("Possible leak detected.", "Water will be shut off automatically unless you acknowledge."):
        assert safe_message("ticket", ok) == ok
    for claim2 in ("We have closed the valve.", "Valve is now closed.", "Your water is off."):
        assert safe_message("alert", claim2) == WITHHELD


def test_ticket_with_pending_warning_says_not_shut_off():
    from bot.tools.alerts import fmt_alert

    text = fmt_alert(_ticket(AL1, F101, shutoff_warned_at=WARNED), "A-101")
    assert "NOT been shut off" in text
    gave_up = fmt_alert(_ticket(AL1, F101, shutoff_gave_up_at=WARNED), "A-101")
    assert "FAILED" in gave_up and "NOT confirmed" in gave_up

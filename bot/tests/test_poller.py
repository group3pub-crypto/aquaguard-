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

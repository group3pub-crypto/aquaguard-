import json
import os

import pytest

from bot.auth import MAX_PENDING, InvalidUsername, LinkStore, RateLimited, Role, TooManyPending
from bot.formatting import escape_md
from bot.handlers import commands
from tests.support import ADMIN, F101, F102, RES, STRANGER, USERS, BackendSim, ctx, make_api, make_deps, upd


def test_role_ordering():
    assert Role.ADMIN > Role.MAINTENANCE > Role.RESIDENT


def test_link_requires_valid_username_and_admin_approval(tmp_path):
    s = LinkStore({1}, str(tmp_path / "l.json"))
    with pytest.raises(InvalidUsername):
        s.request_link(5, 5, "x; drop")
    rid = s.request_link(5, 50, "alice")
    assert s.principal_of(5) is None
    with pytest.raises(PermissionError):
        s.approve(2, rid, role=Role.RESIDENT, flat_id=F101, unit="A-101")
    with pytest.raises(ValueError):
        s.approve(1, rid, role=Role.RESIDENT)  # resident must be bound to a flat
    with pytest.raises(PermissionError):
        s.approve(1, rid, role=Role.ADMIN)
    s.approve(1, rid, role=Role.RESIDENT, flat_id=F101, unit="A-101")
    p = s.principal_of(5)
    assert (p.role, p.flat_id, p.unit, p.username) == (Role.RESIDENT, F101, "A-101", "alice")
    with pytest.raises(KeyError):
        s.approve(1, rid, role=Role.MAINTENANCE)


def test_username_cannot_be_linked_twice(tmp_path):
    s = LinkStore({1})
    s.approve(1, s.request_link(5, 5, "alice"), role=Role.MAINTENANCE)
    rid = s.request_link(6, 6, "Alice")
    with pytest.raises(ValueError):
        s.approve(1, rid, role=Role.MAINTENANCE)


def test_link_rate_limit_and_pending_bounds_and_ttl():
    now = [0.0]
    s = LinkStore({1}, clock=lambda: now[0])
    for _ in range(3):
        s.request_link(5, 5, "alice")
    with pytest.raises(RateLimited):
        s.request_link(5, 5, "alice")
    now[0] = 3601
    s.request_link(5, 5, "alice")
    s2 = LinkStore({1}, clock=lambda: now[0])
    for i in range(MAX_PENDING):
        s2.request_link(100 + i, 1, "user" + str(i))
    with pytest.raises(TooManyPending):
        s2.request_link(999, 1, "overflow")
    now[0] += 25 * 3600
    s2.request_link(999, 1, "overflow")  # expired entries purged
    assert len(s2.pending) == 1


def test_store_atomic_0600_and_reload(tmp_path):
    path = tmp_path / "sub" / "l.json"
    s = LinkStore({1}, str(path))
    s.approve(1, s.request_link(5, 50, "alice"), role=Role.RESIDENT, flat_id=F101, unit="A-101")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not os.path.exists(str(path) + ".tmp")
    assert LinkStore({1}, str(path)).principal_of(5).flat_id == F101


def test_legacy_unbound_links_fail_closed(tmp_path):
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"links": {"5": {"username": "old", "chat_id": 5}}}))
    assert LinkStore({1}, str(path)).principal_of(5) is None


async def test_link_and_approve_commands(tmp_path):
    sim = BackendSim()
    deps = make_deps(make_api(sim), tmp_path)
    u, c = upd(STRANGER), ctx(deps, ["carol"])
    await commands.link(u, c)
    rid = next(iter(deps.store.pending))
    note = c.bot.send_message.call_args.kwargs["text"]
    assert rid in note and c.bot.send_message.call_args.kwargs["chat_id"] == ADMIN
    assert "A\\-102" in note  # flat comes from the backend record
    # non-admin cannot approve
    await commands.approve(upd(RES), ctx(deps, [rid]))
    assert deps.store.principal_of(STRANGER) is None
    # admin approves: role + flat come from the backend, telegram_id is written back
    c3 = ctx(deps, [rid])
    await commands.approve(upd(ADMIN), c3)
    p = deps.store.principal_of(STRANGER)
    assert p.role == Role.RESIDENT and p.unit == "A-102" and p.flat_id == F102
    assert c3.bot.send_message.call_args.kwargs["chat_id"] == STRANGER
    assert sim.telegram_ids == {USERS["carol"]["id"]: STRANGER}
    assert deps.store.links[STRANGER]["user_id"] == USERS["carol"]["id"]


async def test_link_unknown_username_is_rejected_without_admin_ping(tmp_path):
    deps = make_deps(make_api(BackendSim()), tmp_path)
    u, c = upd(STRANGER), ctx(deps, ["nobody"])
    await commands.link(u, c)
    assert deps.store.pending == {}
    c.bot.send_message.assert_not_called()
    assert "could not be verified" in u.effective_message.reply_text.call_args.args[0]


@pytest.mark.parametrize("name", ["root", "svc", "nofl"])
async def test_link_ineligible_accounts_rejected(tmp_path, name):
    deps = make_deps(make_api(BackendSim()), tmp_path)
    c = ctx(deps, [name])
    await commands.link(upd(STRANGER), c)
    assert deps.store.pending == {}
    c.bot.send_message.assert_not_called()


async def test_link_account_claimed_by_other_telegram_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(USERS, "carol", {**USERS["carol"], "telegram_id": 999})
    deps = make_deps(make_api(BackendSim()), tmp_path)
    await commands.link(upd(STRANGER), ctx(deps, ["carol"]))
    assert deps.store.pending == {}


async def test_approve_maintenance_from_backend_role_and_stale_account(tmp_path, monkeypatch):
    sim = BackendSim()
    deps = make_deps(make_api(sim), tmp_path)
    await commands.link(upd(STRANGER), ctx(deps, ["mike"]))
    rid = next(iter(deps.store.pending))
    await commands.approve(upd(ADMIN), ctx(deps, [rid]))
    p = deps.store.principal_of(STRANGER)
    assert p.role == Role.MAINTENANCE and p.flat_id is None
    # account deactivated between /link and /approve
    await commands.link(upd(77), ctx(deps, ["carol"]))
    rid2 = next(iter(deps.store.pending))
    monkeypatch.setitem(USERS, "carol", {**USERS["carol"], "is_active": False})
    u = upd(ADMIN)
    await commands.approve(u, ctx(deps, [rid2]))
    assert deps.store.principal_of(77) is None
    assert "inactive" in u.effective_message.reply_text.call_args.args[0]


async def test_approve_survives_failed_telegram_id_writeback(tmp_path):
    sim = BackendSim()
    sim.patch_status = 403  # service account is not an admin
    deps = make_deps(make_api(sim), tmp_path)
    await commands.link(upd(STRANGER), ctx(deps, ["carol"]))
    u = upd(ADMIN)
    await commands.approve(u, ctx(deps, [next(iter(deps.store.pending))]))
    assert deps.store.principal_of(STRANGER) is not None
    assert "could not record" in u.effective_message.reply_text.call_args.args[0]


async def test_old_two_argument_approve_is_refused(tmp_path):
    deps = make_deps(make_api(BackendSim()), tmp_path)
    await commands.link(upd(STRANGER), ctx(deps, ["carol"]))
    rid = next(iter(deps.store.pending))
    await commands.approve(upd(ADMIN), ctx(deps, [rid, "maintenance"]))
    assert deps.store.principal_of(STRANGER) is None


def test_escape_md():
    assert escape_md("a_b*c[d](e).f-g!") == r"a\_b\*c\[d\]\(e\)\.f\-g\!"
    assert escape_md(12.5) == r"12\.5"

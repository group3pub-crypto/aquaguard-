"""WebSocket: single-use tickets, server-side scope filtering, caps, heartbeat, revocation."""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.database import Database
from app.main import create_app
from app.models import User, WsTicket
from app.repositories import UserRepository
from app.scope import Scope
from app.services import auth as auth_svc
from tests.conftest import RESIDENT_PW, add_society, add_user, make_settings

URL = "/api/v1/ws/live"


class Env:
    """App + seeded users, driven from the sync TestClient through its portal."""

    def __init__(self, tmp_path, **kw):
        self.settings = make_settings(tmp_path, ws_max_connections=3, ws_max_per_user=2, **kw)
        self.db = Database("sqlite+aiosqlite://")
        self.app = create_app(self.settings, self.db)
        self.rt = self.app.state.rt

    def run(self, coro_fn, *a):
        return self.client.portal.call(coro_fn, *a)

    def seed(self):
        async def go():
            await self.db.create_all()
            s_a, f_a = await add_society(self.db, "alpha", "Alpha", ["1", "2"])
            s_b, f_b = await add_society(self.db, "beta", "Beta", ["9"])
            self.a, self.fa, self.b, self.fb = s_a, f_a, s_b, f_b
            kw = dict(society_id=s_a.id)
            self.admin = await add_user(self.db, "admin", RESIDENT_PW, "admin")
            self.maint = await add_user(self.db, "maint", RESIDENT_PW, "maintenance", **kw)
            self.res = await add_user(
                self.db, "res", RESIDENT_PW, "resident", flat_id=f_a[0].id, society_id=s_a.id
            )
            self.unbound = await add_user(self.db, "unb", RESIDENT_PW, "resident")

        self.run(go)

    def ticket(self, user, now=None) -> str:
        async def go():
            async with self.db.session() as s:
                u = await UserRepository(s).get(user.id)
                scope = Scope.for_user(u)
                return (await auth_svc.issue_ws_ticket(s, self.settings, u, scope, now))[0]

        return self.run(go)

    def mutate(self, user, **fields):
        async def go():
            async with self.db.session() as s:
                u = await UserRepository(s).get(user.id)
                for k, v in fields.items():
                    setattr(u, k, v)

        self.run(go)

    def broadcast(self, msg, keys=None):
        return self.run(self.rt.ws.broadcast, msg, keys)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    with TestClient(e.app) as client:
        e.client = client
        e.seed()
        yield e


def raw_connect(env, ticket, extra=""):
    return env.client.websocket_connect(f"{URL}?ticket={ticket}{extra}")


@contextlib.contextmanager
def connect(env, ticket, extra=""):
    """Open a socket and wait until the server has registered it (validation is async)."""
    before = env.rt.ws.count
    with raw_connect(env, ticket, extra) as ws:
        for _ in range(100):
            if env.rt.ws.count > before:
                break
            env.client.portal.call(asyncio.sleep, 0.02)
        yield ws


def expect_close(env, ticket, code, extra=""):
    with pytest.raises(WebSocketDisconnect) as exc, raw_connect(env, ticket, extra) as ws:
        ws.receive_text()
    assert exc.value.code == code
    return exc.value


def test_valid_ticket_receives_event_and_answers_ping(env):
    with connect(env, env.ticket(env.admin)) as ws:
        assert env.broadcast({"type": "reading", "flow": 1.5}, [str(env.fa[0].id)]) == 1
        assert ws.receive_json() == {"type": "reading", "flow": 1.5}
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_ticket_is_single_use(env):
    t = env.ticket(env.admin)
    with connect(env, t):
        pass
    expect_close(env, t, 4401)


def test_ticket_is_stored_hashed_not_raw(env):
    t = env.ticket(env.admin)

    async def rows():
        async with env.db.session() as s:
            return [r.token_hash for r in (await s.execute(select(WsTicket))).scalars()]

    hashes = env.run(rows)
    assert hashes == [auth_svc.hash_token(t)] and t not in hashes[0] and len(hashes[0]) == 64


def test_expired_ticket_rejected(env):
    old = datetime.now(UTC) - timedelta(seconds=env.settings.ws_ticket_ttl_s + 5)
    expect_close(env, env.ticket(env.admin, now=old), 4401)


@pytest.mark.parametrize("ticket", ["", "garbage", "x" * 500, "a.b.c"])
def test_missing_or_invalid_ticket_rejected_with_4401(env, ticket):
    expect_close(env, ticket, 4401)
    assert env.rt.ws.count == 0


def test_no_ticket_param_at_all_rejected(env):
    with pytest.raises(WebSocketDisconnect) as exc, env.client.websocket_connect(URL) as ws:
        ws.receive_text()
    assert exc.value.code == 4401


def test_jwt_in_query_string_is_no_longer_accepted(env):
    jwt_token = auth_svc.create_access_token(env.settings, env.admin)[0]
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        env.client.websocket_connect(f"{URL}?token={jwt_token}") as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 4401
    expect_close(env, jwt_token, 4401)  # nor as a "ticket"


def test_ticket_for_a_nonexistent_user_cannot_be_forged(env):
    # random UUID in a self-signed JWT used to be accepted; tickets only exist in the DB
    ghost = type(
        "U", (), {"id": uuid.uuid4(), "username": "g", "role": "admin", "token_version": 0}
    )
    expect_close(env, auth_svc.create_access_token(env.settings, ghost)[0], 4401)


def test_unbound_resident_gets_4403(env):
    expect_close(env, env.ticket(env.unbound), 4403)


def test_events_are_filtered_by_scope_on_the_server(env):
    mine, other_flat_same_society = str(env.fa[0].id), str(env.fa[1].id)
    foreign = str(env.fb[0].id)
    sa, sb = str(env.a.id), str(env.b.id)
    with (
        connect(env, env.ticket(env.res)) as res,
        connect(env, env.ticket(env.maint)) as maint,
        connect(env, env.ticket(env.admin)) as admin,
    ):
        assert env.rt.ws.count == 3
        # event of the resident's own flat: everyone entitled sees it
        assert env.broadcast({"n": 1}, [mine, sa]) == 3
        # another flat of the same society: staff only
        assert env.broadcast({"n": 2}, [other_flat_same_society, sa]) == 2
        # a foreign society: admin only
        assert env.broadcast({"n": 3}, [foreign, sb]) == 1
        # society-wide event without a flat: staff, not the resident
        assert env.broadcast({"n": 4}, [sa]) == 2
        # unkeyed: only unfiltered all-scope clients
        assert env.broadcast({"n": 5}) == 1
        assert [res.receive_json()["n"]] == [1]
        assert [maint.receive_json()["n"] for _ in range(3)] == [1, 2, 4]
        assert [admin.receive_json()["n"] for _ in range(5)] == [1, 2, 3, 4, 5]


def test_client_filters_can_only_narrow_never_widen(env):
    foreign = str(env.fb[0].id)
    mine = str(env.fa[0].id)
    # the resident asks for a foreign flat: nothing leaks; asking for own flat still works
    with connect(env, env.ticket(env.res), f"&flat_id={foreign}") as ws:
        assert env.broadcast({"n": 1}, [foreign, str(env.b.id)]) == 0
        assert env.broadcast({"n": 2}, [mine, str(env.a.id)]) == 0  # filter excludes own flat too
    with connect(env, env.ticket(env.admin), f"&flat_id={mine}") as ws:
        assert env.broadcast({"n": 3}, [str(env.fa[1].id)]) == 0  # narrowed away
        assert env.broadcast({"n": 4}, [mine]) == 1
        assert ws.receive_json() == {"n": 4}


def test_per_user_cap_4429_and_slot_reusable(env):
    with connect(env, env.ticket(env.admin)), connect(env, env.ticket(env.admin)):
        expect_close(env, env.ticket(env.admin), 4429)
        with connect(env, env.ticket(env.maint)):  # another user is unaffected
            pass
    env.client.portal.call(asyncio.sleep, 0.1)
    with connect(env, env.ticket(env.admin)) as ws:
        env.broadcast({"type": "alert"}, [str(env.fa[0].id)])
        assert ws.receive_json()["type"] == "alert"


def test_global_cap_4429(env):
    with (
        connect(env, env.ticket(env.admin)),
        connect(env, env.ticket(env.admin)),
        connect(env, env.ticket(env.maint)),
    ):
        expect_close(env, env.ticket(env.res), 4429)


def test_connection_cleaned_up_on_disconnect(env):
    with connect(env, env.ticket(env.admin)):
        assert env.rt.ws.count == 1
    env.client.portal.call(asyncio.sleep, 0.1)
    assert env.rt.ws.count == 0


def test_deactivated_user_ticket_rejected(env):
    t = env.ticket(env.admin)
    env.mutate(env.admin, is_active=False)
    expect_close(env, t, 4401)


def test_token_version_bump_invalidates_ticket(env):
    t = env.ticket(env.maint)
    env.mutate(env.maint, token_version=5)
    expect_close(env, t, 4401)


def test_scope_change_after_ticket_rejected(env):
    t = env.ticket(env.maint)
    env.mutate(env.maint, society_id=env.b.id)  # moved to another society after the ticket
    expect_close(env, t, 4401)


def test_live_connection_is_dropped_when_user_is_deactivated(tmp_path):
    e = Env(tmp_path)
    e.settings.ws_revalidate_s = 0.2
    with TestClient(e.app) as client:
        e.client = client
        e.seed()
        with connect(e, e.ticket(e.admin)) as ws:
            e.mutate(e.admin, is_active=False)
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(20):
                    ws.receive_text()
        assert exc.value.code == 4401


def test_server_pings_and_idle_clients_are_closed_4408(tmp_path):
    e = Env(tmp_path)
    e.settings.ws_ping_interval_s = 0.1
    e.settings.ws_idle_timeout_s = 0.5
    with TestClient(e.app) as client:
        e.client = client
        e.seed()
        with raw_connect(e, e.ticket(e.admin)) as ws:
            assert ws.receive_json() == {"type": "ping"}
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(20):
                    ws.receive_json()
        assert exc.value.code == 4408


def test_client_traffic_keeps_the_connection_alive(tmp_path):
    e = Env(tmp_path)
    e.settings.ws_ping_interval_s = 5
    e.settings.ws_idle_timeout_s = 0.6
    with TestClient(e.app) as client:
        e.client = client
        e.seed()
        with connect(e, e.ticket(e.admin)) as ws:
            for _ in range(4):
                client.portal.call(asyncio.sleep, 0.3)
                ws.send_text("ping")
                assert ws.receive_text() == "pong"


def test_broken_client_is_dropped_without_affecting_others():
    from app.ws import Client, ConnectionManager

    class Dead:
        async def send_json(self, _):
            raise RuntimeError("socket closed")

    mgr = ConnectionManager(5)
    mgr._clients.append(Client(Dead(), "u", Scope("all")))  # type: ignore[arg-type]
    assert asyncio.run(mgr.broadcast({"x": 1}, ["k"])) == 0
    assert mgr.count == 0


async def test_ticket_endpoint_requires_auth_and_returns_ticket(client, world, db):
    assert (await client.post("/api/v1/auth/ws-ticket")).status_code == 401
    r = await client.post("/api/v1/auth/ws-ticket", headers=world.resident)
    body = r.json()
    assert r.status_code == 200 and set(body) == {"ticket", "expires_in"}
    assert body["expires_in"] == 30 and len(body["ticket"]) >= 43  # >= 256 bits urlsafe
    async with db.session() as s:
        rows = (await s.execute(select(WsTicket))).scalars().all()
    assert len(rows) == 1 and rows[0].scope["kind"] == "flat" and rows[0].used_at is None
    other = await client.post("/api/v1/auth/ws-ticket", headers=world.resident)
    assert other.json()["ticket"] != body["ticket"]


async def test_inactive_user_cannot_get_a_ticket(client, world, db):
    async with db.session() as s:
        (
            await s.execute(select(User).where(User.username == "resident"))
        ).scalar_one().is_active = False
    assert (await client.post("/api/v1/auth/ws-ticket", headers=world.resident)).status_code == 401

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.database import Database
from app.main import create_app
from tests.conftest import make_settings, ws_token

URL = "/api/v1/ws/live"


@pytest.fixture
def ws_app(tmp_path):
    settings = make_settings(tmp_path, ws_max_connections=2)
    return create_app(settings, Database("sqlite+aiosqlite://")), settings


def settle(client, seconds=0.1):
    client.portal.call(asyncio.sleep, seconds)


def test_valid_token_receives_broadcast(ws_app):
    app, settings = ws_app
    with (
        TestClient(app) as client,
        client.websocket_connect(f"{URL}?token={ws_token(settings)}") as ws,
    ):
        sent = client.portal.call(app.state.rt.ws.broadcast, {"type": "reading", "flow": 1.5})
        assert sent == 1
        assert ws.receive_json() == {"type": "reading", "flow": 1.5}
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


@pytest.mark.parametrize("query", ["", "?token=", "?token=garbage", "?token=a.b.c"])
def test_missing_or_invalid_token_rejected_with_4401(ws_app, query):
    app, _ = ws_app
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(URL + query),
    ):
        pass
    assert exc.value.code == 4401
    assert app.state.rt.ws.count == 0


def test_expired_and_foreign_tokens_rejected(ws_app, tmp_path):
    app, settings = ws_app
    expired = ws_token(settings, now=datetime.now(UTC) - timedelta(hours=5))
    foreign = ws_token(make_settings(tmp_path, secret_key="f" * 40))
    for tok in (expired, foreign):
        with (
            TestClient(app) as client,
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(f"{URL}?token={tok}"),
        ):
            pass
        assert exc.value.code == 4401


def test_connection_cleaned_up_on_disconnect(ws_app):
    app, settings = ws_app
    with TestClient(app) as client:
        with client.websocket_connect(f"{URL}?token={ws_token(settings)}"):
            assert app.state.rt.ws.count == 1
        settle(client)
        assert app.state.rt.ws.count == 0


def test_connection_cap_enforced_and_slot_reusable(ws_app):
    app, settings = ws_app
    tok = ws_token(settings)
    with TestClient(app) as client:
        with (
            client.websocket_connect(f"{URL}?token={tok}"),
            client.websocket_connect(f"{URL}?token={tok}"),
        ):
            assert app.state.rt.ws.count == 2
            with (
                pytest.raises(WebSocketDisconnect) as exc,
                client.websocket_connect(f"{URL}?token={tok}"),
            ):
                pass
            assert exc.value.code == 1013
        settle(client)
        with client.websocket_connect(f"{URL}?token={tok}") as ws:
            client.portal.call(app.state.rt.ws.broadcast, {"type": "alert"})
            assert ws.receive_json()["type"] == "alert"


def test_filters_limit_which_messages_are_delivered(ws_app):
    app, settings = ws_app
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    with (
        TestClient(app) as client,
        client.websocket_connect(f"{URL}?token={ws_token(settings)}&flat_id={mine}") as ws,
    ):
        mgr = app.state.rt.ws
        assert client.portal.call(mgr.broadcast, {"n": 1}, other) == 0
        assert client.portal.call(mgr.broadcast, {"n": 2}, [other, mine]) == 1
        assert (
            client.portal.call(mgr.broadcast, {"n": 3}) == 0
        )  # unkeyed messages skip filtered clients
        assert ws.receive_json() == {"n": 2}


def test_broken_client_is_dropped_without_affecting_others():
    from app.ws import Client, ConnectionManager

    class Dead:
        async def send_json(self, _):
            raise RuntimeError("socket closed")

    mgr = ConnectionManager(5)
    mgr._clients.append(Client(Dead()))  # type: ignore[arg-type]
    assert asyncio.run(mgr.broadcast({"x": 1})) == 0
    assert mgr.count == 0

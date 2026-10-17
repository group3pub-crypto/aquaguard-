"""Live event stream.

Flow: POST /api/v1/auth/ws-ticket (Bearer) -> {ticket, expires_in}; then connect to
``/api/v1/ws/live?ticket=<ticket>``. The ticket is single use, lives 30 s and is bound to the
user, their token_version and their data scope; JWTs are NOT accepted on this endpoint (they
would end up in access logs). Close codes: 4401 auth, 4403 empty scope, 4429 connection cap,
4408 idle timeout.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.dependencies import resolve_scope_society, ticket_policy
from app.repositories import UserRepository
from app.services.auth import redeem_ws_ticket, ticket_scope

router = APIRouter(tags=["websocket"])
log = structlog.get_logger(__name__)
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403
WS_IDLE = 4408
WS_TOO_MANY = 4429


def _valid_uuid(v: str | None) -> str | None:
    try:
        return str(uuid.UUID(v)) if v else None
    except ValueError:
        return None


async def _reject(ws: WebSocket, code: int) -> None:
    # accept first so browsers can SEE the close code (an un-accepted close is an HTTP 403)
    try:
        await ws.close(code=code)
    except Exception:
        log.debug("ws_reject_close_failed")


async def _still_valid(rt, user_id: uuid.UUID, tv: int) -> bool:
    async with rt.db.session() as session:
        user = await UserRepository(session).get(user_id)
        return user is not None and user.is_active and user.token_version == tv


@router.websocket("/ws/live", dependencies=[Depends(ticket_policy)])
async def live(
    ws: WebSocket,
    ticket: str | None = None,
    flat_id: str | None = None,
    society_id: str | None = None,
):
    rt = ws.app.state.rt
    s = rt.settings
    await ws.accept()
    try:
        async with rt.db.session() as session:
            row = await redeem_ws_ticket(session, ticket or "")
            user = await UserRepository(session).get(row.user_id) if row else None
            scope = await resolve_scope_society(session, user) if user else None
    except Exception:
        log.exception("ws_ticket_error")
        await _reject(ws, WS_UNAUTHORIZED)
        return
    if (
        row is None
        or user is None
        or scope is None
        or not user.is_active
        or user.token_version != row.token_version
        or scope != ticket_scope(row)
    ):
        await _reject(ws, WS_UNAUTHORIZED)
        return
    if scope.is_empty:
        await _reject(ws, WS_FORBIDDEN)
        return
    uid = str(user.id)
    if rt.ws.full or rt.ws.user_full(uid):
        await _reject(ws, WS_TOO_MANY)
        return

    client = rt.ws.add(ws, uid, scope, [_valid_uuid(flat_id) or "", _valid_uuid(society_id) or ""])
    loop = asyncio.get_running_loop()
    last_rx = loop.time()
    next_ping = last_rx + s.ws_ping_interval_s
    next_check = last_rx + s.ws_revalidate_s
    try:
        while True:
            now = loop.time()
            wait = max(0.05, min(next_ping, next_check, last_rx + s.ws_idle_timeout_s) - now)
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=wait)
            except TimeoutError:
                pass
            else:
                last_rx = loop.time()
                if msg == "ping":
                    await ws.send_text("pong")
            now = loop.time()
            if now - last_rx >= s.ws_idle_timeout_s:
                await ws.close(code=WS_IDLE)
                break
            if now >= next_ping:
                await ws.send_json({"type": "ping"})
                next_ping = now + s.ws_ping_interval_s
            if now >= next_check:
                next_check = now + s.ws_revalidate_s
                if not await _still_valid(rt, user.id, user.token_version):
                    await ws.close(code=WS_UNAUTHORIZED)
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_error")
    finally:
        rt.ws.disconnect(client)

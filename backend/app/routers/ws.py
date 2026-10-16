from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.auth import decode_token

router = APIRouter(tags=["websocket"])
log = structlog.get_logger(__name__)
WS_UNAUTHORIZED = 4401
WS_TRY_LATER = 1013


def _valid_uuid(v: str | None) -> str | None:
    try:
        return str(uuid.UUID(v)) if v else None
    except ValueError:
        return None


@router.websocket("/ws/live")
async def live(
    ws: WebSocket,
    token: str | None = None,
    flat_id: str | None = None,
    society_id: str | None = None,
):
    rt = ws.app.state.rt
    claims = decode_token(rt.settings, token) if token else None
    if claims is None:
        await ws.close(code=WS_UNAUTHORIZED)  # rejected before the handshake is accepted
        return
    if rt.ws.full:
        await ws.close(code=WS_TRY_LATER)
        return
    client = None
    try:
        client = await rt.ws.connect(
            ws, [_valid_uuid(flat_id) or "", _valid_uuid(society_id) or ""]
        )
        while True:
            remaining = (claims.exp - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                await ws.close(code=WS_UNAUTHORIZED)
                break
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=remaining)
            except TimeoutError:
                continue  # loop re-checks token expiry
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_error")
    finally:
        if client is not None:
            rt.ws.disconnect(client)

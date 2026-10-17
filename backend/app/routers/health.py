from __future__ import annotations

import hmac
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app import metrics
from app.dependencies import RT, AdminUser, PublicRoute, Session, metrics_policy
from app.services import detector
from app.migrations import current_revision, head_revision, revision_from_session

router = APIRouter(tags=["health"])


def _mqtt_state(rt) -> str:
    if not rt.settings.mqtt_enabled:
        return "disabled"
    return "connected" if rt.publisher.connected else "disconnected"


async def _detection_state(rt) -> dict:
    if rt.settings.detection_policy != "rules":
        await rt.models.ensure_loaded()
    effective, reason = detector.effective_policy(rt.settings.detection_policy, rt.models)
    return {"policy": rt.settings.detection_policy, "effective": effective, "reason": reason}


async def _session_ok(session) -> bool:
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _mqtt_ok(rt) -> bool:
    return rt.publisher.connected or not rt.settings.mqtt_required


@router.get("/health", dependencies=[PublicRoute])
async def health(rt: RT):
    """Liveness-ish summary for load balancers: NOTHING but ok/degraded is disclosed."""
    ok = await rt.db.ping() and _mqtt_ok(rt)
    return {"status": "ok" if ok else "degraded"}


@router.get("/health/ready", dependencies=[PublicRoute])
async def ready(rt: RT, response: Response):
    """503 unless the DB answers, the schema is at the code's alembic head and (when
    MQTT_REQUIRED) the broker is connected."""
    db_ok = await rt.db.ping()
    migrated = db_ok and (await current_revision(rt.db)) == head_revision()
    mqtt_ok = _mqtt_ok(rt)
    if not (db_ok and migrated and mqtt_ok):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ready"}


@router.get("/health/details")
async def details(rt: RT, admin: AdminUser, session: Session):
    # use the request session: a second connection could not coexist with it on SQLite
    revision = await revision_from_session(session)
    db_ok = revision is not None or await _session_ok(session)
    return {
        "status": "ok" if db_ok else "degraded",
        "version": rt.settings.app_version,
        "uptime_s": round(time.time() - rt.started_at),
        "db": "ok" if db_ok else "down",
        "migration": {
            "current": revision,
            "head": head_revision(),
        },
        "mqtt": _mqtt_state(rt),
        "maintenance_leader": rt.is_leader,
        "ws_connections": rt.ws.count,
        "models": getattr(rt.models, "model_status", None),
        "detection": await _detection_state(rt),
        "ingest": dict(rt.counters),
        "dead_letters": {"count": len(rt.dead_letters), "recent": list(rt.dead_letters)[-10:]},
    }


@router.get("/metrics", dependencies=[Depends(metrics_policy)], include_in_schema=False)
async def prometheus(rt: RT, authorization: Annotated[str | None, Header()] = None):
    """Prometheus exposition. Disabled (404) unless METRICS_ENABLED; then needs the static
    bearer METRICS_TOKEN (constant-time comparison)."""
    s = rt.settings
    if not s.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    supplied = authorization[7:] if authorization and authorization[:7].lower() == "bearer " else ""
    if not hmac.compare_digest(supplied.encode(), s.metrics_token.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return PlainTextResponse(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

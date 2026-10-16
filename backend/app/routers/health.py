from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.dependencies import RT

router = APIRouter(tags=["health"])


def _mqtt_state(rt) -> str:
    if not rt.settings.mqtt_enabled:
        return "disabled"
    return "connected" if rt.publisher.connected else "disconnected"


@router.get("/health")
async def health(rt: RT):
    db_ok = await rt.db.ping()
    return {
        "status": "ok" if db_ok else "degraded",
        "version": rt.settings.app_version,
        "db": "ok" if db_ok else "down",
        "mqtt": _mqtt_state(rt),
        "ingest": dict(rt.counters),
        "dead_letters": {"count": len(rt.dead_letters), "recent": list(rt.dead_letters)[-10:]},
    }


@router.get("/health/ready")
async def ready(rt: RT, response: Response):
    if not await rt.db.ping():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "db": "down"}
    return {"status": "ready", "db": "ok"}

"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import Settings, get_settings
from app.database import Database
from app.middleware.logging import RequestLogMiddleware, configure_logging
from app.middleware.security import SecurityHeadersMiddleware
from app.mqtt.client import MqttService
from app.rate_limit import limiter
from app.routers import alerts, anomalies, auth, demo, flats, health, societies, tools, valves, ws
from app.runtime import NullPublisher, Runtime
from app.services import baseline, ingest
from app.services.detector import ModelStore
from app.services.explainer import get_explainer
from app.ws import ConnectionManager

log = structlog.get_logger(__name__)
API_PREFIX = "/api/v1"
MAINTENANCE_INTERVAL_S = 60
BASELINE_INTERVAL_S = 3600


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    baseline.configure_timezone(settings.timezone)
    limiter.enabled = settings.rate_limit_enabled
    rt = Runtime(
        settings=settings,
        db=db or Database(settings.database_url),
        ws=ConnectionManager(settings.ws_max_connections),
        publisher=NullPublisher(),
        models=ModelStore(settings.model_dir),
        explainer=get_explainer(settings),
        dead_letters=deque(maxlen=settings.dead_letter_size),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if settings.seed_demo_data:
            from app.seed import seed_demo

            async with rt.db.session() as session:
                await seed_demo(session, settings)
        if settings.background_tasks_enabled:
            tasks.append(asyncio.create_task(_maintenance_loop(rt)))
            if settings.mqtt_enabled:
                mqtt = MqttService(
                    settings,
                    lambda t, p: ingest.handle_message(rt, t, p),
                    client_id=settings.mqtt_client_id,
                )
                rt.publisher = mqtt
                tasks.append(asyncio.create_task(mqtt.run()))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await t
            await rt.db.dispose()

    app = FastAPI(
        title="AquaGuard API",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.rt = rt
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    app.include_router(health.router)
    for r in (auth, societies, flats, anomalies, alerts, valves, tools, ws):
        app.include_router(r.router, prefix=API_PREFIX)
    if settings.demo_mode:
        app.include_router(demo.router, prefix=API_PREFIX)
    return app


async def _maintenance_loop(rt: Runtime) -> None:
    from datetime import UTC, datetime

    last_baseline = 0.0
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_S)
        await ingest.run_maintenance(rt)
        if loop.time() - last_baseline >= BASELINE_INTERVAL_S:
            last_baseline = loop.time()
            try:
                await ingest.retrain_baselines(rt, datetime.now(UTC))
            except Exception:
                log.exception("baseline_retrain_failed")


def app_factory() -> FastAPI:
    """uvicorn --factory entry point: `uvicorn app.main:app_factory --factory`."""
    return create_app()

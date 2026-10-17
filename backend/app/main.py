"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import metrics
from app.config import Settings, get_settings
from app.database import Database
from app.leader import LeaderLock
from app.middleware.logging import RequestLogMiddleware, configure_logging
from app.middleware.security import SecurityHeadersMiddleware
from app.mqtt.client import MqttService
from app.rate_limit import limiter
from app.routers import alerts, anomalies, auth, flats, health, societies, tools, users, valves, ws
from app.runtime import NullPublisher, Runtime
from app.services import baseline, ingest, maintenance, retention
from app.services.detector import ModelStore
from app.services.explainer import get_explainer
from app.ws import ConnectionManager

log = structlog.get_logger(__name__)
API_PREFIX = "/api/v1"
BASELINE_INTERVAL_S = 3600
RETENTION_INTERVAL_S = 24 * 3600


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    baseline.configure_timezone(settings.timezone)
    baseline.configure_min_slot_samples(settings.min_slot_samples)
    limiter.configure(settings)
    metrics.instrument_detection()
    rt = Runtime(
        settings=settings,
        db=db or Database(settings.database_url),
        ws=ConnectionManager(settings.ws_max_connections, settings.ws_max_per_user),
        publisher=NullPublisher(),
        models=ModelStore(settings.model_dir, manifest_required=settings.manifest_required),
        explainer=get_explainer(settings),
        dead_letters=deque(maxlen=settings.dead_letter_size),
    )
    metrics.bind(rt)
    leader = LeaderLock(rt.db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        maint: asyncio.Task | None = None
        others: list[asyncio.Task] = []
        if settings.background_tasks_enabled:
            maint = asyncio.create_task(_maintenance_loop(rt, leader, stop))
            if settings.mqtt_enabled:
                mqtt = MqttService(
                    settings,
                    lambda t, p: ingest.handle_message(rt, t, p),
                    client_id=settings.mqtt_client_id,
                )
                rt.publisher = mqtt
                others.append(asyncio.create_task(mqtt.run()))
        else:
            rt.is_leader = True
        try:
            yield
        finally:
            # uvicorn has stopped accepting requests; let an in-flight maintenance cycle (and
            # its transaction) finish - bounded - before cancelling anything
            stop.set()
            if maint is not None:
                done, _ = await asyncio.wait({maint}, timeout=settings.shutdown_grace_s)
                if not done:
                    log.warning("maintenance_shutdown_timeout")
                    maint.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await maint
            for t in others:  # MQTT: cancelling closes the client cleanly
                t.cancel()
            for t in others:
                with suppress(asyncio.CancelledError, Exception):
                    await t
            await rt.ws.close_all()
            await leader.release()
            rt.is_leader = False
            await rt.db.dispose()

    docs = settings.docs_enabled
    app = FastAPI(
        title="AquaGuard API",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.rt = rt

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
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    app.include_router(health.router)
    for r in (auth, users, societies, flats, anomalies, alerts, valves, tools, ws):
        app.include_router(r.router, prefix=API_PREFIX)
    return app


async def _maintenance_loop(rt: Runtime, leader: LeaderLock, stop: asyncio.Event) -> None:
    """Leader-only housekeeping. Each iteration runs to completion; `stop` is only observed
    between iterations so a cycle's transactions are never torn down mid-flight."""
    loop = asyncio.get_running_loop()
    s = rt.settings
    last_baseline = last_retention = float("-inf")
    while not stop.is_set():
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=s.maintenance_interval_s)
        if stop.is_set():
            break
        try:
            rt.is_leader = await leader.acquire()
            if not rt.is_leader:
                continue
            await maintenance.run_cycle(rt)
            if loop.time() - last_baseline >= BASELINE_INTERVAL_S:
                last_baseline = loop.time()
                await maintenance.retrain_baselines(rt, datetime.now(UTC))
            if loop.time() - last_retention >= RETENTION_INTERVAL_S:
                last_retention = loop.time()
                await retention.run_retention(rt.db, s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maintenance_loop_error")


def app_factory() -> FastAPI:
    """uvicorn --factory entry point: `uvicorn app.main:app_factory --factory`."""
    return create_app()

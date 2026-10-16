"""MQTT message ingestion: validation, idempotent storage, detection trigger, sensor health."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from app.models import Alert, Flat, FlowReading
from app.mqtt.topics import ParsedTopic, parse_topic
from app.repositories import (
    AuditRepository,
    BaselineRepository,
    FlatRepository,
    ReadingRepository,
    SensorHealthRepository,
)
from app.runtime import Runtime
from app.schemas.domain import ReadingOut
from app.schemas.mqtt import MAX_PAYLOAD_BYTES, FlowPayload, HeartbeatPayload, ValveAckPayload
from app.services import alert_engine, baseline, detector, valve_control

log = structlog.get_logger(__name__)
LATE_AFTER = timedelta(minutes=2)
OFFLINE_AFTER = timedelta(minutes=5)
EXPECTED_INTERVAL_S = 60.0
M = TypeVar("M", bound=BaseModel)


class IngestError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sensor_status(last_seen: datetime | None, now: datetime) -> str:
    """online <=2 min, late <=5 min, offline beyond that (or never seen)."""
    if last_seen is None:
        return "offline"
    age = now - last_seen
    return "online" if age <= LATE_AFTER else ("late" if age <= OFFLINE_AFTER else "offline")


def _fail(rt: Runtime, counter: str, topic: str, reason: str) -> None:
    rt.counters[counter] += 1
    rt.dead_letter(topic, reason)
    log.warning("ingest_rejected", topic=topic, reason=reason)


async def handle_message(rt: Runtime, topic: str, payload: bytes) -> None:
    """Entry point for every inbound MQTT message. Never raises."""
    try:
        parsed = parse_topic(topic)
        if parsed is None:
            rt.dead_letter(topic, "unrecognised_topic")
            return
        counter = {
            "flow": "flow_failed",
            "heartbeat": "heartbeat_failed",
            "valve_state": "valve_ack_failed",
        }[parsed.kind]
        if len(payload) > MAX_PAYLOAD_BYTES:
            _fail(rt, counter, topic, "oversize_payload")
            return
        try:
            if parsed.kind == "flow":
                await handle_flow(rt, parsed, payload)
            elif parsed.kind == "heartbeat":
                await handle_heartbeat(rt, parsed, payload)
            else:
                await handle_valve_state(rt, parsed, payload)
        except IngestError as exc:
            _fail(rt, counter, topic, exc.reason)
        except Exception as exc:
            log.exception("ingest_unexpected_error", topic=topic)
            _fail(rt, counter, topic, f"error:{type(exc).__name__}")
    except Exception:  # last line of defence for the MQTT loop
        log.exception("ingest_dispatch_failed")


def _parse(model: type[M], payload: bytes, max_future_skew_s: float | None = None) -> M:
    try:
        ctx = {"max_future_skew_s": max_future_skew_s} if max_future_skew_s is not None else None
        return model.model_validate_json(payload, context=ctx)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise IngestError(f"invalid_payload:{'.'.join(map(str, first['loc']))}") from None


async def _resolve_flat(session, parsed: ParsedTopic) -> Flat:
    flat = await FlatRepository(session).get_by_slug_unit(parsed.slug, parsed.unit)
    if flat is None:
        raise IngestError("unknown_flat")
    return flat


async def handle_flow(rt: Runtime, parsed: ParsedTopic, payload: bytes) -> None:
    data = _parse(FlowPayload, payload, rt.settings.mqtt_max_future_skew_s)
    ts = datetime.fromtimestamp(data.ts, UTC)
    now = datetime.now(UTC)
    async with rt.db.session() as session:
        flat = await _resolve_flat(session, parsed)
        reading = FlowReading(
            flat_id=flat.id,
            timestamp=ts,
            flow_rate=data.flow,
            volume=data.vol,
            pressure=data.psi,
            temperature=data.tmp,
            battery_pct=data.bat,
            rssi=data.rssi,
            message_id=data.id.lower(),
            received_at=now,
        )
        if not await ReadingRepository(session).add_idempotent(reading):
            rt.counters["flow_duplicate"] += 1
            return
        await SensorHealthRepository(session).upsert(
            flat.id, last_seen=now, status="online", missed_readings=0
        )
        flat_id, society_id = flat.id, flat.society_id
        out = ReadingOut.model_validate(reading).model_dump(mode="json")
    rt.counters["flow_ok"] += 1
    rt.last_flow_monotonic = time.monotonic()
    await rt.ws.broadcast({"type": "reading", **out}, [str(flat_id), str(society_id)])
    await maybe_detect(rt, flat_id, ts)


async def handle_heartbeat(rt: Runtime, parsed: ParsedTopic, payload: bytes) -> None:
    _parse(HeartbeatPayload, payload)
    async with rt.db.session() as session:
        flat = await _resolve_flat(session, parsed)
        await SensorHealthRepository(session).upsert(
            flat.id, last_seen=datetime.now(UTC), status="online", missed_readings=0
        )
    rt.counters["heartbeat_ok"] += 1


async def handle_valve_state(rt: Runtime, parsed: ParsedTopic, payload: bytes) -> None:
    data = _parse(ValveAckPayload, payload)
    async with rt.db.session() as session:
        flat = await _resolve_flat(session, parsed)
        await valve_control.handle_ack(
            session, rt, flat, data.cmd_id, data.state, datetime.now(UTC)
        )
    rt.counters["valve_ack_ok"] += 1


async def maybe_detect(rt: Runtime, flat_id, now: datetime) -> Alert | None:
    """Debounced per-flat detection on the trailing window. Never raises."""
    mono = time.monotonic()
    if mono - rt.last_detect.get(flat_id, float("-inf")) < rt.settings.detect_debounce_seconds:
        return None
    rt.last_detect[flat_id] = mono
    try:
        async with rt.db.session() as session:
            flat = await FlatRepository(session).get(flat_id)
            if flat is None:
                return None
            outcome = await detector.run_detection(session, rt.models, flat, now)
            if outcome is None:
                return None
            for resolved in outcome.resolved:
                await alert_engine.auto_resolve(session, rt, flat, resolved, now)
            if outcome.anomaly is None:
                return None
            alert = await alert_engine.process_anomaly(session, rt, flat, outcome.anomaly, now)
            if outcome.created:
                await AuditRepository(session).record(
                    entity_type="anomaly",
                    entity_id=outcome.anomaly.id,
                    action="anomaly_detected",
                    actor="system",
                    details={
                        "type": outcome.anomaly.anomaly_type,
                        "severity": outcome.anomaly.severity,
                    },
                    flat_id=flat.id,
                    society_id=flat.society_id,
                )
                await rt.ws.broadcast(
                    {
                        "type": "anomaly",
                        "flat_id": str(flat.id),
                        "anomaly_id": str(outcome.anomaly.id),
                        "anomaly_type": outcome.anomaly.anomaly_type,
                        "severity": outcome.anomaly.severity,
                    },
                    [str(flat.id), str(flat.society_id)],
                )
            return alert
    except Exception:
        log.exception("detection_failed", flat=str(flat_id))
        return None


async def refresh_sensor_health(rt: Runtime, now: datetime) -> int:
    """Mark sensors late/offline from last_seen. Returns number of status changes."""
    changed = 0
    async with rt.db.session() as session:
        flats = FlatRepository(session)
        for row in await SensorHealthRepository(session).all():
            status = sensor_status(row.last_seen, now)
            row.missed_readings = (
                int((now - row.last_seen).total_seconds() // EXPECTED_INTERVAL_S)
                if row.last_seen
                else 0
            )
            if status != row.status:
                row.status = status
                changed += 1
                flat = await flats.get(row.flat_id)
                if flat:
                    await rt.ws.broadcast(
                        {"type": "sensor", "flat_id": str(flat.id), "status": status},
                        [str(flat.id), str(flat.society_id)],
                    )
    return changed


async def retrain_baselines(
    rt: Runtime, now: datetime, max_age: timedelta = timedelta(hours=24)
) -> int:
    n = 0
    async with rt.db.session() as session:
        for flat in await FlatRepository(session).find(limit=10_000):
            active = await BaselineRepository(session).active(flat.id)
            if active and active.trained_to and now - active.trained_to < max_age:
                continue
            if await ReadingRepository(session).count(flat.id) < 60:
                continue
            await baseline.train_baseline(session, flat, now=now)
            n += 1
    return n


async def run_maintenance(rt: Runtime, now: datetime | None = None) -> None:
    """Periodic housekeeping: escalations, valve ack timeouts, sensor health. Never raises."""
    now = now or datetime.now(UTC)
    try:
        async with rt.db.session() as session:
            await alert_engine.run_escalations(session, rt, now)
            await valve_control.expire_pending(session, now)
            for flat in await FlatRepository(session).find(limit=10_000):
                await baseline.refresh_status(session, flat, now)
        await refresh_sensor_health(rt, now)
    except Exception:
        log.exception("maintenance_failed")

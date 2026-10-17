"""MQTT message ingestion: validation, idempotent storage, detection trigger, sensor health."""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID

import structlog
from pydantic import BaseModel, ValidationError

from app.models import Alert, Flat, FlowReading
from app.mqtt.topics import ParsedTopic, parse_topic
from app.repositories import (
    AuditRepository,
    FlatRepository,
    ReadingRepository,
    SensorHealthRepository,
)
from app.runtime import Runtime
from app.schemas.domain import ReadingOut
from app.schemas.mqtt import MAX_PAYLOAD_BYTES, FlowPayload, HeartbeatPayload, ValveAckPayload
from app.services import alert_engine, detector, device_status, valve_control
from app.services.batcher import ReadingBatcher

log = structlog.get_logger(__name__)
LATE_AFTER = timedelta(minutes=2)
OFFLINE_AFTER = timedelta(minutes=5)
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
        if not _allow(rt, parsed):
            rt.counters["rate_limited"] += 1
            if rt.counters["rate_limited"] % 100 == 1:  # keep the dead-letter ring useful
                rt.dead_letter(topic, "rate_limited")
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


def _allow(rt: Runtime, parsed: ParsedTopic) -> bool:
    """Token bucket per device: refill mqtt_device_rate_per_min/min, burst max(10, rate/4)."""
    rate = rt.settings.mqtt_device_rate_per_min
    if rate <= 0:
        return True
    key = (parsed.slug, parsed.unit)
    now = time.monotonic()
    cap = max(10.0, rate / 4)
    tokens, last = rt.rate_buckets.get(key, (cap, now))
    tokens = min(cap, tokens + (now - last) * rate / 60.0)
    if len(rt.rate_buckets) >= FLAT_CACHE_MAX:
        rt.rate_buckets.clear()
    if tokens < 1.0:
        rt.rate_buckets[key] = (tokens, now)
        return False
    rt.rate_buckets[key] = (tokens - 1.0, now)
    return True


def check_reading_age(rt: Runtime, ts: datetime, now: datetime) -> None:
    """Reject stale readings. (Too-far-future ones are rejected by the payload validator.)"""
    max_age = rt.settings.mqtt_max_reading_age_s
    if (now - ts).total_seconds() > max_age:
        rt.counters["stale_reading"] += 1
        raise IngestError("stale_reading")


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


FLAT_CACHE_TTL_S = 300.0
FLAT_CACHE_MAX = 100_000


async def _flat_ids(rt: Runtime, session, parsed: ParsedTopic) -> tuple[UUID, UUID]:
    """(flat_id, society_id) for a topic; cached briefly so the hot path skips the join."""
    key = (parsed.slug, parsed.unit)
    hit = rt.flat_cache.get(key)
    now = time.monotonic()
    if hit and hit[2] > now:
        return hit[0], hit[1]
    flat = await _resolve_flat(session, parsed)
    if len(rt.flat_cache) >= FLAT_CACHE_MAX:
        rt.flat_cache.clear()
    rt.flat_cache[key] = (flat.id, flat.society_id, now + FLAT_CACHE_TTL_S)
    return flat.id, flat.society_id


async def handle_flow(rt: Runtime, parsed: ParsedTopic, payload: bytes) -> None:
    data = _parse(FlowPayload, payload, rt.settings.mqtt_max_future_skew_s)
    ts = datetime.fromtimestamp(data.ts, UTC)
    now = datetime.now(UTC)
    check_reading_age(rt, ts, now)
    try:
        hit = rt.flat_cache.get((parsed.slug, parsed.unit))
        if hit and hit[2] > time.monotonic():
            flat_id, society_id = hit[0], hit[1]
        else:
            async with rt.db.session() as session:
                flat_id, society_id = await _flat_ids(rt, session, parsed)
        values = {
            "flat_id": flat_id,
            "timestamp": ts,
            "flow_rate": data.flow,
            "volume": data.vol,
            "pressure": data.psi,
            "temperature": data.tmp,
            "battery_pct": None if data.bat is None else round(data.bat),
            "rssi": data.rssi,
            "message_id": data.id.lower(),
            "received_at": now,
            "total_volume": data.tot,
            "battery_volts": data.vbat,
            "device_valve_state": data.valve,
            "firmware_version": data.fw,
            "fault_codes": data.err,
        }
        if rt.batcher is None:
            rt.batcher = ReadingBatcher(rt)
        reading_id = await rt.batcher.add(values)
        if reading_id is None:
            rt.counters["flow_duplicate"] += 1
            return
    except Exception:
        rt.flat_cache.pop((parsed.slug, parsed.unit), None)  # e.g. flat deleted: re-resolve
        raise
    rt.counters["flow_ok"] += 1
    rt.last_flow_monotonic = time.monotonic()
    if any(v is not None for v in (data.tot, data.vbat, data.valve, data.fw, data.err, data.bat)):
        await device_status.apply_report(
            rt,
            flat_id,
            society_id,
            min(ts, now),
            fw=data.fw,
            bat=data.bat,
            vbat=data.vbat,
            tot=data.tot,
            valve=data.valve,
            err=list(data.err or []),
        )
    out = ReadingOut.model_validate({"id": reading_id, **values}).model_dump(mode="json")
    await rt.ws.broadcast({"type": "reading", **out}, [str(flat_id), str(society_id)])
    # detection runs on wall-clock time; the reading's own ts only counts when it is not in the
    # future beyond the configured skew, so a stale or forged ts can never move the window
    horizon = now + timedelta(seconds=rt.settings.mqtt_max_future_skew_s)
    await maybe_detect(rt, flat_id, max(now, min(ts, horizon)))


async def handle_heartbeat(rt: Runtime, parsed: ParsedTopic, payload: bytes) -> None:
    data = _parse(HeartbeatPayload, payload)
    now = datetime.now(UTC)
    async with rt.db.session() as session:
        flat = await _resolve_flat(session, parsed)
        await SensorHealthRepository(session).upsert(
            flat.id, last_seen=now, status="online", missed_readings=0
        )
        flat_id, society_id = flat.id, flat.society_id
    if data.fw is not None or data.bat is not None:
        await device_status.apply_report(
            rt,
            flat_id,
            society_id,
            min(datetime.fromtimestamp(data.ts, UTC), now),
            fw=data.fw,
            bat=data.bat,
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
    debounce = rt.settings.detect_debounce_seconds
    if mono - rt.last_detect.get(flat_id, float("-inf")) < debounce:
        return None
    if rt.detect_inflight >= rt.settings.detect_max_concurrency:
        rt.counters["detect_shed"] += 1  # overloaded: try again on this flat's next reading
        return None
    # jitter (up to +25%) so flats that started together do not all re-trigger in one burst
    rt.last_detect[flat_id] = mono + random.uniform(0.0, 0.25) * debounce
    rt.detect_inflight += 1
    rt.counters["detect_run"] += 1
    try:
        async with rt.db.session() as session:
            flat = await FlatRepository(session).get(flat_id)
            if flat is None:
                return None
            outcome = await detector.run_detection(
                session, rt.models, flat, now, policy=rt.settings.detection_policy
            )
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
    finally:
        rt.detect_inflight -= 1


async def backfill_readings(
    session,
    flat: Flat,
    rows: list[tuple[datetime, float, float, str]],
    *,
    actor: str,
) -> int:
    """ADMIN-ONLY history import (never reachable from MQTT): store (timestamp, flow_rate,
    volume, message_id) rows idempotently, skipping the live-ingest age window and detection.
    The caller must have authorised ``actor`` as an admin. Returns the number of new rows."""
    repo = ReadingRepository(session)
    added = 0
    for ts, flow, vol, mid in rows:
        reading = FlowReading(
            flat_id=flat.id,
            timestamp=ts,
            flow_rate=flow,
            volume=vol,
            message_id=mid.lower(),
            received_at=datetime.now(UTC),
        )
        added += int(await repo.add_idempotent(reading))
    await AuditRepository(session).record(
        entity_type="flat",
        entity_id=flat.id,
        action="readings_backfilled",
        actor=actor,
        details={"rows": len(rows), "added": added},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    return added

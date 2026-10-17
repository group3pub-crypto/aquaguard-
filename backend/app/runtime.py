"""Process-wide runtime container stored on app.state.rt."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.config import Settings
from app.database import Database
from app.ws import ConnectionManager


class Publisher(Protocol):
    connected: bool

    async def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


class NullPublisher:
    """Used when MQTT is disabled; never connected, publishing raises."""

    connected = False

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        raise ConnectionError("MQTT is not connected")


@dataclass
class Runtime:
    settings: Settings
    db: Database
    ws: ConnectionManager
    publisher: Publisher
    models: Any  # app.services.detector.ModelStore
    explainer: Any  # app.services.explainer.Explainer
    dead_letters: deque = field(default_factory=deque)
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "flow_ok": 0,
            "flow_duplicate": 0,
            "flow_failed": 0,
            "heartbeat_ok": 0,
            "heartbeat_failed": 0,
            "valve_ack_ok": 0,
            "valve_ack_failed": 0,
            "rate_limited": 0,
            "stale_reading": 0,
            "detect_run": 0,
            "detect_shed": 0,
        }
    )
    last_detect: dict[UUID, float] = field(default_factory=dict)
    # (slug, unit) -> (flat_id, society_id, expires_monotonic): saves a join per MQTT message
    flat_cache: dict[tuple[str, str], tuple[UUID, UUID, float]] = field(default_factory=dict)
    last_flow_monotonic: float | None = None
    started_at: float = field(default_factory=time.time)
    is_leader: bool = False  # holds the maintenance advisory lock (always True without PG)
    detect_inflight: int = 0
    batcher: Any = None  # app.services.batcher.ReadingBatcher, created on first reading
    # (slug, unit) -> (tokens, last_refill_monotonic): per-device ingest rate limit
    rate_buckets: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)

    def dead_letter(self, topic: str, reason: str) -> None:
        self.dead_letters.append(
            {"at": datetime.now(UTC).isoformat(), "topic": topic[:200], "reason": reason[:200]}
        )

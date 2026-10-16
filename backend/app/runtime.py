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
class DemoState:
    active_faults: dict[UUID, str] = field(default_factory=dict)
    speed: float = 1.0


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
            "flow_ok": 0, "flow_duplicate": 0, "flow_failed": 0, "heartbeat_ok": 0,
            "heartbeat_failed": 0, "valve_ack_ok": 0, "valve_ack_failed": 0,
        }
    )
    demo: DemoState = field(default_factory=DemoState)
    last_detect: dict[UUID, float] = field(default_factory=dict)
    last_flow_monotonic: float | None = None

    def dead_letter(self, topic: str, reason: str) -> None:
        self.dead_letters.append(
            {"at": datetime.now(UTC).isoformat(), "topic": topic[:200], "reason": reason[:200]}
        )

    def simulator_running(self, window_s: float = 30.0) -> bool:
        return (
            self.last_flow_monotonic is not None
            and time.monotonic() - self.last_flow_monotonic < window_s
        )

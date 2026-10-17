"""Prometheus metrics (served by GET /metrics when METRICS_ENABLED=true).

Process-wide registry; gauges and the ingest counters are read from the bound Runtime at scrape
time so there is a single source of truth (`rt.counters`, the WS manager, the MQTT service).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

registry = CollectorRegistry(auto_describe=True)

DETECTION_SECONDS = Histogram(
    "detection_seconds",
    "Wall time of one anomaly detection pass",
    registry=registry,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
ALERTS_TOTAL = Counter(
    "alerts_total", "Alerts created or escalated, by resulting tier", ["tier"], registry=registry
)
for _tier in ("1", "2", "3", "4"):
    ALERTS_TOTAL.labels(tier=_tier)

_bound: dict[str, Any] = {"rt": None}


def bind(rt: Any) -> None:
    """Point the scrape-time collector at the live runtime (latest app wins)."""
    _bound["rt"] = rt


class _RuntimeCollector(Collector):
    def collect(self) -> Iterable[Any]:
        rt = _bound["rt"]
        ingest = CounterMetricFamily(
            "ingest_messages", "MQTT messages handled, by outcome", labels=["result"]
        )
        ws = GaugeMetricFamily("ws_connections", "Open WebSocket connections")
        mqtt = GaugeMetricFamily("mqtt_connected", "1 when the MQTT client is connected")
        leader = GaugeMetricFamily(
            "maintenance_leader", "1 when this instance runs the maintenance loop"
        )
        if rt is not None:
            for result, n in sorted(rt.counters.items()):
                ingest.add_metric([result], float(n))
            ws.add_metric([], float(rt.ws.count))
            mqtt.add_metric([], 1.0 if rt.publisher.connected else 0.0)
            leader.add_metric([], 1.0 if getattr(rt, "is_leader", False) else 0.0)
        else:
            ws.add_metric([], 0.0)
            mqtt.add_metric([], 0.0)
            leader.add_metric([], 0.0)
        yield from (ingest, ws, mqtt, leader)


registry.register(_RuntimeCollector())


def instrument_detection() -> None:
    """Time `detector.run_detection` without touching the detector module (idempotent)."""
    import functools
    import time

    from app.services import detector

    original = detector.run_detection
    if getattr(original, "_timed", False):
        return

    @functools.wraps(original)
    async def timed(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return await original(*args, **kwargs)
        finally:
            DETECTION_SECONDS.observe(time.perf_counter() - t0)

    timed._timed = True  # type: ignore[attr-defined]
    detector.run_detection = timed

"""Publisher-side MQTT load test.

Simulates N smart meters publishing telemetry for T seconds and reports the
achieved publish rate and QoS-1 publish latency (time until PUBACK from the
broker). This measures the *broker + network + publisher* path only. It does
NOT measure backend ingestion, DB write throughput or end-to-end alert latency.

Usage:
  MQTT_USERNAME=... MQTT_PASSWORD=... \
  python infra/loadtest/load_test.py --meters 500 --duration 30 --interval 1.0
Requires: pip install aiomqtt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


async def meter(client, society: str, idx: int, interval: float, end: float,
                latencies: list[float], errors: list[int]) -> int:
    topic = f"aquaguard/{society}/load-{idx:04d}/telemetry"
    sent = 0
    await asyncio.sleep(random.random() * interval)  # de-synchronise meters
    while time.monotonic() < end:
        payload = json.dumps({
            "device_id": f"load-{idx:04d}",
            "ts": time.time(),
            "flow_lpm": round(random.uniform(0, 12), 2),
            "pressure_kpa": round(random.uniform(180, 320), 1),
        })
        t0 = time.perf_counter()
        try:
            await client.publish(topic, payload, qos=1)
            latencies.append((time.perf_counter() - t0) * 1000)
            sent += 1
        except Exception:  # noqa: BLE001 - count and continue
            errors[0] += 1
        await asyncio.sleep(interval)
    return sent


async def main() -> None:
    import aiomqtt

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.getenv("MQTT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    ap.add_argument("--meters", type=int, default=500)
    ap.add_argument("--duration", type=float, default=30.0, help="seconds")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between msgs per meter")
    ap.add_argument("--society", default="loadtest")
    args = ap.parse_args()

    user, pwd = os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD")
    if not user or not pwd:
        raise SystemExit("set MQTT_USERNAME and MQTT_PASSWORD")

    latencies: list[float] = []
    errors = [0]
    start = time.monotonic()
    end = start + args.duration
    async with aiomqtt.Client(args.host, args.port, username=user, password=pwd,
                              identifier=f"aqg-load-{os.getpid()}") as client:
        counts = await asyncio.gather(*[
            meter(client, args.society, i, args.interval, end, latencies, errors)
            for i in range(args.meters)
        ])
    elapsed = time.monotonic() - start
    total = sum(counts)
    print(f"meters={args.meters} duration={elapsed:.1f}s target={args.meters / args.interval:.0f} msg/s")
    print(f"published={total} errors={errors[0]} achieved={total / elapsed:.1f} msg/s")
    print("publish latency (ms, QoS1 PUBACK): "
          f"p50={percentile(latencies, 50):.2f} p95={percentile(latencies, 95):.2f} "
          f"p99={percentile(latencies, 99):.2f}")
    print("NOTE: publisher-side only; backend ingestion is not measured.")


if __name__ == "__main__":
    asyncio.run(main())

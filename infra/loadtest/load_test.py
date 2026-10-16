"""MQTT -> backend -> PostgreSQL load test.

Simulates N smart meters publishing QoS-1 flow readings for T seconds. Without ``--db-url`` it
measures the publish side only (broker + network + publisher, PUBACK latency). With ``--db-url``
it also measures what actually matters end to end:

* provisions the ``--society`` and its N flats if missing (``--provision``, implied by --db-url),
* counts the rows that really landed in ``flow_readings`` (msgs/s ingested, drain time),
* p50/p95/p99 latency from *publish* to *row visible to a SQL reader* (50 ms polling),
* CPU use of the backend process (``--backend-pid``, from /proc; % of one core).

Usage:
  MQTT_USERNAME=... MQTT_PASSWORD=... DATABASE_URL=postgresql+asyncpg://... \\
  python infra/loadtest/load_test.py --meters 500 --duration 45 --interval 1.0 \\
      --db-url "$DATABASE_URL" --backend-pid $(pgrep -f 'uvicorn app.main')
Requires: pip install aiomqtt asyncpg
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
import uuid


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def cpu_seconds(pid: int) -> float:
    """utime+stime of a process in seconds (Linux /proc)."""
    with open(f"/proc/{pid}/stat") as fh:
        fields = fh.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def unit(idx: int) -> str:
    return f"L-{idx:04d}"


async def provision(dsn: str, society: str, meters: int) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        sid = await conn.fetchval("SELECT id FROM societies WHERE slug=$1", society)
        if sid is None:
            sid = uuid.uuid4()
            await conn.execute(
                "INSERT INTO societies (id, name, slug, total_flats) VALUES ($1,$2,$3,$4)",
                sid, f"Load test {society}", society, meters,
            )  # fmt: skip
        have = {
            r["unit_number"]
            for r in await conn.fetch(
                "SELECT unit_number FROM flats WHERE society_id=$1", sid
            )
        }
        rows = [
            (uuid.uuid4(), sid, unit(i), f"{society}-{unit(i)}")
            for i in range(meters)
            if unit(i) not in have
        ]
        if rows:
            await conn.executemany(
                "INSERT INTO flats (id, society_id, unit_number, sensor_id) VALUES ($1,$2,$3,$4)",
                rows,
            )
        print(f"provisioned society={society} flats={meters} (new={len(rows)})")
    finally:
        await conn.close()


async def meter(client, society, idx, interval, end, sent_at, errors) -> int:
    topic = f"aquaguard/{society}/{unit(idx)}/flow"
    sent = 0
    await asyncio.sleep(random.random() * interval)  # de-synchronise meters
    while time.monotonic() < end:
        mid = f"{random.getrandbits(32):08x}"
        now = time.time()
        payload = json.dumps(
            {
                "v": 1, "id": mid, "ts": now, "flow": round(random.uniform(0, 12), 2),
                "vol": round(random.uniform(0, 500), 1), "psi": None, "tmp": None,
                "bat": None, "rssi": None,
            }
        )  # fmt: skip
        sent_at[(idx, mid)] = now
        try:
            await client.publish(topic, payload, qos=1)
            sent += 1
        except Exception:  # noqa: BLE001 - count and continue
            errors[0] += 1
        await asyncio.sleep(interval)
    return sent


async def poller(dsn, society, sent_at, seen_lat, stop: asyncio.Event) -> None:
    """Record when each published message first becomes visible to a SQL reader."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    units = {}
    try:
        for r in await conn.fetch(
            "SELECT f.id, f.unit_number FROM flats f JOIN societies s ON s.id=f.society_id "
            "WHERE s.slug=$1",
            society,
        ):
            units[r["id"]] = int(r["unit_number"].split("-")[1])
        watermark = await conn.fetchval("SELECT coalesce(max(id),0) FROM flow_readings")
        seen_ids: set[int] = set()
        while True:
            rows = await conn.fetch(
                "SELECT id, flat_id, message_id FROM flow_readings WHERE id > $1",
                watermark - 3000,
            )
            t = time.time()
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                watermark = max(watermark, r["id"])
                key = (units.get(r["flat_id"]), r["message_id"])
                if key in sent_at:
                    seen_lat.append(t - sent_at[key])
            if stop.is_set():
                return
            await asyncio.sleep(0.05)
    finally:
        await conn.close()


async def main() -> None:
    import aiomqtt

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default=os.getenv("MQTT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    ap.add_argument("--meters", type=int, default=500)
    ap.add_argument("--duration", type=float, default=30.0, help="seconds")
    ap.add_argument(
        "--interval", type=float, default=1.0, help="seconds between msgs per meter"
    )
    ap.add_argument("--society", default="loadtest", help="society slug")
    ap.add_argument(
        "--connections", type=int, default=5, help="MQTT connections to spread over"
    )
    ap.add_argument(
        "--db-url", default=None, help="enable DB-side measurement + provisioning"
    )
    ap.add_argument(
        "--backend-pid", type=int, default=None, help="measure this process' CPU"
    )
    ap.add_argument("--drain-timeout", type=float, default=60.0)
    args = ap.parse_args()

    user, pwd = os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD")
    if not user or not pwd:
        raise SystemExit("set MQTT_USERNAME and MQTT_PASSWORD")
    dsn = (
        args.db_url.replace("postgresql+asyncpg://", "postgresql://")
        if args.db_url
        else None
    )
    if dsn:
        await provision(dsn, args.society, args.meters)

    sent_at: dict[tuple[int, str], float] = {}
    seen_lat: list[float] = []
    errors = [0]
    stop_poll = asyncio.Event()
    poll_task = (
        asyncio.create_task(poller(dsn, args.society, sent_at, seen_lat, stop_poll))
        if dsn
        else None
    )
    cpu0 = cpu_seconds(args.backend_pid) if args.backend_pid else None
    rows0 = None
    if dsn:
        import asyncpg

        c = await asyncpg.connect(dsn)
        rows0 = await c.fetchval("SELECT count(*) FROM flow_readings")
        await c.close()

    start = time.monotonic()
    end = start + args.duration
    clients = []
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        for k in range(args.connections):
            clients.append(
                await stack.enter_async_context(
                    aiomqtt.Client(
                        args.host,
                        args.port,
                        username=user,
                        password=pwd,
                        identifier=f"aqg-load-{os.getpid()}-{k}",
                    )
                )
            )
        counts = await asyncio.gather(
            *[
                meter(
                    clients[i % len(clients)],
                    args.society,
                    i,
                    args.interval,
                    end,
                    sent_at,
                    errors,
                )
                for i in range(args.meters)
            ]
        )
    elapsed = time.monotonic() - start
    total = sum(counts)
    cpu1 = cpu_seconds(args.backend_pid) if args.backend_pid else None
    print(
        f"meters={args.meters} duration={elapsed:.1f}s target={args.meters / args.interval:.0f} msg/s"
    )
    print(
        f"published={total} errors={errors[0]} publish_rate={total / elapsed:.1f} msg/s"
    )

    if dsn:
        import asyncpg

        c = await asyncpg.connect(dsn)
        t_drain0 = time.monotonic()
        landed = 0
        while time.monotonic() - t_drain0 < args.drain_timeout:
            landed = await c.fetchval("SELECT count(*) FROM flow_readings") - rows0
            if landed >= total:
                break
            await asyncio.sleep(0.25)
        drain = time.monotonic() - t_drain0
        await asyncio.sleep(0.2)
        stop_poll.set()
        await poll_task
        await c.close()
        print(
            f"rows landed in flow_readings: {landed}/{total} "
            f"({100 * landed / max(total, 1):.1f}%), drain after last publish: {drain:.1f}s"
        )
        print(
            f"ingest throughput: {landed / (elapsed + drain):.1f} msg/s over publish+drain window"
        )
        print(
            f"publish->row-visible latency (s, n={len(seen_lat)}): "
            f"p50={percentile(seen_lat, 50):.3f} p95={percentile(seen_lat, 95):.3f} "
            f"p99={percentile(seen_lat, 99):.3f} max={max(seen_lat, default=float('nan')):.3f}"
        )
    if cpu0 is not None and cpu1 is not None:
        print(f"backend CPU: {(cpu1 - cpu0):.1f} cpu-s over {elapsed:.1f}s = "
              f"{100 * (cpu1 - cpu0) / elapsed:.0f}% of one core (publish window only)")  # fmt: skip
    if not dsn:
        print("NOTE: publisher-side only; pass --db-url to measure backend ingestion.")


if __name__ == "__main__":
    asyncio.run(main())

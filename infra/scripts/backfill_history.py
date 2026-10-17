"""Back-fill realistic flow history so baselines can be trained (demo / integration tests).

Generates per-minute readings with the SAME household model the edge simulator uses (so a live
simulator run is consistent with the learned baseline) and bulk-loads them into PostgreSQL with
COPY. Message ids start at 10_000_000 so they never collide with the live simulator's ids.

  DATABASE_URL=postgresql+asyncpg://... python infra/scripts/backfill_history.py \
      --society green-valley --units 101,102,201,202,301,302 --days 15

The flats must already exist (start the backend once with SEED_DEMO_DATA=true, or onboard them).
Requires: asyncpg, numpy, pydantic (edge package importable: `pip install -e edge`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import asyncpg
from edge.simulator.profiles import make_profiles
from edge.simulator.simulator import FlatSimulator


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--society", default="green-valley")
    ap.add_argument("--units", default="101,102,201,202,301,302")
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--seed", type=int, default=42, help="must equal the live simulator's --seed")
    ap.add_argument("--end", type=float, default=None, help="epoch seconds (default: now)")
    args = ap.parse_args()
    if not args.database_url:
        raise SystemExit("set DATABASE_URL or --database-url")
    dsn = args.database_url.replace("postgresql+asyncpg://", "postgresql://")
    units = [u.strip() for u in args.units.split(",") if u.strip()]
    end = int((args.end or time.time()) // 60 * 60)
    start = end - args.days * 86400

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT f.unit_number, f.id FROM flats f JOIN societies s ON s.id=f.society_id "
            "WHERE s.slug=$1",
            args.society,
        )
        ids = {r["unit_number"]: r["id"] for r in rows}
        missing = [u for u in units if u not in ids]
        if missing:
            raise SystemExit(f"flats not found in society {args.society!r}: {missing}")
        from datetime import UTC, datetime

        profiles = make_profiles(len(units), args.seed, units=units)
        total = 0
        for i, profile in enumerate(profiles):
            fs = FlatSimulator(profile, args.society, args.seed, i, start)
            fs.seq = 10_000_000
            records = []
            for ts in range(start, end, 60):
                body = json.loads(fs.flow_message(ts).payload)
                records.append(
                    (
                        ids[profile.unit],
                        datetime.fromtimestamp(ts, UTC),
                        body["flow"],
                        body["vol"],
                        None,
                        None,
                        int(body["bat"]),
                        body["rssi"],
                        body["id"],
                        datetime.fromtimestamp(ts, UTC),
                    )
                )
            await conn.copy_records_to_table(
                "flow_readings",
                records=records,
                columns=[
                    "flat_id", "timestamp", "flow_rate", "volume", "pressure", "temperature",
                    "battery_pct", "rssi", "message_id", "received_at",
                ],
            )  # fmt: skip
            total += len(records)
            print(f"unit {profile.unit}: {len(records)} readings")
        print(f"inserted {total} readings ({args.days} days) for {args.society}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""CLI: python -m edge.simulator --society demo --flats 6 --speed 60 --broker localhost:1883"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal

from edge.simulator.simulator import Simulator, run_mqtt, run_mqtt_per_device


def parse_broker(s: str) -> tuple[str, int]:
    host, _, port = s.partition(":")
    return host or "localhost", int(port or 1883)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m edge.simulator", description=__doc__)
    ap.add_argument("--society", default="demo", help="society slug used in topics")
    ap.add_argument("--flats", type=int, default=6)
    ap.add_argument("--speed", type=float, default=1.0, help="1=realtime, 60=1 sim hour/real min")
    ap.add_argument("--broker", default="localhost:1883")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--units",
        default="",
        help="comma separated unit numbers (overrides --flats/--unit-base), "
        "e.g. 101,102,201,202,301,302 for the seeded demo society 'green-valley'",
    )
    ap.add_argument("--unit-base", type=int, default=101, help="first flat unit number")
    ap.add_argument(
        "--flat-map",
        default=os.environ.get("AQUAGUARD_FLAT_MAP", ""),
        help='JSON {"<unit>": "<backend flat_id>"} so control msgs may use either key',
    )
    ap.add_argument("--username", default=os.environ.get("MQTT_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    ap.add_argument(
        "--device-secret",
        default=os.environ.get("MQTT_DEVICE_SECRET"),
        help="per-device broker accounts: every flat connects as <society>/<unit> with a password "
        "derived from this secret; --username/--password then only serve the control topics",
    )
    ap.add_argument(
        "--tls-ca",
        default=os.environ.get("MQTT_CA_FILE"),
        help="CA certificate file: connect with TLS (broker port 8883) and verify the server",
    )
    ap.add_argument("--log-level", default="INFO")
    return ap


async def amain(args: argparse.Namespace) -> None:
    host, port = parse_broker(args.broker)
    flat_ids = json.loads(args.flat_map) if args.flat_map else None
    sim = Simulator(
        args.society,
        args.flats,
        args.speed,
        args.seed,
        unit_base=args.unit_base,
        flat_ids=flat_ids,
        units=[u.strip() for u in args.units.split(",") if u.strip()] or None,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    if args.device_secret:
        await run_mqtt_per_device(
            sim,
            host,
            port,
            stop,
            args.device_secret,
            args.username,
            args.password,
            tls_ca=args.tls_ca,
        )
    else:
        await run_mqtt(sim, host, port, stop, args.username, args.password, tls_ca=args.tls_ca)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(amain(args))
    return 0

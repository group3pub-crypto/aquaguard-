from __future__ import annotations

import json
import time
from datetime import timedelta
from types import SimpleNamespace

from tests.conftest import T0

TOPIC = "aquaguard/green-valley/101/flow"


def flow_msg(mid: str = "a1b2c3d4", ts: float | None = None, flow: float = 1.5, **kw) -> bytes:
    body = {
        "v": 1,
        "id": mid,
        "ts": ts if ts is not None else time.time() - 5,
        "flow": flow,
        "vol": 0.1,
        "psi": 40.0,
        "tmp": 21.5,
        "bat": 90,
        "rssi": -60,
        **kw,
    }
    return json.dumps(body).encode()


def series(start, minutes: int, flow, step_s: int = 30):
    """Readings (namespaces) from `start`, flow may be a float or callable(i)."""
    out = []
    for i in range(minutes * 60 // step_s + 1):
        f = flow(i) if callable(flow) else flow
        out.append(SimpleNamespace(timestamp=start + timedelta(seconds=i * step_s), flow_rate=f))
    return out


NIGHT = T0  # Tuesday 02:00 UTC
DAY = T0.replace(hour=14)

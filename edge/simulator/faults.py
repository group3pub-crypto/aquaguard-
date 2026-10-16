"""Fault models that modify the base flow of a flat (L/min)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np


class FaultType(StrEnum):
    SLOW_LEAK = "slow_leak"
    RUNNING_TOILET = "running_toilet"
    DRIPPING_TAP = "dripping_tap"
    BURST = "burst"
    OVERUSE = "overuse"


# (low, high) of the default magnitude: L/min added, or multiplier for overuse
MAGNITUDE = {
    FaultType.SLOW_LEAK: (0.5, 3.0),
    FaultType.RUNNING_TOILET: (0.45, 0.55),
    FaultType.DRIPPING_TAP: (0.05, 0.2),
    FaultType.BURST: (15.0, 50.0),
    FaultType.OVERUSE: (2.0, 4.0),
}


@dataclass
class ActiveFault:
    fault_type: FaultType
    magnitude: float
    start_ts: float
    end_ts: float | None = None  # None = until cleared

    def active_at(self, ts: float) -> bool:
        return ts >= self.start_ts and (self.end_ts is None or ts < self.end_ts)

    def expired(self, ts: float) -> bool:
        return self.end_ts is not None and ts >= self.end_ts


def make_fault(
    fault_type: str | FaultType,
    ts: float,
    rng: np.random.Generator,
    params: dict[str, Any] | None = None,
) -> ActiveFault:
    """params: ``rate`` (L/min added; multiplier for overuse), ``duration_min``."""
    params = params or {}
    ft = FaultType(fault_type)
    mag = params.get("rate", params.get("magnitude"))
    if mag is None:
        mag = rng.uniform(*MAGNITUDE[ft])
    dur = params.get("duration_min")
    return ActiveFault(ft, float(mag), float(ts), None if dur is None else ts + 60.0 * float(dur))


def apply_faults(
    base: float, faults: list[ActiveFault], ts: float, rng: np.random.Generator
) -> float:
    """Return the flow after all faults active at ``ts`` (never negative)."""
    flow = base
    for f in faults:
        if not f.active_at(ts):
            continue
        if f.fault_type is FaultType.OVERUSE:
            extra = rng.gamma(2.0, 0.375 * f.magnitude) if rng.random() < 0.5 else 0.0
            flow = flow * f.magnitude + extra
        else:  # additive constant floor, also at night
            flow += f.magnitude * (1.0 + rng.normal(0.0, 0.05))
    return max(0.0, flow)

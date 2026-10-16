"""Household profiles (who lives in the flat and when they use water)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCHEDULES = ("early_bird", "night_owl", "regular", "wfh")


@dataclass(frozen=True)
class Profile:
    unit: str  # flat unit as used in the MQTT topic, e.g. "101"
    flat_id: str  # backend identifier (defaults to "flat-<unit>"; overridable via mapping)
    family_size: int
    schedule: str
    weekend_factor: float


def make_profiles(
    n: int,
    seed: int = 42,
    unit_base: int = 101,
    flat_ids: dict[str, str] | None = None,
) -> list[Profile]:
    """``flat_ids`` maps unit -> backend flat_id (optional)."""
    rng = np.random.default_rng([seed, 1])
    flat_ids = flat_ids or {}
    out = []
    for i in range(n):
        unit = str(unit_base + i)
        out.append(
            Profile(
                unit=unit,
                flat_id=flat_ids.get(unit, f"flat-{unit}"),
                family_size=int(rng.integers(1, 6)),
                schedule=SCHEDULES[i % len(SCHEDULES)],
                weekend_factor=float(rng.uniform(1.05, 1.4)),
            )
        )
    return out

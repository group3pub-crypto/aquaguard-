"""Deterministic synthetic per-minute household water-flow generator with injected faults.

Everything here is SIMULATED. Real sensors (YF-S201 pulse counters, noisy plumbing, real
households) will look different; see the generated reports for the caveats.

Usage: python -m ml.scripts.generate_data --flats 12 --days 30 --seed 42 --out ml/data
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FAULT_TYPES = ("slow_leak", "running_toilet", "dripping_tap", "burst", "overuse")
FAULT_PROBS = (0.20, 0.20, 0.15, 0.25, 0.20)
SCHEDULES = ("early_bird", "night_owl", "regular", "wfh")
NO_FAULT = "none"
START = pd.Timestamp("2025-01-06 00:00", tz="UTC")  # a Monday, so day 0 is Monday
MIN_PER_DAY = 1440

# (morning_mu, morning_sigma, evening_mu, evening_sigma, midday_weight) in hours
_SCHEDULE_PARAMS = {
    "early_bird": (5.5, 0.9, 18.5, 1.4, 0.15),
    "regular": (7.0, 1.0, 20.0, 1.5, 0.15),
    "night_owl": (9.0, 1.2, 22.5, 1.5, 0.15),
    "wfh": (8.0, 1.2, 19.5, 1.6, 0.55),
}
# duration range (minutes) and magnitude range (L/min or multiplier) per fault type
_DURATION = {
    "slow_leak": (360, 2160),
    "running_toilet": (360, 1440),
    "dripping_tap": (720, 2880),
    "burst": (10, 120),
    "overuse": (60, 480),
}
_MAGNITUDE = {
    "slow_leak": (0.5, 3.0),
    "running_toilet": (0.45, 0.55),
    "dripping_tap": (0.05, 0.2),
    "burst": (15.0, 50.0),
    "overuse": (2.0, 4.0),
}


@dataclass(frozen=True)
class Household:
    flat_id: str
    family_size: int
    schedule: str
    weekend_factor: float


@dataclass(frozen=True)
class FaultEpisode:
    kind: str
    start: int  # minute index (inclusive)
    end: int  # minute index (exclusive)
    magnitude: float


def make_households(n: int, seed: int) -> list[Household]:
    rng = np.random.default_rng([seed, 9999])
    return [
        Household(
            flat_id=f"flat-{i + 1:02d}",
            family_size=int(rng.integers(1, 6)),
            schedule=str(rng.choice(SCHEDULES)),
            weekend_factor=float(rng.uniform(1.05, 1.4)),
        )
        for i in range(n)
    ]


def _circ_gauss(hours: np.ndarray, mu: float, sigma: float) -> np.ndarray:
    d = (hours - mu + 12.0) % 24.0 - 12.0
    return np.exp(-0.5 * (d / sigma) ** 2)


def demand_probability(h: Household, n_min: int, rng: np.random.Generator) -> np.ndarray:
    """Per-minute probability that a tap/appliance is running."""
    idx = np.arange(n_min)
    day = idx // MIN_PER_DAY
    hours = (idx % MIN_PER_DAY) / 60.0
    weekend = (day % 7) >= 5
    m_mu, m_sd, e_mu, e_sd, mid = _SCHEDULE_PARAMS[h.schedule]
    amp = min(0.8, 0.08 + 0.10 * h.family_size)
    morning = np.where(
        weekend, _circ_gauss(hours, m_mu + 1.5, m_sd * 1.3), _circ_gauss(hours, m_mu, m_sd)
    )
    evening = _circ_gauss(hours, e_mu, e_sd)
    midday = mid * _circ_gauss(hours, 13.0, 2.0)
    p = amp * (morning + evening + midday) + 0.003  # tiny night-time flush probability
    p = p * np.where(weekend, h.weekend_factor, 1.0)
    day_factor = np.exp(rng.normal(0.0, 0.15, size=int(day.max()) + 1))[day]
    return np.clip(p * day_factor, 0.0, 0.85)


def sample_base_flow(p: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    active = rng.random(p.size) < p
    flow = np.where(active, rng.gamma(2.0, 1.5, p.size), 0.0)  # mean 3 L/min while active
    return flow * (1.0 + rng.normal(0.0, 0.05, p.size))  # multiplicative sensor noise


def schedule_faults(n_min: int, rng: np.random.Generator) -> list[FaultEpisode]:
    """Sequential, non-overlapping fault episodes spread over the whole timeline."""
    eps: list[FaultEpisode] = []
    cur = MIN_PER_DAY + int(rng.integers(0, 600))
    while True:
        kind = str(rng.choice(FAULT_TYPES, p=FAULT_PROBS))
        dur = int(rng.integers(*_DURATION[kind]))
        if cur + dur >= n_min:
            break
        mag = float(rng.uniform(*_MAGNITUDE[kind]))
        eps.append(FaultEpisode(kind, cur, cur + dur, mag))
        cur += dur + int(rng.integers(1440, 7200))
    return eps


def apply_faults(
    base: np.ndarray, eps: list[FaultEpisode], rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    flow = base.copy()
    labels = np.zeros(base.size, dtype=np.int8)
    for ep in eps:
        sl = slice(ep.start, min(ep.end, base.size))
        n = sl.stop - sl.start
        if ep.kind == "overuse":
            extra = (rng.random(n) < 0.5) * rng.gamma(2.0, 0.375 * ep.magnitude, n)
            flow[sl] = base[sl] * ep.magnitude + extra
        else:  # additive, constant floor (also at night)
            flow[sl] += ep.magnitude * (1.0 + rng.normal(0.0, 0.05, n))
        labels[sl] = FAULT_TYPES.index(ep.kind) + 1
    return np.clip(flow, 0.0, None), labels


def generate_flat(
    h: Household,
    days: int,
    seed: int,
    index: int = 0,
    episodes: list[FaultEpisode] | None = None,
    inject_faults: bool = True,
    dropout: float = 0.005,
) -> pd.DataFrame:
    """One flat, per-minute. Columns: ts, flat_id, flow (L/min, NaN = dropout), fault."""
    rng = np.random.default_rng([seed, index])
    n = days * MIN_PER_DAY
    base = sample_base_flow(demand_probability(h, n, rng), rng)
    if episodes is None:
        episodes = schedule_faults(n, rng) if inject_faults else []
    flow, lab = apply_faults(base, episodes, rng)
    flow[rng.random(n) < dropout] = np.nan
    names = np.array([NO_FAULT, *FAULT_TYPES])
    return pd.DataFrame(
        {
            "ts": pd.date_range(START, periods=n, freq="min"),
            "flat_id": h.flat_id,
            "flow": flow.astype("float32"),
            "fault": pd.Categorical(names[lab], categories=list(names)),
        }
    )


def generate_dataset(
    n_flats: int, days: int, seed: int, inject_faults: bool = True
) -> pd.DataFrame:
    hs = make_households(n_flats, seed)
    frames = [generate_flat(h, days, seed, i, inject_faults=inject_faults) for i, h in enumerate(hs)]
    return pd.concat(frames, ignore_index=True)


def dataset_hash(df: pd.DataFrame) -> str:
    import hashlib

    h = pd.util.hash_pandas_object(df[["ts", "flat_id", "flow"]], index=False).to_numpy()
    return hashlib.sha256(h.tobytes() + df["fault"].astype(str).str.cat().encode()).hexdigest()[:16]


def save_dataset(df: pd.DataFrame, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for flat_id, g in df.groupby("flat_id", observed=True):
        try:
            p = out / f"{flat_id}.parquet"
            g.to_parquet(p, index=False)
        except ImportError:  # no pyarrow/fastparquet
            p = out / f"{flat_id}.csv"
            g.to_csv(p, index=False)
        paths.append(p)
    return paths


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flats", type=int, default=12)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path(__file__).parents[1] / "data")
    a = ap.parse_args(argv)
    df = generate_dataset(a.flats, a.days, a.seed)
    paths = save_dataset(df, a.out)
    frac = (df["fault"] != NO_FAULT).mean()
    print(f"wrote {len(paths)} files to {a.out} ({len(df)} rows, {frac:.1%} faulty minutes)")


if __name__ == "__main__":
    main()

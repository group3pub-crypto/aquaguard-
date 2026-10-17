"""Deterministic synthetic per-minute household water-flow generator with injected faults.

Everything here is SIMULATED. Real sensors (YF-S201 pulse counters, noisy plumbing, real
households) will look different; see the generated reports for the caveats.

Usage: python -m ml.scripts.generate_data --flats 12 --days 30 --seed 42 --out ml/data

Stress-test options (still SYNTHETIC; used by ``ml/scripts/benchmark.py``):

* ``--profile shifted``: different household schedules, flow statistics, nocturnal use and fault
  ranges (a distribution shift relative to the default generator).
* ``--sensor-model yfs201``: post-processes the true flow through an assumed YF-S201 model
  (stall threshold, 450 pulses/L quantization, per-device calibration error and drift, missed
  pulses, timing jitter, outages, stuck sensor). See ``SensorSpec`` for every assumption.

The defaults (``--profile default --sensor-model none``) reproduce the original output exactly.

The trainers can read this output with ``--data-dir ml/data`` (otherwise they generate the same
data in memory from --seed/--flats/--days, so this step is optional).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
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
class ProfileSpec:
    """Everything the generator needs to know about a population of households and faults."""

    name: str
    schedules: tuple[str, ...]
    schedule_params: dict
    fault_probs: tuple[float, ...]
    durations: dict
    magnitudes: dict
    family_range: tuple[int, int] = (1, 6)  # [lo, hi) for rng.integers
    weekend_range: tuple[float, float] = (1.05, 1.4)
    gamma_shape: float = 2.0
    gamma_scale: float = 1.5  # mean active flow = shape * scale (L/min)
    flow_noise: float = 0.05
    day_sigma: float = 0.15
    night_prob: float = 0.003  # per-minute night-time flush probability floor
    dropout: float = 0.005


DEFAULT_PROFILE = ProfileSpec(
    name="default",
    schedules=SCHEDULES,
    schedule_params=_SCHEDULE_PARAMS,
    fault_probs=FAULT_PROBS,
    durations=_DURATION,
    magnitudes=_MAGNITUDE,
)

# A deliberately different world: more varied (and nocturnal) schedules, burstier/larger draws,
# 4x more night-time legitimate use, noisier days, bigger households, and fault ranges that do
# NOT mirror the rule thresholds (weaker slow leaks, bursts that are smaller). Still synthetic.
SHIFTED_PROFILE = ProfileSpec(
    name="shifted",
    schedules=("shift_worker", "retiree", "student", "busy_family"),
    schedule_params={
        "shift_worker": (14.0, 1.5, 1.5, 1.8, 0.25),  # peaks mid-afternoon and ~01:30
        "retiree": (8.5, 1.8, 17.0, 2.2, 0.70),
        "student": (10.5, 1.8, 23.5, 1.8, 0.35),
        "busy_family": (6.5, 0.8, 19.0, 2.0, 0.30),
    },
    fault_probs=(0.25, 0.20, 0.15, 0.15, 0.25),
    durations={
        "slow_leak": (240, 2880),
        "running_toilet": (180, 1800),
        "dripping_tap": (480, 3600),
        "burst": (5, 90),
        "overuse": (30, 600),
    },
    magnitudes={
        "slow_leak": (0.2, 2.0),
        "running_toilet": (0.25, 0.9),
        "dripping_tap": (0.03, 0.35),
        "burst": (8.0, 40.0),
        "overuse": (1.5, 3.5),
    },
    family_range=(1, 8),
    weekend_range=(1.0, 1.7),
    gamma_shape=2.5,
    gamma_scale=1.6,
    flow_noise=0.08,
    day_sigma=0.30,
    night_prob=0.012,
    dropout=0.01,
)
PROFILES = {"default": DEFAULT_PROFILE, "shifted": SHIFTED_PROFILE}


# ----------------------------------------------------------------------------- sensor model
PULSES_PER_LITRE = 450.0
"""YF-S201: F[Hz] = 7.5 * Q[L/min]  =>  7.5 pulses/s per (L/min) = 450 pulses per minute per
(L/min) = 450 pulses per litre. One pulse therefore resolves 1/450 = 0.0022 L (per-minute counts:
0.0022 L/min per pulse/min). Quantization is NEGLIGIBLE; the stall threshold below is not."""


@dataclass(frozen=True)
class SensorSpec:
    """Assumed YF-S201 behaviour. Every number is an ASSUMPTION (see docs in benchmark report).

    * ``min_flow_lpm``: below this instantaneous flow the rotor does not turn / pulses are not
      emitted. The commonly published YF-S201 operating range is 1-30 L/min, so 1.0 L/min is
      used as the nominal start-up threshold. This was NOT measured here; it is the single most
      consequential assumption (it hides slow leaks) and the benchmark sweeps it. Applied to the
      per-minute MEAN flow (a per-minute simulator cannot resolve sub-minute draw patterns).
    * ``min_flow_spread``: per-device relative spread of that threshold (uniform +-).
    * ``cal_error``: per-device K-factor/gain error, uniform +-10% (typical datasheet accuracy).
    * ``drift_per_30d``: per-device linear gain drift, uniform +- over 30 days (scale, wear).
    * ``pulse_loss``: independent per-pulse loss probability; ``loss_event_*``: rare minutes in
      which a fraction of pulses is lost (interrupt latency / wifi stalls on the ESP32).
    * ``jitter_s``: std (s) of the minute-boundary timing jitter; pulses near a boundary are
      attributed to the neighbouring minute. A digital pulse counter has no ADC; this is the
      timing-error analogue.
    * ``outage_*``: telemetry gaps (NaN), ``stuck_*``: rotor jam (reads 0) or frozen reading.
    """

    min_flow_lpm: float = 1.0
    min_flow_spread: float = 0.15
    cal_error: float = 0.10
    drift_per_30d: float = 0.03
    pulse_loss: float = 0.002
    loss_event_rate_per_day: float = 0.3
    loss_event_frac: tuple[float, float] = (0.10, 0.50)
    jitter_s: float = 1.0
    outage_rate_per_day: float = 0.05
    outage_minutes: tuple[int, int] = (5, 180)
    stuck_rate_per_day: float = 0.03
    stuck_minutes: tuple[int, int] = (30, 720)
    pulses_per_litre: float = field(default=PULSES_PER_LITRE)


YFS201 = SensorSpec()
SENSOR_MODELS: dict[str, SensorSpec | None] = {"none": None, "yfs201": YFS201}


@dataclass(frozen=True)
class DeviceParams:
    gain: float
    drift: float
    min_flow: float


def make_device(rng: np.random.Generator, spec: SensorSpec) -> DeviceParams:
    return DeviceParams(
        gain=float(rng.uniform(1.0 - spec.cal_error, 1.0 + spec.cal_error)),
        drift=float(rng.uniform(-spec.drift_per_30d, spec.drift_per_30d)),
        min_flow=float(
            spec.min_flow_lpm * rng.uniform(1.0 - spec.min_flow_spread, 1.0 + spec.min_flow_spread)
        ),
    )


def _poisson_spans(
    n: int, rate_per_day: float, lo_hi: tuple[int, int], rng: np.random.Generator
) -> list[tuple[int, int]]:
    k = int(rng.poisson(rate_per_day * n / MIN_PER_DAY))
    spans = []
    for _ in range(k):
        s = int(rng.integers(0, n))
        spans.append((s, min(n, s + int(rng.integers(lo_hi[0], lo_hi[1] + 1)))))
    return spans


def apply_sensor_model(
    flow: np.ndarray, rng: np.random.Generator, spec: SensorSpec, device: DeviceParams
) -> np.ndarray:
    """True per-minute flow (L/min) -> what a YF-S201 + pulse counter would report (L/min).

    Chain: stall threshold -> calibration gain & drift -> pulses (450/L) -> timing jitter ->
    lost pulses -> integer pulse counts (random phase rounding) -> decode with the NOMINAL
    K-factor (so gain error appears as a flow error) -> stuck sensor -> outages (NaN).
    Deterministic given ``rng``.
    """
    f = np.nan_to_num(np.asarray(flow, dtype=float), nan=0.0)
    n = f.size
    t = np.arange(n) / (30.0 * MIN_PER_DAY)
    gain = device.gain * (1.0 + device.drift * t)
    expected = np.where(f >= device.min_flow, f, 0.0) * spec.pulses_per_litre * gain
    # timing jitter: a signed fraction of this minute's pulses lands in the adjacent minute
    s = np.clip(rng.normal(0.0, spec.jitter_s / 60.0, n), -0.5, 0.5)
    moved = expected * np.abs(s)
    out = expected - moved
    out[1:] += np.where(s[:-1] > 0, moved[:-1], 0.0)
    out[:-1] += np.where(s[1:] < 0, moved[1:], 0.0)
    # rare minutes with a large fraction of lost pulses
    ev = rng.random(n) < spec.loss_event_rate_per_day / MIN_PER_DAY
    frac = rng.uniform(*spec.loss_event_frac, n)
    out = out * np.where(ev, 1.0 - frac, 1.0)
    pulses = np.floor(out + rng.random(n)).astype(np.int64)  # integer counts, random phase
    pulses = rng.binomial(pulses, 1.0 - spec.pulse_loss)
    reported = pulses / spec.pulses_per_litre  # firmware decodes with the nominal K-factor
    for a, b in _poisson_spans(n, spec.stuck_rate_per_day, spec.stuck_minutes, rng):
        hold = bool(rng.random() < 0.5)
        reported[a:b] = reported[a - 1] if (hold and a > 0) else 0.0
    for a, b in _poisson_spans(n, spec.outage_rate_per_day, spec.outage_minutes, rng):
        reported[a:b] = np.nan
    return reported


def resolve_sensor(
    sensor_model: str | SensorSpec | None, yfs_min_flow: float | None = None
) -> SensorSpec | None:
    spec = SENSOR_MODELS[sensor_model] if isinstance(sensor_model, str) else sensor_model
    if spec is not None and yfs_min_flow is not None:
        spec = replace(spec, min_flow_lpm=float(yfs_min_flow))
    return spec


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


def make_households(n: int, seed: int, profile: str = "default") -> list[Household]:
    spec = PROFILES[profile]
    rng = np.random.default_rng([seed, 9999])
    return [
        Household(
            flat_id=f"flat-{i + 1:02d}",
            family_size=int(rng.integers(*spec.family_range)),
            schedule=str(rng.choice(spec.schedules)),
            weekend_factor=float(rng.uniform(*spec.weekend_range)),
        )
        for i in range(n)
    ]


def _circ_gauss(hours: np.ndarray, mu: float, sigma: float) -> np.ndarray:
    d = (hours - mu + 12.0) % 24.0 - 12.0
    return np.exp(-0.5 * (d / sigma) ** 2)


def demand_probability(
    h: Household,
    n_min: int,
    rng: np.random.Generator,
    spec: ProfileSpec = DEFAULT_PROFILE,
) -> np.ndarray:
    """Per-minute probability that a tap/appliance is running."""
    idx = np.arange(n_min)
    day = idx // MIN_PER_DAY
    hours = (idx % MIN_PER_DAY) / 60.0
    weekend = (day % 7) >= 5
    m_mu, m_sd, e_mu, e_sd, mid = spec.schedule_params[h.schedule]
    amp = min(0.8, 0.08 + 0.10 * h.family_size)
    morning = np.where(
        weekend, _circ_gauss(hours, m_mu + 1.5, m_sd * 1.3), _circ_gauss(hours, m_mu, m_sd)
    )
    evening = _circ_gauss(hours, e_mu, e_sd)
    midday = mid * _circ_gauss(hours, 13.0, 2.0)
    p = amp * (morning + evening + midday) + spec.night_prob  # night-time flush probability
    p = p * np.where(weekend, h.weekend_factor, 1.0)
    day_factor = np.exp(rng.normal(0.0, spec.day_sigma, size=int(day.max()) + 1))[day]
    return np.clip(p * day_factor, 0.0, 0.85)


def sample_base_flow(
    p: np.ndarray, rng: np.random.Generator, spec: ProfileSpec = DEFAULT_PROFILE
) -> np.ndarray:
    active = rng.random(p.size) < p
    # default: mean 3 L/min while active
    flow = np.where(active, rng.gamma(spec.gamma_shape, spec.gamma_scale, p.size), 0.0)
    return flow * (1.0 + rng.normal(0.0, spec.flow_noise, p.size))  # multiplicative noise


def schedule_faults(
    n_min: int, rng: np.random.Generator, spec: ProfileSpec = DEFAULT_PROFILE
) -> list[FaultEpisode]:
    """Sequential, non-overlapping fault episodes spread over the whole timeline."""
    eps: list[FaultEpisode] = []
    cur = MIN_PER_DAY + int(rng.integers(0, 600))
    while True:
        kind = str(rng.choice(FAULT_TYPES, p=spec.fault_probs))
        dur = int(rng.integers(*spec.durations[kind]))
        if cur + dur >= n_min:
            break
        mag = float(rng.uniform(*spec.magnitudes[kind]))
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


def true_base_flow(
    h: Household, days: int, seed: int, index: int = 0, profile: str = "default"
) -> tuple[np.ndarray, np.random.Generator]:
    """Fault-free true flow for one flat and the generator state right after drawing it."""
    spec = PROFILES[profile]
    rng = np.random.default_rng([seed, index])
    n = days * MIN_PER_DAY
    return sample_base_flow(demand_probability(h, n, rng, spec), rng, spec), rng


def generate_flat(
    h: Household,
    days: int,
    seed: int,
    index: int = 0,
    episodes: list[FaultEpisode] | None = None,
    inject_faults: bool = True,
    dropout: float | None = None,
    profile: str = "default",
    sensor_model: str | SensorSpec | None = "none",
    yfs_min_flow: float | None = None,
) -> pd.DataFrame:
    """One flat, per-minute. Columns: ts, flat_id, flow (L/min, NaN = dropout), fault.

    ``sensor_model="yfs201"`` passes the (faulted) true flow through :func:`apply_sensor_model`
    using an independent RNG stream, so the fault/household draws are identical with and without
    the sensor model (paired comparisons are possible) and the default output is unchanged.
    """
    spec = PROFILES[profile]
    n = days * MIN_PER_DAY
    base, rng = true_base_flow(h, days, seed, index, profile)
    if episodes is None:
        episodes = schedule_faults(n, rng, spec) if inject_faults else []
    flow, lab = apply_faults(base, episodes, rng)
    sensor = resolve_sensor(sensor_model, yfs_min_flow)
    if sensor is not None:
        srng = np.random.default_rng([seed, index, 7777])
        flow = apply_sensor_model(flow, srng, sensor, make_device(srng, sensor))
    drop = spec.dropout if dropout is None else dropout
    flow[rng.random(n) < drop] = np.nan
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
    n_flats: int,
    days: int,
    seed: int,
    inject_faults: bool = True,
    profile: str = "default",
    sensor_model: str | SensorSpec | None = "none",
    yfs_min_flow: float | None = None,
) -> pd.DataFrame:
    hs = make_households(n_flats, seed, profile)
    frames = [
        generate_flat(
            h,
            days,
            seed,
            i,
            inject_faults=inject_faults,
            profile=profile,
            sensor_model=sensor_model,
            yfs_min_flow=yfs_min_flow,
        )
        for i, h in enumerate(hs)
    ]
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


def load_dataset(data_dir: Path) -> pd.DataFrame:
    """Read what ``save_dataset`` wrote (parquet or csv per flat) back into the training frame."""
    files = sorted([*data_dir.glob("*.parquet"), *data_dir.glob("*.csv")])
    if not files:
        raise FileNotFoundError(f"no .parquet/.csv files in {data_dir}")
    parts = [
        pd.read_parquet(f) if f.suffix == ".parquet" else pd.read_csv(f, parse_dates=["ts"])
        for f in files
    ]
    df = pd.concat(parts, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["flow"] = df["flow"].astype("float32")
    df["fault"] = pd.Categorical(df["fault"].astype(str), categories=[NO_FAULT, *FAULT_TYPES])
    df["flat_id"] = df["flat_id"].astype(str)
    return df.sort_values(["flat_id", "ts"], ignore_index=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flats", type=int, default=12)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path(__file__).parents[1] / "data")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="default")
    ap.add_argument("--sensor-model", choices=sorted(SENSOR_MODELS), default="none")
    ap.add_argument(
        "--yfs-min-flow",
        type=float,
        default=None,
        help="override the assumed YF-S201 start-up threshold (L/min, default 1.0)",
    )
    a = ap.parse_args(argv)
    df = generate_dataset(
        a.flats,
        a.days,
        a.seed,
        profile=a.profile,
        sensor_model=a.sensor_model,
        yfs_min_flow=a.yfs_min_flow,
    )
    paths = save_dataset(df, a.out)
    frac = (df["fault"] != NO_FAULT).mean()
    print(f"wrote {len(paths)} files to {a.out} ({len(df)} rows, {frac:.1%} faulty minutes)")


if __name__ == "__main__":
    main()

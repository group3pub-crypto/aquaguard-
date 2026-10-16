"""Daily usage pattern: morning/evening Gaussian peaks, near-zero nights, weekend factor."""

from __future__ import annotations

import math

import numpy as np

from edge.simulator.profiles import Profile

# (morning_mu, morning_sigma, evening_mu, evening_sigma, midday_weight) in hours
_SCHEDULE = {
    "early_bird": (5.5, 0.9, 18.5, 1.4, 0.15),
    "regular": (7.0, 1.0, 20.0, 1.5, 0.15),
    "night_owl": (9.0, 1.2, 22.5, 1.5, 0.15),
    "wfh": (8.0, 1.2, 19.5, 1.6, 0.55),
}


def _g(h: float, mu: float, sigma: float) -> float:
    d = (h - mu + 12.0) % 24.0 - 12.0
    return math.exp(-0.5 * (d / sigma) ** 2)


def day_factor(seed: int, day_index: int) -> float:
    return float(np.exp(np.random.default_rng([seed, 2, day_index]).normal(0.0, 0.15)))


def demand_probability(profile: Profile, epoch_s: float, seed: int = 42) -> float:
    """Probability that water is flowing during the minute starting at ``epoch_s`` (UTC)."""
    day = int(epoch_s // 86400)
    hour = (epoch_s % 86400) / 3600.0
    weekend = ((day + 3) % 7) >= 5  # 1970-01-01 was a Thursday
    m_mu, m_sd, e_mu, e_sd, mid = _SCHEDULE[profile.schedule]
    morning = _g(hour, m_mu + 1.5, m_sd * 1.3) if weekend else _g(hour, m_mu, m_sd)
    amp = min(0.8, 0.08 + 0.10 * profile.family_size)
    p = amp * (morning + _g(hour, e_mu, e_sd) + mid * _g(hour, 13.0, 2.0)) + 0.003
    if weekend:
        p *= profile.weekend_factor
    return float(min(0.85, max(0.0, p * day_factor(seed, day))))


def sample_flow(
    profile: Profile, epoch_s: float, rng: np.random.Generator, seed: int = 42
) -> float:
    """Normal (fault-free) flow in L/min for one minute."""
    if rng.random() >= demand_probability(profile, epoch_s, seed):
        return 0.0
    return float(rng.gamma(2.0, 1.5) * (1.0 + rng.normal(0.0, 0.05)))

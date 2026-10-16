"""Demo: a 2 L/min night-time slow leak is caught by a personal baseline, missed by a 500 L/day cap.

Usage: python -m ml.scripts.demo_baseline
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ml.features.engineering import build_baselines, featurize_dataset
from ml.scripts.generate_data import (
    MIN_PER_DAY,
    START,
    FaultEpisode,
    Household,
    generate_flat,
)

DAILY_CAP_L = 500.0
LEAK_LPM = 2.0
Z_THRESHOLD = 4.0
LEAK_MIN = 90


def run_demo(seed: int = 7) -> dict:
    """Single-person flat, 14 days; leak 00:00-01:30 on day 10 (a Thursday)."""
    h = Household("demo-flat", family_size=1, schedule="regular", weekend_factor=1.2)
    leak_day = 10
    start = leak_day * MIN_PER_DAY
    ep = FaultEpisode("slow_leak", start, start + LEAK_MIN, LEAK_LPM)
    df = generate_flat(h, 14, seed, episodes=[ep], dropout=0.0)

    # personal baseline: first 7 days (all normal)
    split = START + pd.Timedelta(days=7)
    base = build_baselines(df, split)
    w = featurize_dataset(df, base, stride=5)
    w = w[w["ts"] >= split]
    flagged = (w["baseline_deviation"] > Z_THRESHOLD) & (w["min_flow"] > 0.5)

    in_leak = (w["ts"] >= START + pd.Timedelta(minutes=start + 15)) & (
        w["ts"] < START + pd.Timedelta(minutes=start + LEAK_MIN)
    )
    first = w[flagged & in_leak]["ts"]
    day = df[
        (df["ts"] >= START + pd.Timedelta(days=leak_day))
        & (df["ts"] < START + pd.Timedelta(days=leak_day + 1))
    ]
    daily_total = float(day["flow"].sum())

    # how long would the cap take to trip if the same leak ran all day?
    cum = np.nancumsum(day["flow"].to_numpy(float) + LEAK_LPM * (day["fault"] == "none").to_numpy())
    cap_trip_h = float(np.argmax(cum > DAILY_CAP_L) / 60.0) if (cum > DAILY_CAP_L).any() else None
    return {
        "leak_lpm": LEAK_LPM,
        "leak_minutes": LEAK_MIN,
        "leak_volume_l": LEAK_LPM * LEAK_MIN,
        "daily_total_l": daily_total,
        "daily_cap_l": DAILY_CAP_L,
        "cap_missed": daily_total <= DAILY_CAP_L,
        "baseline_flagged": bool(first.size),
        "baseline_detect_min_after_onset": float(
            (first.iloc[0] - (START + pd.Timedelta(minutes=start))).total_seconds() / 60
        )
        if first.size
        else None,
        "false_alarm_windows_other_days": int(flagged[~in_leak].sum()),
        "windows_other_days": int((~in_leak).sum()),
        "cap_trip_hours_if_leak_all_day": cap_trip_h,
    }


def main() -> None:
    r = run_demo()
    print("AquaGuard demo: 2 L/min slow leak, 00:00-01:30, single-person flat (synthetic)")
    print(f"  leak volume that night            : {r['leak_volume_l']:.0f} L")
    print(
        f"  daily total incl. leak            : {r['daily_total_l']:.0f} L "
        f"(cap {r['daily_cap_l']:.0f} L) -> fixed cap "
        f"{'MISSED the leak' if r['cap_missed'] else 'tripped'}"
    )
    print(
        f"  personal baseline                 : "
        f"{'FLAGGED' if r['baseline_flagged'] else 'missed'} "
        f"{r['baseline_detect_min_after_onset']:.0f} min after onset"
    )
    print(
        f"  baseline false alarms elsewhere   : {r['false_alarm_windows_other_days']} of "
        f"{r['windows_other_days']} windows"
    )
    print(
        f"  cap would trip only after         : {r['cap_trip_hours_if_leak_all_day']:.1f} h "
        "of a leak running all day"
    )


if __name__ == "__main__":
    main()

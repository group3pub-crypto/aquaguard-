"""Topic helpers: aquaguard/{society_slug}/{flat_unit}/flow|heartbeat|valve/cmd|valve/state."""

from __future__ import annotations

import re
from dataclasses import dataclass

ROOT = "aquaguard"
SUBSCRIPTIONS = (f"{ROOT}/+/+/flow", f"{ROOT}/+/+/heartbeat", f"{ROOT}/+/+/valve/state")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


@dataclass(frozen=True)
class ParsedTopic:
    slug: str
    unit: str
    kind: str  # flow | heartbeat | valve_state


def valve_cmd_topic(slug: str, unit: str) -> str:
    return f"{ROOT}/{slug}/{unit}/valve/cmd"


def parse_topic(topic: str) -> ParsedTopic | None:
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != ROOT or parts[1] == "control":
        return None
    slug, unit, rest = parts[1], parts[2], "/".join(parts[3:])
    if not (_SEGMENT.match(slug) and _SEGMENT.match(unit)):
        return None
    kind = {"flow": "flow", "heartbeat": "heartbeat", "valve/state": "valve_state"}.get(rest)
    return ParsedTopic(slug, unit, kind) if kind else None

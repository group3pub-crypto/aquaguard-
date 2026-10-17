import re
from datetime import UTC, datetime, timedelta
from typing import Any

from bot.auth import Principal, Role
from bot.tools.registry import Tool, ToolRegistry
from bot.tools.scope import can_see_alert, scoped_flat

# Mirrors backend ALERT_STATES (backend/app/models/alerts.py).
STATES = ["nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed", "overridden", "resolved"]
LABELS = {
    "nudge": "NUDGE",
    "alert": "ALERT",
    "ticket": "TICKET",
    "auto_shutoff": "AUTO-SHUTOFF",
    "acknowledged": "ACKNOWLEDGED",
    "snoozed": "SNOOZED",
    "overridden": "OVERRIDDEN",
    "resolved": "RESOLVED",
}
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


# Backend SHUTOFF_WARNING_SECONDS (default 300): the bot only derives a countdown from
# `shutoff_warned_at`, the backend decides when the valve command is really sent.
_warning_seconds = 300.0

# Past/present-tense claims that water/valve IS or WAS shut off. Only a device-confirmed alert
# (state == auto_shutoff) may say so; "will be shut off" (a warning) must not match.
_SHUTOFF_CLAIM = re.compile(
    r"(\b(water|supply|valve|mains|flat|unit)\b[^.!?\n]{0,40}"
    r"\b(has been|have been|was|were|is now|are now|is)\s+(automatically\s+)?"
    r"(shut[\s-]?off|shut\s+down|shut|closed|turned\s+off|cut\s+off|isolated|off)\b"
    r"|\b(we|i|aquaguard|the system)\s+(have\s+|has\s+)?(automatically\s+)?"
    r"(shut|closed|turned\s+off|cut\s+off|isolated)\b)",
    re.I,
)
WITHHELD = "(message withheld: it claimed a shut-off the device has not confirmed)"


def set_warning_seconds(seconds: float) -> None:
    global _warning_seconds
    _warning_seconds = max(0.0, float(seconds))


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def safe_message(state: str, message: str | None) -> str:
    """Backend text, minus any claim of a shut-off unless the alert is device-confirmed."""
    msg = (message or "").strip()
    if msg and state != "auto_shutoff" and _SHUTOFF_CLAIM.search(msg):
        return WITHHELD
    return msg


def shutoff_eta(a: dict, now: datetime | None = None) -> tuple[datetime, int] | None:
    """(planned shut-off time, seconds remaining) while a warning window is running."""
    warned = _parse_ts(a.get("shutoff_warned_at"))
    if warned is None or a.get("shutoff_gave_up_at") or a.get("state") in ("auto_shutoff", "resolved"):
        return None
    when = warned + timedelta(seconds=_warning_seconds)
    return when, int((when - (now or datetime.now(UTC))).total_seconds())


def countdown(remaining: int) -> str:
    if remaining <= 0:
        return "any moment now"
    if remaining < 60:
        return "in under a minute"
    return f"in about {round(remaining / 60)} min"


def fmt_alert(a: dict, unit: str, now: datetime | None = None) -> str:
    """One alert as text. `unit` is the resolved unit number (not the flat UUID).

    Wording rule: water is only reported as shut off when `state == auto_shutoff`, which the
    backend sets only after the valve device confirmed. Every other state says the opposite.
    """
    state = a.get("state", "?")
    head = f"[{LABELS.get(state, state)}] {unit} - tier {a.get('tier', '?')}"
    score = a.get("severity_score")
    if isinstance(score, int | float):
        head += f", severity {score:.2f}"
    lines = [head]
    msg = safe_message(state, a.get("message"))
    if msg:
        lines.append(msg)
    if state == "auto_shutoff":
        lines.append("Valve closed automatically (confirmed by the device).")
    elif a.get("shutoff_gave_up_at") and state not in ("resolved", "overridden"):
        lines.append("Automatic shut-off FAILED: the valve is NOT confirmed closed. Close it manually.")
    else:
        eta = shutoff_eta(a, now)
        if eta is not None:
            when, remaining = eta
            lines.append(
                f"Automatic shut-off planned {countdown(remaining)} ({when:%H:%M} UTC) unless the alert is "
                "acknowledged. Water has NOT been shut off."
            )
    return "\n".join(lines)


def fmt_shutoff_warning(a: dict, unit: str, now: datetime | None = None) -> str:
    eta = shutoff_eta(a, now)
    when = f" {countdown(eta[1])} ({eta[0]:%H:%M} UTC)" if eta else " soon"
    return (
        f"[SHUT-OFF WARNING] {unit}: the water will be shut off automatically{when} unless the alert "
        "is acknowledged. Water has NOT been shut off yet."
    )


def fmt_shutoff_failed(a: dict, unit: str) -> str:
    return (
        f"[SHUT-OFF FAILED] {unit}: the automatic shut-off could not be confirmed by the valve device "
        "and the system gave up. Water is NOT confirmed shut off. Close the valve manually."
    )


def acted_by(resp: Any) -> str:
    """Suffix surfacing the backend's echo of `on_behalf_of` (asserted, never verified)."""
    who = resp.get("on_behalf_of") if isinstance(resp, dict) else None
    if not who:
        return " Recorded under the bot service account."
    return f" Recorded on behalf of {who} (asserted by the bot, not verified by the backend)."


async def list_alerts(api, args: dict, p: Principal) -> str:
    flat_id, _ = await scoped_flat(api, p, args.get("unit"))
    state = args.get("state")
    if state:
        items = await api.alerts(state=state, flat_id=flat_id, limit=50)
    else:
        items = await api.active_alerts(flat_id=flat_id)
    items = [a for a in items if can_see_alert(p, a)]
    if not items:
        return "No active alerts." if not state else f"No alerts in state '{state}'."
    lines = [fmt_alert(a, await api.unit_label(a["flat_id"])) for a in items[:20]]
    more = f"\n... and {len(items) - 20} more" if len(items) > 20 else ""
    return f"{len(items)} alert(s)\n\n" + "\n\n".join(lines) + more


async def ack(api, args: dict, p: Principal) -> str:
    resp = await api.ack_alert(args["alert_id"], p.on_behalf_of)
    return f"Alert {args['alert_id']} acknowledged.{acted_by(resp)}"


async def snooze(api, args: dict, p: Principal) -> str:
    mins = args.get("minutes", 60)
    resp = await api.snooze_alert(args["alert_id"], mins, p.on_behalf_of)
    return f"Alert {args['alert_id']} snoozed for {mins} min.{acted_by(resp)}"


async def resolve(api, args: dict, p: Principal) -> str:
    resp = await api.resolve_alert(args["alert_id"], p.on_behalf_of)
    return f"Alert {args['alert_id']} resolved.{acted_by(resp)}"


_ID = {"type": "string", "pattern": _UUID, "maxLength": 36}


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "list_active_alerts",
            "List open leak alerts (all open states), or alerts in one state. Residents see only their flat.",
            {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": STATES},
                    "unit": {"type": "string", "maxLength": 32, "pattern": r"^[A-Za-z0-9-]+$"},
                },
                "required": [],
                "additionalProperties": False,
            },
            list_alerts,
            Role.RESIDENT,
        )
    )
    for name, desc, fn, extra in (
        ("acknowledge_alert", "Acknowledge an alert by id.", ack, {}),
        (
            "snooze_alert",
            "Snooze an alert for 15, 30, 60 or 120 minutes.",
            snooze,
            {"minutes": {"type": "integer", "enum": [15, 30, 60, 120]}},
        ),
        ("resolve_alert", "Mark an alert as resolved.", resolve, {}),
    ):
        reg.register(
            Tool(
                name,
                desc,
                {
                    "type": "object",
                    "properties": {"alert_id": _ID, **extra},
                    "required": ["alert_id"],
                    "additionalProperties": False,
                },
                fn,
                Role.MAINTENANCE,
            )
        )

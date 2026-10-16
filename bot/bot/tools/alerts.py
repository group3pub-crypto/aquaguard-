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


def fmt_alert(a: dict, unit: str) -> str:
    """One alert as text. `unit` is the resolved unit number (not the flat UUID)."""
    state = a.get("state", "?")
    head = f"[{LABELS.get(state, state)}] {unit} - tier {a.get('tier', '?')}"
    score = a.get("severity_score")
    if isinstance(score, int | float):
        head += f", severity {score:.2f}"
    msg = (a.get("message") or "").strip()
    return f"{head}\n{msg}" if msg else head


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
    await api.ack_alert(args["alert_id"])
    return f"Alert {args['alert_id']} acknowledged."


async def snooze(api, args: dict, p: Principal) -> str:
    mins = args.get("minutes", 60)
    await api.snooze_alert(args["alert_id"], mins)
    return f"Alert {args['alert_id']} snoozed for {mins} min."


async def resolve(api, args: dict, p: Principal) -> str:
    await api.resolve_alert(args["alert_id"])
    return f"Alert {args['alert_id']} resolved."


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

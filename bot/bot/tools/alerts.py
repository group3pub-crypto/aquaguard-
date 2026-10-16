from bot.auth import Role
from bot.tools.registry import Tool, ToolRegistry

STATES = ["watch", "alert", "ticket", "critical"]


def fmt_alert(a: dict) -> str:
    unit = a.get("unit", a.get("unit_id", "?"))
    return f"[{a.get('state', '?')}] {unit}: {a.get('message', a.get('summary', ''))}".strip()


async def list_alerts(api, args: dict) -> str:
    items = await api.alerts(state=args.get("state"))
    if not items:
        return "No active alerts."
    return "\n".join(fmt_alert(a) for a in items[:20])


async def ack(api, args: dict) -> str:
    await api.ack_alert(args["alert_id"])
    return f"Alert {args['alert_id']} acknowledged."


async def snooze(api, args: dict) -> str:
    mins = args.get("minutes", 60)
    await api.snooze_alert(args["alert_id"], mins)
    return f"Alert {args['alert_id']} snoozed for {mins} min."


_ID = {"type": "string", "maxLength": 64}


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "list_active_alerts",
            "List leak alerts, optionally filtered by alert state.",
            {
                "type": "object",
                "properties": {"state": {"type": "string", "enum": STATES}},
                "required": [],
                "additionalProperties": False,
            },
            list_alerts,
            Role.RESIDENT,
        )
    )
    reg.register(
        Tool(
            "acknowledge_alert",
            "Acknowledge an alert by id.",
            {
                "type": "object",
                "properties": {"alert_id": _ID},
                "required": ["alert_id"],
                "additionalProperties": False,
            },
            ack,
            Role.MAINTENANCE,
        )
    )
    reg.register(
        Tool(
            "snooze_alert",
            "Snooze an alert for a number of minutes.",
            {
                "type": "object",
                "properties": {"alert_id": _ID, "minutes": {"type": "integer", "minimum": 5, "maximum": 1440}},
                "required": ["alert_id"],
                "additionalProperties": False,
            },
            snooze,
            Role.MAINTENANCE,
        )
    )

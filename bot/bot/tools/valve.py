from bot.auth import Principal, Role
from bot.tools.alerts import acted_by
from bot.tools.registry import Tool, ToolRegistry


async def set_valve(api, args: dict, p: Principal) -> str:
    cmd = await api.set_valve(args["unit"], args["action"], p.on_behalf_of)
    status = cmd.get("status", "sent")
    unit, action = args["unit"], args["action"]
    if status == "confirmed":
        done = "closed" if action == "close" else "opened"
        text = f"Valve for {unit}: {action} command confirmed by the device (valve {done})."
    else:
        text = f"Valve for {unit}: {action} command {status}; NOT confirmed by the device yet."
    return text + acted_by(cmd)


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "set_valve",
            "Open or close the shut-off valve of a unit. Admin only; callers must confirm.",
            {
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "maxLength": 32, "pattern": r"^[A-Za-z0-9-]+$"},
                    "action": {"type": "string", "enum": ["open", "close"]},
                },
                "required": ["unit", "action"],
                "additionalProperties": False,
            },
            set_valve,
            Role.ADMIN,
        )
    )

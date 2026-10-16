from bot.auth import Principal, Role
from bot.tools.registry import Tool, ToolRegistry


async def set_valve(api, args: dict, p: Principal) -> str:
    cmd = await api.set_valve(args["unit"], args["action"], p.actor)
    return f"Valve for {args['unit']}: {args['action']} command {cmd.get('status', 'sent')}."


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

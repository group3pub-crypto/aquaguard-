from bot.auth import Role
from bot.tools.registry import Tool, ToolRegistry


async def set_valve(api, args: dict, actor: str) -> str:
    await api.set_valve(args["unit"], args["action"], actor)
    return f"Valve for {args['unit']}: {args['action']} command sent."


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "set_valve",
            "Open or close the shut-off valve of a unit. Admin only; callers must confirm.",
            {
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "maxLength": 32},
                    "action": {"type": "string", "enum": ["open", "close"]},
                },
                "required": ["unit", "action"],
                "additionalProperties": False,
            },
            set_valve,
            Role.ADMIN,
        )
    )

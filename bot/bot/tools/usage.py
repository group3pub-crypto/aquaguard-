from bot.auth import Role
from bot.tools.registry import Tool, ToolRegistry


async def get_usage(api, args: dict, actor: str) -> str:
    data = await api.usage_summary(args["unit"], args.get("period", "today"))
    return "\n".join(f"{k}: {v}" for k, v in data.items()) or "No usage data."


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            name="get_usage_summary",
            description="Water usage of one flat for today, this week or this month.",
            parameters={
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "description": "Flat unit number, e.g. A-101"},
                    "period": {"type": "string", "enum": ["today", "week", "month"]},
                },
                "required": ["unit"],
                "additionalProperties": False,
            },
            handler=get_usage,
            required_role=Role.RESIDENT,
        )
    )

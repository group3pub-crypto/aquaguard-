from bot.auth import Role
from bot.tools.registry import Tool, ToolRegistry


async def get_usage(api, args: dict) -> str:
    data = await api.usage_summary(unit=args.get("unit"), days=args.get("days", 1))
    return (
        f"Usage ({data.get('period', 'period')}): {data.get('total_litres', '?')} L total, "
        f"{data.get('units', '?')} units, avg {data.get('avg_litres_per_unit', '?')} L/unit"
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            name="get_usage_summary",
            description="Water usage summary for the society or one unit over the last N days.",
            parameters={
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "description": "Unit/flat id, e.g. A-101"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 90},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=get_usage,
            required_role=Role.RESIDENT,
        )
    )

from bot.auth import Principal, Role
from bot.tools.registry import Tool, ToolError, ToolRegistry
from bot.tools.scope import scoped_flat


def fmt_usage(unit: str, data: dict) -> str:
    hours = data.get("hours", "?")
    lines = [
        f"{unit} - last {hours} h",
        f"Used: {data.get('volume_l', 0):.1f} L (avg {data.get('avg_lpm', 0):.3f} L/min)",
    ]
    expected = data.get("expected_l")
    if expected is None:
        lines.append("Expected: n/a (no baseline yet)")
    else:
        lines.append(f"Expected: {expected:.1f} L (baseline)")
    return "\n".join(lines)


async def get_usage(api, args: dict, p: Principal) -> str:
    flat_id, unit = await scoped_flat(api, p, args.get("unit"))
    if flat_id is None or unit is None:
        raise ToolError("missing argument: unit")
    data = await api.flat_usage(flat_id, args.get("hours", 24))
    return fmt_usage(unit, data)


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            name="get_usage_summary",
            description="Water usage of one flat over the last N hours (default 24). "
            "Residents get their own flat; staff must name a unit.",
            parameters={
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "maxLength": 32, "pattern": r"^[A-Za-z0-9-]+$"},
                    "hours": {"type": "integer", "minimum": 1, "maximum": 720},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=get_usage,
            required_role=Role.RESIDENT,
        )
    )

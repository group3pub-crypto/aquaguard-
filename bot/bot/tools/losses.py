from bot.auth import Role
from bot.tools.registry import Tool, ToolRegistry


async def top_losses(api, args: dict, actor: str) -> str:
    rows = await api.top_losses(limit=args.get("limit", 5))
    if isinstance(rows, dict):
        rows = rows.get("losses") or rows.get("flats") or []
    if not rows:
        return "No losses recorded."
    return "\n".join(f"{i}. {r.get('unit', '?')}: {r.get('litres_lost', '?')} L" for i, r in enumerate(rows, 1))


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "top_losses",
            "Units with the largest estimated water loss.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                "required": [],
                "additionalProperties": False,
            },
            top_losses,
            Role.RESIDENT,
        )
    )

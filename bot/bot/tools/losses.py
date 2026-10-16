from bot.auth import Principal, Role
from bot.tools.registry import Tool, ToolRegistry


def fmt_losses(data: dict) -> str:
    rows = data.get("losses") or []
    if not rows:
        return f"No losses recorded in the last {data.get('days', '?')} days."
    lines = [f"Top estimated losses, last {data.get('days', '?')} days"]
    lines += [f"{i}. {r.get('unit', '?')}: {r.get('estimated_loss_l', 0):.1f} L" for i, r in enumerate(rows, 1)]
    return "\n".join(lines)


async def top_losses(api, args: dict, p: Principal) -> str:
    return fmt_losses(await api.top_losses(days=args.get("days", 7), limit=args.get("limit", 5)))


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "top_losses",
            "Units with the largest estimated water loss (staff only: it spans all flats).",
            {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 90},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": [],
                "additionalProperties": False,
            },
            top_losses,
            Role.MAINTENANCE,
        )
    )

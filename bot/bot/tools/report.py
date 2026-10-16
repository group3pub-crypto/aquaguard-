from bot.auth import Role
from bot.tools.registry import Tool, ToolRegistry


async def report(api, args: dict, actor: str) -> str:
    r = await api.report()
    return "\n".join(f"{k}: {v}" for k, v in r.items()) or "Report is empty."


def register(reg: ToolRegistry) -> None:
    reg.register(
        Tool(
            "generate_report",
            "Summary report: usage, estimated losses and alert counts.",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            report,
            Role.RESIDENT,
        )
    )

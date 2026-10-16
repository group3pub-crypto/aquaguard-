import re

from bot.auth import Principal, Role
from bot.tools.registry import Tool, ToolRegistry

MAX_CHARS = 3500  # Telegram messages cap at 4096 before escaping


def fmt_report(markdown: str) -> str:
    """The backend returns markdown; the bot sends plain text, so drop the markup."""
    text = re.sub(r"^#+\s*", "", markdown, flags=re.M)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
    text = text.strip()
    return text[:MAX_CHARS] + ("\n..." if len(text) > MAX_CHARS else "") or "Report is empty."


def fmt_status(s: dict) -> str:
    return (
        f"Flats monitored: {s.get('flats', '?')}\n"
        f"Active alerts: {s.get('active_alerts', '?')}\n"
        f"Volume (24 h): {s.get('volume_24h_l', 0):.1f} L\n"
        f"Sensors offline: {s.get('sensors_offline', '?')}"
    )


async def report(api, args: dict, p: Principal) -> str:
    return fmt_report(await api.weekly_report())


async def system_status(api, args: dict, p: Principal) -> str:
    return fmt_status(await api.society_stats())


def register(reg: ToolRegistry) -> None:
    empty = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    reg.register(
        Tool(
            "generate_report",
            "Weekly society report: consumption, anomalies, alerts, valve commands, top losses (staff only).",
            empty,
            report,
            Role.MAINTENANCE,
        )
    )
    reg.register(
        Tool("system_status", "Society-wide counts: flats, active alerts, 24 h volume, offline sensors.", empty,
             system_status, Role.MAINTENANCE)
    )

"""Server-side (bot-side) data scoping: residents only ever see their bound flat."""

from __future__ import annotations

from bot.auth import Principal, Role
from bot.tools.registry import PermissionDenied


async def scoped_flat(api, principal: Principal, unit: str | None) -> tuple[str | None, str | None]:
    """Return (flat_id, unit) the caller may query; (None, None) means society-wide (staff only)."""
    if principal.role == Role.RESIDENT:
        if not principal.flat_id or not principal.unit:
            raise PermissionDenied("no flat is bound to your account")
        if unit and unit.strip().lower() != principal.unit.lower():
            raise PermissionDenied("residents may only access their own flat")
        return principal.flat_id, principal.unit
    if unit:
        flat = await api.find_flat(unit)
        return str(flat["id"]), str(flat["unit_number"])
    return None, None


def can_see_alert(principal: Principal, alert: dict) -> bool:
    if principal.role >= Role.MAINTENANCE:
        return True
    return bool(principal.flat_id) and str(alert.get("flat_id")) == principal.flat_id

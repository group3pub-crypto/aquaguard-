"""Admin-side user management rules (binding, role changes, deactivation)."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import FlatRepository, SocietyRepository
from app.services import auth as auth_svc


async def resolve_binding(
    session: AsyncSession,
    role: str,
    flat_id: uuid.UUID | None,
    society_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Validate a (flat, society) binding for `role`; returns the normalised pair.

    resident -> flat (society derived from the flat); maintenance -> society; admin -> none.
    Raises HTTPException 404 (unknown id) / 422 (not allowed for that role).
    """
    if role == "admin":
        if flat_id or society_id:
            raise HTTPException(422, "admin accounts are not bound to a flat or society")
        return None, None
    if role == "maintenance":
        if flat_id:
            raise HTTPException(422, "maintenance accounts bind to a society, not a flat")
        if society_id and await SocietyRepository(session).get(society_id) is None:
            raise HTTPException(404, "Society not found")
        return None, society_id
    # resident
    if society_id and not flat_id:
        raise HTTPException(422, "residents bind to a flat (flat_id)")
    if flat_id is None:
        return None, None
    flat = await FlatRepository(session).get(flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    if society_id and society_id != flat.society_id:
        raise HTTPException(422, "society_id does not match the flat's society")
    return flat.id, flat.society_id


async def change_role(session: AsyncSession, user: User, new_role: str) -> bool:
    """Set the role; clears the binding (it was for the old role) and revokes live sessions.
    Returns True if the binding was cleared. 409 on demoting the last admin."""
    if new_role == user.role:
        return False
    if user.role == "admin":
        try:
            await auth_svc.ensure_not_last_admin(session, user)
        except auth_svc.LastAdminError as exc:
            raise HTTPException(409, str(exc)) from None
    cleared = bool(user.flat_id or user.society_id)
    user.role = new_role
    user.flat_id = user.society_id = None
    await auth_svc.revoke_all_sessions(session, user)
    return cleared


async def set_active(session: AsyncSession, user: User, active: bool) -> None:
    if active == user.is_active:
        return
    if not active:
        try:
            await auth_svc.ensure_not_last_admin(session, user)
        except auth_svc.LastAdminError as exc:
            raise HTTPException(409, str(exc)) from None
    user.is_active = active
    await auth_svc.revoke_all_sessions(session, user)

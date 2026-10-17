"""Scoped object lookups shared by the routers: out-of-scope == 404 (never 403)."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Flat, Society
from app.repositories import FlatRepository, SocietyRepository
from app.scope import Scope


async def flat_in_scope(session: AsyncSession, scope: Scope, flat_id: uuid.UUID) -> Flat:
    flat = await FlatRepository(session).get_scoped(flat_id, scope)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    return flat


async def society_in_scope(session: AsyncSession, scope: Scope, society_id: uuid.UUID) -> Society:
    society = await SocietyRepository(session).get_scoped(society_id, scope)
    if society is None:
        raise HTTPException(404, "Society not found")
    return society


def require_staff_scope(scope: Scope) -> None:
    """Society-wide aggregates (stats, reports, loss rankings) are for staff, not residents."""
    if not scope.allows_society_stats():
        raise HTTPException(403, "Insufficient permissions")

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.models import AuditLog
from app.repositories.base import BaseRepository


class AuditRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def record(
        self,
        *,
        entity_type: str,
        entity_id: Any,
        action: str,
        actor: str,
        details: dict | None = None,
        flat_id: uuid.UUID | None = None,
        society_id: uuid.UUID | None = None,
    ) -> AuditLog:
        return await self.add(
            AuditLog(
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id is not None else None,
                action=action,
                actor=actor,
                details=details,
                flat_id=flat_id,
                society_id=society_id,
            )
        )

    async def find(
        self, *, entity_type: str | None = None, action: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.id.desc())
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        return await self._all(stmt.limit(limit).offset(offset))

"""Refresh tokens and single-use WebSocket tickets (both stored only as SHA-256 hashes)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, UTCDateTime, new_uuid, utcnow


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # every token minted by rotation shares the family of the login that started it
    family_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=sa.func.now()
    )
    user_agent: Mapped[str | None] = mapped_column(sa.String(300))
    ip: Mapped[str | None] = mapped_column(sa.String(64))


class WsTicket(Base):
    __tablename__ = "ws_tickets"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=sa.func.now()
    )

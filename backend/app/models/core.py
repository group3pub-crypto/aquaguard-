from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime, new_uuid, utcnow

VALVE_STATES = ("open", "closed", "unknown")
BASELINE_STATUSES = ("learning", "ready", "stale")
ROLES = ("resident", "admin", "maintenance")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Society(TimestampMixin, Base):
    __tablename__ = "societies"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    address: Mapped[str | None] = mapped_column(sa.String(500))
    total_flats: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")


class Flat(TimestampMixin, Base):
    __tablename__ = "flats"
    __table_args__ = (
        sa.UniqueConstraint("society_id", "unit_number", name="uq_flats_society_unit"),
        sa.CheckConstraint(_in("valve_state", VALVE_STATES), name="valve_state"),
        sa.CheckConstraint(_in("baseline_status", BASELINE_STATUSES), name="baseline_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    society_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("societies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unit_number: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    floor: Mapped[int | None] = mapped_column(sa.Integer)
    residents: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1, server_default="1")
    sensor_id: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    valve_state: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="unknown", server_default="unknown"
    )
    baseline_status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="learning", server_default="learning"
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (sa.CheckConstraint(_in("role", ROLES), name="role"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(sa.String(50), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    role: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="resident", server_default="resident"
    )
    telegram_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True)
    flat_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("flats.id", ondelete="SET NULL"))
    society_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("societies.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=sa.func.now()
    )

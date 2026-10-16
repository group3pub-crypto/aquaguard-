from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigPK, JSONType, UTCDateTime, new_uuid, utcnow


class FlowReading(Base):
    __tablename__ = "flow_readings"
    __table_args__ = (
        sa.UniqueConstraint("flat_id", "message_id", name="uq_flow_readings_flat_message"),
        sa.Index("ix_flow_readings_flat_ts", "flat_id", "timestamp"),
        sa.Index("ix_flow_readings_timestamp", "timestamp"),  # retention scans
    )

    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    flow_rate: Mapped[float] = mapped_column(sa.Float, nullable=False)
    volume: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    pressure: Mapped[float | None] = mapped_column(sa.Float)
    temperature: Mapped[float | None] = mapped_column(sa.Float)
    battery_pct: Mapped[int | None] = mapped_column(sa.Integer)
    rssi: Mapped[int | None] = mapped_column(sa.Integer)
    message_id: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=sa.func.now()
    )


class Baseline(Base):
    __tablename__ = "baselines"
    __table_args__ = (
        sa.UniqueConstraint("flat_id", "version", name="uq_baselines_flat_version"),
        sa.Index(
            "uq_baselines_one_active",
            "flat_id",
            unique=True,
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    slots: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    trained_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    trained_to: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sample_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (sa.Index("ix_forecasts_flat_generated", "flat_id", "generated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    horizon_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    predictions: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    mape: Mapped[float | None] = mapped_column(sa.Float)


class SensorHealth(Base):
    __tablename__ = "sensor_health"
    __table_args__ = (
        sa.CheckConstraint("status IN ('online', 'late', 'offline')", name="status"),
        sa.Index("ix_sensor_health_last_seen", "last_seen"),
    )

    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), primary_key=True
    )
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, default="offline", server_default="offline"
    )
    missed_readings: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow, server_default=sa.func.now()
    )

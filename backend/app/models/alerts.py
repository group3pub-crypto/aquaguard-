from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigPK, JSONType, TimestampMixin, UTCDateTime, new_uuid, utcnow
from app.models.core import _in

ANOMALY_TYPES = ("leak", "burst", "overuse", "running_toilet", "dripping_tap", "unknown")
ALERT_STATES = (
    "nudge",
    "alert",
    "ticket",
    "auto_shutoff",
    "acknowledged",
    "snoozed",
    "overridden",
    "resolved",
)
VALVE_SOURCES = ("auto", "manual_dashboard", "manual_bot", "manual_override")
CMD_STATUSES = ("pending", "confirmed", "failed", "timeout")
OPEN_STATES = ("nudge", "alert", "ticket", "auto_shutoff", "acknowledged", "snoozed")


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        sa.CheckConstraint(_in("anomaly_type", ANOMALY_TYPES), name="anomaly_type"),
        sa.Index("ix_anomalies_flat_detected", "flat_id", "detected_at"),
        sa.Index(
            "ix_anomalies_unresolved",
            "flat_id",
            postgresql_where=sa.text("resolved_at IS NULL"),
            sqlite_where=sa.text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), nullable=False
    )
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    anomaly_type: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    severity: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    shap_values: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    explanation: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    baseline_version: Mapped[int | None] = mapped_column(sa.Integer)
    window_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    window_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    est_loss_lpm: Mapped[float | None] = mapped_column(sa.Float)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=sa.func.now()
    )


class Alert(TimestampMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = (
        sa.CheckConstraint(_in("state", ALERT_STATES), name="state"),
        sa.CheckConstraint("tier BETWEEN 1 AND 4", name="tier"),
        sa.Index(
            "ix_alerts_open",
            "flat_id",
            "state",
            postgresql_where=sa.text("state NOT IN ('resolved', 'overridden')"),
            sqlite_where=sa.text("state NOT IN ('resolved', 'overridden')"),
        ),
        # escalation scan: candidates are open alerts by tier whose delay may have elapsed
        sa.Index(
            "ix_alerts_escalation",
            "tier",
            "created_at",
            postgresql_where=sa.text("state IN ('nudge', 'alert', 'ticket')"),
            sqlite_where=sa.text("state IN ('nudge', 'alert', 'ticket')"),
        ),
        sa.Index(
            "ix_alerts_snoozed",
            "snoozed_until",
            postgresql_where=sa.text("state = 'snoozed'"),
            sqlite_where=sa.text("state = 'snoozed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    anomaly_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("anomalies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    tier: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    severity_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    message: Mapped[str | None] = mapped_column(sa.Text)
    message_source: Mapped[str | None] = mapped_column(sa.String(30))
    escalated_at: Mapped[list[str] | None] = mapped_column(JSONType)
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(sa.String(100))
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # auto shut-off safety: warning issued at (the close runs SHUTOFF_WARNING_SECONDS later) and
    # the moment we gave up after a failed close + one retry
    shutoff_warned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    shutoff_gave_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ValveCommand(Base):
    __tablename__ = "valve_commands"
    __table_args__ = (
        sa.CheckConstraint("command IN ('open', 'close')", name="command"),
        sa.CheckConstraint(_in("source", VALVE_SOURCES), name="source"),
        sa.CheckConstraint(_in("status", CMD_STATUSES), name="status"),
        sa.Index(
            "ix_valve_commands_pending",
            "flat_id",
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        ),
        sa.Index("ix_valve_commands_alert_issued", "alert_id", "issued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid)
    flat_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("alerts.id", ondelete="SET NULL")
    )
    command: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    issued_by: Mapped[str | None] = mapped_column(sa.String(100))
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="pending", server_default="pending"
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_log_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigPK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, server_default=sa.func.now()
    )
    entity_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(sa.String(64))
    action: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    flat_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("flats.id", ondelete="SET NULL")
    )
    society_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("societies.id", ondelete="SET NULL")
    )

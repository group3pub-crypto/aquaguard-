from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ORM


class SocietyOut(ORM):
    id: UUID
    name: str
    slug: str
    address: str | None = None
    total_flats: int
    created_at: datetime


class SocietyStats(BaseModel):
    society_id: UUID
    flats: int
    active_alerts: int
    volume_24h_l: float
    sensors_offline: int


class SensorStatus(BaseModel):
    status: Literal["online", "late", "offline"]
    last_seen: datetime | None = None
    missed_readings: int = 0


class FlatOut(ORM):
    id: UUID
    society_id: UUID
    unit_number: str
    floor: int | None = None
    residents: int
    sensor_id: str
    valve_state: str
    baseline_status: str


class FlatDetail(FlatOut):
    current_flow: float | None = None
    last_reading_at: datetime | None = None
    sensor_status: SensorStatus


class ReadingOut(ORM):
    id: int
    flat_id: UUID
    timestamp: datetime
    flow_rate: float
    volume: float
    pressure: float | None = None
    temperature: float | None = None
    battery_pct: int | None = None
    rssi: int | None = None
    total_volume: float | None = None
    battery_volts: float | None = None
    device_valve_state: str | None = None
    firmware_version: str | None = None
    fault_codes: list[str] | None = None


class BaselineOut(ORM):
    id: UUID
    flat_id: UUID
    version: int
    slots: list[dict[str, Any]]
    trained_from: datetime | None = None
    trained_to: datetime | None = None
    sample_days: int
    is_active: bool


class ForecastOut(ORM):
    id: UUID
    flat_id: UUID
    generated_at: datetime
    horizon_hours: int
    predictions: list[dict[str, Any]]
    model_version: str
    mape: float | None = None


class ForecastRequest(BaseModel):
    horizon_hours: int = Field(24, ge=1, le=168)


class AnomalyOut(ORM):
    id: UUID
    flat_id: UUID
    detected_at: datetime
    anomaly_type: str
    confidence: float
    severity: float
    features: dict[str, Any] | None = None
    shap_values: list[dict[str, Any]] | None = None
    explanation: dict[str, Any] | None = None
    baseline_version: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    est_loss_lpm: float | None = None
    resolved_at: datetime | None = None


class AlertOut(ORM):
    id: UUID
    flat_id: UUID
    anomaly_id: UUID
    state: str
    tier: int
    severity_score: float
    message: str | None = None
    message_source: str | None = None
    escalated_at: list[str] | None = None
    snoozed_until: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    shutoff_warned_at: datetime | None = None
    shutoff_gave_up_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AssertedActor(BaseModel):
    """Echo of an `on_behalf_of` assertion by a SERVICE account: recorded, never verified."""

    on_behalf_of: str | None = Field(
        None, description="Asserted by a service account; NOT verified by the backend"
    )
    on_behalf_of_verified: bool | None = Field(
        None, description="Always false when on_behalf_of is set"
    )


class AlertActionOut(AlertOut, AssertedActor):
    pass


OnBehalfOf = Field(
    None,
    max_length=100,
    description="Service accounts only: end user the action is performed for. Recorded in the "
    "audit trail as asserted, not verified. Other callers get 422.",
)


class AcknowledgeRequest(BaseModel):
    on_behalf_of: str | None = OnBehalfOf
    acknowledged_by: str | None = Field(
        None, max_length=100, deprecated=True, description="Ignored; the actor is the caller"
    )


class SnoozeRequest(BaseModel):
    duration_minutes: Literal[15, 30, 60, 120]
    on_behalf_of: str | None = OnBehalfOf
    snoozed_by: str | None = Field(
        None, max_length=100, deprecated=True, description="Ignored; the actor is the caller"
    )


class OverrideRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    on_behalf_of: str | None = OnBehalfOf
    overridden_by: str | None = Field(
        None, max_length=100, deprecated=True, description="Ignored; the actor is the caller"
    )


class ResolveRequest(BaseModel):
    on_behalf_of: str | None = OnBehalfOf
    resolved_by: str | None = Field(
        None, max_length=100, deprecated=True, description="Ignored; the actor is the caller"
    )


class ValveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["manual_dashboard", "manual_bot", "manual_override"] = "manual_dashboard"
    reason: str | None = Field(None, max_length=500)
    on_behalf_of: str | None = OnBehalfOf
    issued_by: str | None = Field(
        None, max_length=100, deprecated=True, description="Ignored; the actor is the caller"
    )


class ValveCommandOut(ORM):
    id: UUID
    flat_id: UUID
    alert_id: UUID | None = None
    command: str
    source: str
    issued_by: str | None = None
    issued_at: datetime
    confirmed_at: datetime | None = None
    status: str


class ValveStatus(BaseModel):
    flat_id: UUID
    valve_state: str
    pending: list[ValveCommandOut]
    recent: list[ValveCommandOut]


class ValveCommandActionOut(ValveCommandOut, AssertedActor):
    pass

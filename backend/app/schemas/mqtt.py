"""Wire formats for MQTT payloads."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

MAX_FLOW_LPM = 500.0
MAX_PAYLOAD_BYTES = 2048
MAX_FUTURE_SKEW_S = 300


class FlowPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    v: Literal[1]
    id: str = Field(pattern=r"^[0-9a-fA-F]{8}$")
    ts: float = Field(gt=0)
    flow: float = Field(ge=0, le=MAX_FLOW_LPM)
    vol: float = Field(ge=0, le=100_000)
    psi: float | None = Field(None, ge=0, le=500)
    tmp: float | None = Field(None, ge=-50, le=150)
    bat: int | None = Field(None, ge=0, le=100)  # percent; decimals (99.9) are rounded
    rssi: int | None = Field(None, ge=-150, le=0)
    # Optional firmware extras, persisted by the ingest service (see FlowReading). Unknown keys
    # stay ignored.
    tot: float | None = Field(None, ge=0, le=1e9)  # cumulative litres
    vbat: float | None = Field(None, ge=2.5, le=4.5)  # battery volts
    valve: Literal["open", "closed"] | None = None  # valve position seen by the device
    fw: str | None = Field(None, max_length=32)
    err: list[Annotated[str, Field(min_length=1, max_length=24)]] | None = Field(
        None, max_length=8
    )  # short sensor-fault codes

    @field_validator("bat", mode="before")
    @classmethod
    def _round_bat(cls, v: object) -> object:
        if isinstance(v, bool):
            raise ValueError("bat must be a number")
        if isinstance(v, int | float) and v == v and abs(v) != float("inf"):
            return round(v)
        return v

    @field_validator("ts")
    @classmethod
    def _not_future(cls, v: float, info: ValidationInfo) -> float:
        # the allowed skew comes from Settings.mqtt_max_future_skew_s (validation context); the
        # accelerated edge simulator (--speed > 1) runs ahead of wall clock and needs a larger value
        skew = (info.context or {}).get("max_future_skew_s", MAX_FUTURE_SKEW_S)
        if v > time.time() + skew:
            raise ValueError("timestamp is in the future")
        return v


class HeartbeatPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    v: Literal[1]
    ts: float = Field(gt=0)
    uptime: int | None = Field(None, ge=0)
    bat: float | None = Field(None, ge=0, le=100)
    fw: str | None = Field(None, max_length=50)


class ValveAckPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: Literal[1]
    cmd_id: str = Field(min_length=1, max_length=16)
    state: Literal["open", "closed"]
    ts: float = Field(gt=0)

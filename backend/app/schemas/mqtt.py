"""Wire formats for MQTT payloads."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    bat: int | None = Field(None, ge=0, le=100)
    rssi: int | None = Field(None, ge=-150, le=0)

    @field_validator("ts")
    @classmethod
    def _not_future(cls, v: float) -> float:
        if v > time.time() + MAX_FUTURE_SKEW_S:
            raise ValueError("timestamp is in the future")
        return v


class HeartbeatPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    v: Literal[1]
    ts: float = Field(gt=0)
    uptime: int | None = Field(None, ge=0)
    bat: int | None = Field(None, ge=0, le=100)
    fw: str | None = Field(None, max_length=50)


class ValveAckPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: Literal[1]
    cmd_id: str = Field(min_length=1, max_length=16)
    state: Literal["open", "closed"]
    ts: float = Field(gt=0)

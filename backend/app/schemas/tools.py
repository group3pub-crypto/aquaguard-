from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ActiveAlertsRequest(BaseModel):
    society_id: UUID | None = None
    flat_id: UUID | None = None
    limit: int = Field(10, ge=1, le=100)


class FlatUsageRequest(BaseModel):
    flat_id: UUID
    hours: int = Field(24, ge=1, le=24 * 30)


class TopLossesRequest(BaseModel):
    society_id: UUID
    days: int = Field(7, ge=1, le=90)
    limit: int = Field(5, ge=1, le=50)


class ToolValveRequest(BaseModel):
    flat_id: UUID
    action: Literal["open", "close"]
    reason: str | None = Field(None, max_length=500)
    on_behalf_of: str | None = Field(None, max_length=100)  # service accounts only
    issued_by: str | None = Field(None, max_length=100, deprecated=True)  # ignored


class WeeklyReportRequest(BaseModel):
    society_id: UUID

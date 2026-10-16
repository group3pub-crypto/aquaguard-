from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, ConfigDict

Limit = Annotated[int, Query(ge=1, le=500, description="Page size (max 500)")]
Offset = Annotated[int, Query(ge=0)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Msg(BaseModel):
    detail: str


__all__ = ["ORM", "UUID", "Limit", "Msg", "Offset", "datetime"]

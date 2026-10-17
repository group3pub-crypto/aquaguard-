"""Request bodies of the admin-only onboarding endpoints (societies, flats, users)."""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import Role, StrongPassword, Username

# slug and unit end up in MQTT topics and in the broker device username "<slug>/<unit>":
# keep them to characters that are safe in both (no '/', '+', '#', no '.' in the unit)
SLUG_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"
UNIT_RE = r"^[A-Za-z0-9_-]{1,50}$"
SENSOR_RE = r"^[A-Za-z0-9_.-]{1,100}$"
SLUG_PATTERN = re.compile(SLUG_RE)


class SocietyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_RE)
    address: str | None = Field(None, max_length=500)


class SocietyUpdate(BaseModel):
    """The slug is immutable: it is part of every MQTT topic and device credential."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=200)
    address: str | None = Field(None, max_length=500)


class FlatCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    society_id: UUID
    unit_number: str = Field(pattern=UNIT_RE)
    floor: int | None = Field(None, ge=-5, le=300)
    residents: int = Field(1, ge=0, le=50)
    sensor_id: str | None = Field(None, pattern=SENSOR_RE)  # default: "<slug>.<unit>"


class FlatUpdate(BaseModel):
    """Unit number and society are immutable (they name the MQTT topics)."""

    model_config = ConfigDict(extra="forbid")
    floor: int | None = Field(None, ge=-5, le=300)
    residents: int | None = Field(None, ge=0, le=50)
    sensor_id: str | None = Field(None, pattern=SENSOR_RE)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Username
    password: StrongPassword
    name: str = Field(min_length=1, max_length=200)
    role: Role = "resident"
    flat_id: UUID | None = None
    society_id: UUID | None = None
    is_service: bool = False


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=200)
    role: Role | None = None
    is_active: bool | None = None
    is_service: bool | None = None
    telegram_id: int | None = Field(None, ge=1)


class BindingUpdate(BaseModel):
    """resident -> flat_id; maintenance -> society_id; admin -> neither. Null/null unbinds."""

    model_config = ConfigDict(extra="forbid")
    flat_id: UUID | None = None
    society_id: UUID | None = None


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: StrongPassword

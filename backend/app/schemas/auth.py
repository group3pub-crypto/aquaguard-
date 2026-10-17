from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.passwords import PasswordPolicyError, validate_password
from app.schemas.common import ORM

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
Role = Literal["resident", "admin", "maintenance"]


def _username(v: str) -> str:
    v = v.strip()
    if not USERNAME_RE.match(v):
        raise ValueError("username may only contain letters, digits, '_', '.', '-'")
    return v.lower()  # usernames are case-insensitive: stored and compared lowercase


def _password(v: str) -> str:
    try:
        return validate_password(v)
    except PasswordPolicyError as exc:
        raise ValueError(str(exc)) from None


Username = Annotated[str, Field(min_length=3, max_length=50), AfterValidator(_username)]
# NOTE: policy is checked in BYTES (bcrypt truncates at 72 bytes) -> 422, never silent truncation
StrongPassword = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_password)]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Username
    password: StrongPassword
    name: str = Field(min_length=1, max_length=200)

    @field_validator("password")
    @classmethod
    def _not_username(cls, v: str, info) -> str:
        name = (info.data or {}).get("username")
        if name and v.strip().lower() == name:
            raise ValueError("password must not equal the username")
        return v


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=20, max_length=200)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: StrongPassword


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    role: Role
    expires_in: int  # access token lifetime, seconds
    refresh_expires_in: int  # refresh token lifetime, seconds


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


class UserOut(ORM):
    id: UUID
    username: str
    name: str
    role: Role
    flat_id: UUID | None = None
    society_id: UUID | None = None
    is_active: bool = True
    is_service: bool = False
    telegram_id: int | None = None
    created_at: datetime

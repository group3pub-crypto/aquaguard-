from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import ORM

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
Role = Literal["resident", "admin", "maintenance"]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=72)
    name: str = Field(min_length=1, max_length=200)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        if not USERNAME_RE.match(v):
            raise ValueError("username may only contain letters, digits, '_', '.', '-'")
        return v

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        if not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
            raise ValueError("password must contain at least one letter and one digit")
        return v


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: Role
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
    created_at: datetime

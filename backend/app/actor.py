"""Who performed an action: always the authenticated account, never a body field.

Service accounts (the chat bot) may additionally *assert* an end user via ``on_behalf_of``.
That value is stored in the audit trail and echoed back clearly labelled as asserted, not
verified: the backend cannot prove the bot's claim, it only records it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.models import User

MAX_ON_BEHALF_OF = 100
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def sanitize_on_behalf_of(value: str | None) -> str | None:
    """Strip control characters and collapse whitespace; empty -> None."""
    if value is None:
        return None
    cleaned = " ".join(_CONTROL.sub(" ", value).split())[:MAX_ON_BEHALF_OF]
    return cleaned or None


@dataclass(frozen=True)
class Actor:
    username: str  # authenticated account (audit_log.actor, valve_commands.issued_by, ...)
    on_behalf_of: str | None = None  # asserted by a service account, NOT verified

    def audit_details(self) -> dict[str, Any]:
        if self.on_behalf_of is None:
            return {}
        return {"on_behalf_of": self.on_behalf_of, "on_behalf_of_verified": False}

    def echo(self) -> dict[str, Any]:
        return {
            "on_behalf_of": self.on_behalf_of,
            "on_behalf_of_verified": False if self.on_behalf_of else None,
        }


def resolve_actor(user: User, on_behalf_of: str | None) -> Actor:
    """Actor for a request. 422 when a non-service caller tries to assert another identity."""
    asserted = sanitize_on_behalf_of(on_behalf_of)
    if asserted is not None and not user.is_service:
        raise HTTPException(
            422,
            "on_behalf_of is only accepted from service accounts",
        )
    return Actor(user.username, asserted)

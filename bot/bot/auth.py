"""Roles and the telegram-user <-> backend-account link store.

Admins come only from TELEGRAM_ADMIN_IDS. Anyone else sends `/link <backend_username>`;
an admin approves with `/approve <request_id> <flat_unit>` (resident bound to that flat) or
`/approve <request_id> maintenance` (staff, all flats, no valve). Role escalation to admin
is impossible through the bot. The binding {telegram_id -> username, role, flat} is what the
bot uses to scope data: the bot's backend account is an admin/staff service account, so
scoping of residents is enforced here, not by the backend.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from bot.fsutil import atomic_write_json

log = logging.getLogger("bot.auth")

LINK_RATE_MAX = 3  # requests ...
LINK_RATE_WINDOW = 3600.0  # ... per hour per telegram user
PENDING_TTL = 24 * 3600.0
MAX_PENDING = 50
MAX_RATE_TRACKED = 1000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


class Role(IntEnum):
    RESIDENT = 1
    MAINTENANCE = 2
    ADMIN = 3


def role_allows(user_role: Role | None, required: Role) -> bool:
    return user_role is not None and user_role >= required


class LinkError(Exception):
    pass


class RateLimited(LinkError):
    pass


class TooManyPending(LinkError):
    pass


class InvalidUsername(LinkError):
    pass


@dataclass(frozen=True)
class Principal:
    """Who is acting. Residents carry the flat they are bound to."""

    telegram_id: int
    role: Role
    username: str | None = None
    flat_id: str | None = None
    unit: str | None = None

    @property
    def actor(self) -> str:
        """String stored as `issued_by` in the backend / in the bot audit log."""
        return f"telegram:{self.telegram_id}" + (f"/{self.username}" if self.username else "")


class LinkStore:
    def __init__(
        self,
        admin_ids: set[int],
        path: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.admin_ids = set(admin_ids)
        self._path = Path(path) if path else None
        self._clock = clock
        # telegram_id -> {username, chat_id, role ("resident"|"maintenance"), flat_id, unit}
        self.links: dict[int, dict[str, Any]] = {}
        # request_id -> {telegram_id, chat_id, username, expires}
        self.pending: dict[str, dict[str, Any]] = {}
        self._attempts: dict[int, deque[float]] = {}
        self._load()

    # --- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not (self._path and self._path.exists()):
            return
        try:
            data = json.loads(self._path.read_text())
            for k, v in data.get("links", {}).items():
                # legacy entries (no role / flat binding) fail closed: re-link required
                if v.get("role") == "maintenance" or (v.get("role") == "resident" and v.get("flat_id")):
                    self.links[int(k)] = v
        except (OSError, ValueError, AttributeError):
            log.error("could not read link store %s", self._path, exc_info=True)
            self.links = {}

    def _save(self) -> None:
        if self._path:
            atomic_write_json(self._path, {"links": {str(k): v for k, v in self.links.items()}})

    # --- lookups -----------------------------------------------------------
    def principal_of(self, telegram_id: int) -> Principal | None:
        if telegram_id in self.admin_ids:
            return Principal(telegram_id, Role.ADMIN)
        link = self.links.get(telegram_id)
        if link is None:
            return None
        role = Role.MAINTENANCE if link["role"] == "maintenance" else Role.RESIDENT
        return Principal(telegram_id, role, link.get("username"), link.get("flat_id"), link.get("unit"))

    def role_of(self, telegram_id: int) -> Role | None:
        p = self.principal_of(telegram_id)
        return p.role if p else None

    def recipients(self) -> list[tuple[Principal, int]]:
        """(principal, chat_id) for everyone who should get proactive alerts."""
        out: list[tuple[Principal, int]] = [(Principal(a, Role.ADMIN), a) for a in sorted(self.admin_ids)]
        for tid, link in self.links.items():
            if tid in self.admin_ids:
                continue
            p = self.principal_of(tid)
            if p:
                out.append((p, int(link["chat_id"])))
        return out

    # --- link requests -----------------------------------------------------
    def _purge_pending(self) -> None:
        now = self._clock()
        for rid in [r for r, v in self.pending.items() if v["expires"] <= now]:
            del self.pending[rid]

    def _check_rate(self, telegram_id: int) -> None:
        now = self._clock()
        for tid in [t for t, d in self._attempts.items() if not d or d[-1] <= now - LINK_RATE_WINDOW]:
            del self._attempts[tid]
        if telegram_id not in self._attempts and len(self._attempts) >= MAX_RATE_TRACKED:
            raise RateLimited("too many link requests, try later")
        q = self._attempts.setdefault(telegram_id, deque())
        while q and q[0] <= now - LINK_RATE_WINDOW:
            q.popleft()
        if len(q) >= LINK_RATE_MAX:
            raise RateLimited("too many link requests, try later")
        q.append(now)

    def request_link(self, telegram_id: int, chat_id: int, username: str) -> str:
        """Register a pending request naming a backend username; returns the request id."""
        if not USERNAME_RE.match(username):
            raise InvalidUsername("invalid username")
        self._check_rate(telegram_id)
        self._purge_pending()
        for rid in [r for r, v in self.pending.items() if v["telegram_id"] == telegram_id]:
            del self.pending[rid]  # one open request per user
        if len(self.pending) >= MAX_PENDING:
            raise TooManyPending("too many pending requests, try later")
        rid = secrets.token_hex(3)
        self.pending[rid] = {
            "telegram_id": telegram_id,
            "chat_id": chat_id,
            "username": username,
            "expires": self._clock() + PENDING_TTL,
        }
        return rid

    def _require_admin(self, approver_id: int) -> None:
        if approver_id not in self.admin_ids:
            raise PermissionError("only admins can do that")

    def approve(
        self,
        approver_id: int,
        request_id: str,
        *,
        role: Role,
        flat_id: str | None = None,
        unit: str | None = None,
    ) -> dict[str, Any]:
        """Bind the requester. Residents need a flat; maintenance never has one; no admins."""
        self._require_admin(approver_id)
        if role == Role.ADMIN:
            raise PermissionError("admin cannot be granted through the bot")
        if role == Role.RESIDENT and not (flat_id and unit):
            raise ValueError("a resident must be bound to a flat")
        self._purge_pending()
        req = self.pending.get(request_id)
        if req is None:
            raise KeyError("no pending request")
        uname = req["username"].lower()
        for tid, link in self.links.items():
            if tid != req["telegram_id"] and link.get("username", "").lower() == uname:
                raise ValueError("that backend username is already linked to another telegram user")
        del self.pending[request_id]
        entry = {
            "username": req["username"],
            "chat_id": req["chat_id"],
            "role": "maintenance" if role == Role.MAINTENANCE else "resident",
            "flat_id": flat_id if role == Role.RESIDENT else None,
            "unit": unit if role == Role.RESIDENT else None,
        }
        self.links[req["telegram_id"]] = entry
        self._save()
        return {"telegram_id": req["telegram_id"], **entry}

    def reject(self, approver_id: int, request_id: str) -> dict[str, Any] | None:
        self._require_admin(approver_id)
        return self.pending.pop(request_id, None)

    def revoke(self, approver_id: int, target_id: int) -> bool:
        self._require_admin(approver_id)
        removed = self.links.pop(target_id, None)
        if removed is not None:
            self._save()
        return removed is not None

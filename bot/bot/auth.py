"""Roles and the telegram-user <-> account link store.

Admins come only from TELEGRAM_ADMIN_IDS. Residents request a link with
`/link <username>`; an admin approves via an inline button. Linked users are
always residents: role escalation is never possible through the bot.
"""

from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path


class Role(IntEnum):
    RESIDENT = 1
    MAINTENANCE = 2
    ADMIN = 3


def role_allows(user_role: Role | None, required: Role) -> bool:
    return user_role is not None and user_role >= required


class LinkStore:
    def __init__(self, admin_ids: set[int], path: str | None = None) -> None:
        self.admin_ids = set(admin_ids)
        self._path = Path(path) if path else None
        self.links: dict[int, dict] = {}  # telegram_id -> {username, chat_id}
        self.pending: dict[int, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self.links = {int(k): v for k, v in data.get("links", {}).items()}
            except (OSError, ValueError):
                self.links = {}

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"links": self.links}))

    def role_of(self, telegram_id: int) -> Role | None:
        if telegram_id in self.admin_ids:
            return Role.ADMIN
        if telegram_id in self.links:
            return Role.RESIDENT
        return None

    def request_link(self, telegram_id: int, chat_id: int, username: str) -> None:
        self.pending[telegram_id] = {"username": username, "chat_id": chat_id}

    def approve(self, approver_id: int, target_id: int) -> dict:
        if self.role_of(approver_id) != Role.ADMIN:
            raise PermissionError("only admins can approve links")
        req = self.pending.pop(target_id, None)
        if req is None:
            raise KeyError("no pending request")
        self.links[target_id] = req
        self._save()
        return req

    def reject(self, approver_id: int, target_id: int) -> dict | None:
        if self.role_of(approver_id) != Role.ADMIN:
            raise PermissionError("only admins can reject links")
        return self.pending.pop(target_id, None)

    def recipients(self) -> dict[int, int]:
        """telegram_id -> chat_id for everyone who should get proactive alerts."""
        out = {tid: v["chat_id"] for tid, v in self.links.items()}
        for aid in self.admin_ids:
            out.setdefault(aid, aid)  # private chat id == user id
        return out

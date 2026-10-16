"""Data scope of a caller: which societies / flats it may see or act on.

    admin        -> everything
    maintenance  -> the society they are bound to (none when unbound)
    resident     -> only the flat they are bound to; the read-only summary (name, address) of
                    that flat's society (none when unbound)

Scope is enforced in SQL (``flat_pred`` / ``flat_id_pred`` / ``society_pred``), never by
post-filtering, and out-of-scope object ids look exactly like missing ones (404).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import ColumnElement

from app.models import Flat

ScopeKind = Literal["all", "society", "flat", "none"]


@dataclass(frozen=True)
class Scope:
    kind: ScopeKind
    society_id: uuid.UUID | None = None
    flat_id: uuid.UUID | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def for_user(cls, user: Any) -> Scope:
        role = user.role
        if role == "admin":
            return cls("all")
        if role == "maintenance":
            return cls("society", society_id=user.society_id) if user.society_id else cls("none")
        if role == "resident" and user.flat_id:
            return cls("flat", society_id=user.society_id, flat_id=user.flat_id)
        return cls("none")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scope:
        def _u(v: Any) -> uuid.UUID | None:
            return uuid.UUID(v) if v else None

        return cls(data["kind"], _u(data.get("society_id")), _u(data.get("flat_id")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "society_id": str(self.society_id) if self.society_id else None,
            "flat_id": str(self.flat_id) if self.flat_id else None,
        }

    # ------------------------------------------------------------------ SQL predicates
    @property
    def is_empty(self) -> bool:
        return self.kind == "none"

    @property
    def unrestricted(self) -> bool:
        return self.kind == "all"

    def flat_pred(self) -> ColumnElement[bool]:
        """Predicate over the ``flats`` table."""
        if self.kind == "all":
            return sa.true()
        if self.kind == "society" and self.society_id:
            return Flat.society_id == self.society_id
        if self.kind == "flat" and self.flat_id:
            return Flat.id == self.flat_id
        return sa.false()

    def flat_id_pred(self, col: Any) -> ColumnElement[bool]:
        """Predicate for any column holding a flat id (alerts.flat_id, readings.flat_id, ...)."""
        if self.kind == "all":
            return sa.true()
        if self.kind == "flat" and self.flat_id:
            return col == self.flat_id
        if self.kind == "society" and self.society_id:
            return col.in_(sa.select(Flat.id).where(Flat.society_id == self.society_id))
        return sa.false()

    def society_pred(self, col: Any) -> ColumnElement[bool]:
        """Predicate for a column holding a society id."""
        if self.kind == "all":
            return sa.true()
        if self.kind in ("society", "flat") and self.society_id:
            return col == self.society_id
        return sa.false()

    # ------------------------------------------------------------------ in-memory checks
    def allows_flat(self, flat: Flat) -> bool:
        if self.kind == "all":
            return True
        if self.kind == "society":
            return flat.society_id == self.society_id
        return self.kind == "flat" and flat.id == self.flat_id

    def allows_society(self, society_id: uuid.UUID) -> bool:
        if self.kind == "all":
            return True
        return self.kind in ("society", "flat") and society_id == self.society_id

    def allows_society_stats(self) -> bool:
        """Aggregates over a whole society are staff-only (residents see the summary only)."""
        return self.kind in ("all", "society")

    def matches_keys(self, keys: set[str]) -> bool:
        """Does a WS event keyed by [flat_id, society_id, ...] fall inside this scope?"""
        if self.kind == "all":
            return True
        if self.kind == "society" and self.society_id:
            return str(self.society_id) in keys
        if self.kind == "flat" and self.flat_id:
            return str(self.flat_id) in keys
        return False

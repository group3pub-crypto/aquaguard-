from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User
from app.repositories import FlatRepository, UserRepository
from app.runtime import Runtime
from app.scope import Scope
from app.services.auth import decode_token

bearer = HTTPBearer(auto_error=False)

# Every route must carry exactly the policy markers it needs; tests/test_scope_matrix.py walks
# app.routes and fails for a route without one of ACCEPTED_POLICIES.
ACCEPTED_POLICIES = frozenset(
    {"public", "self", "admin", "scoped", "service_or_admin", "metrics", "ticket"}
)


def get_rt(request: Request) -> Runtime:
    return request.app.state.rt


def get_settings_dep(rt: Annotated[Runtime, Depends(get_rt)]) -> Settings:
    return rt.settings


async def get_session(rt: Annotated[Runtime, Depends(get_rt)]) -> AsyncIterator[AsyncSession]:
    async with rt.db.session() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
RT = Annotated[Runtime, Depends(get_rt)]

_UNAUTH = HTTPException(
    status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"}
)


async def get_current_user(
    rt: RT,
    session: Session,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> User | None:
    """None when no token is sent; 401 when a token is sent but no longer valid.

    The user is re-read on EVERY request: role changes, deactivation, password changes and
    "logout everywhere" (token_version bump) take effect immediately.
    """
    if creds is None:
        return None
    claims = decode_token(rt.settings, creds.credentials)
    if claims is None:
        raise _UNAUTH
    try:
        user = await UserRepository(session).get(uuid.UUID(claims.sub))
    except ValueError:
        raise _UNAUTH from None
    if user is None or not user.is_active or user.token_version != claims.tv:
        raise _UNAUTH
    return user


async def require_user(user: Annotated[User | None, Depends(get_current_user)]) -> User:
    if user is None:
        raise _UNAUTH
    return user


CurrentUser = Annotated[User, Depends(require_user)]


# ---------------------------------------------------------------------------- policy markers
def _policy(kind: str) -> Callable[..., None]:
    def marker() -> None:
        return None

    marker.policy_kind = kind  # type: ignore[attr-defined]
    return marker


public_policy = _policy("public")
PublicRoute = Depends(public_policy)
metrics_policy = _policy("metrics")
ticket_policy = _policy("ticket")


async def self_policy(user: CurrentUser) -> User:
    """The route only ever acts on the caller's own account / session."""
    return user


self_policy.policy_kind = "self"  # type: ignore[attr-defined]
SelfRoute = Depends(self_policy)


class RoleChecker:
    def __init__(self, *roles: str) -> None:
        self.roles = roles
        # admin-only is a complete policy; staff (admin|maintenance) must be combined with scope
        self.policy_kind = "admin" if roles == ("admin",) else "role"

    async def __call__(self, user: CurrentUser) -> User:
        if user.role not in self.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user


def require_role(*roles: str) -> RoleChecker:
    return RoleChecker(*roles)


async def resolve_scope_society(session: AsyncSession, user: User) -> Scope:
    """Scope with the society id resolved from the flat when the user row lacks it."""
    scope = Scope.for_user(user)
    if scope.kind == "flat" and scope.society_id is None and scope.flat_id is not None:
        flat = await FlatRepository(session).get(scope.flat_id)
        if flat is not None:
            return Scope("flat", flat.society_id, flat.id)
    return scope


async def get_scope(user: CurrentUser, session: Session) -> Scope:
    """Data scope of the caller; every data route depends on this (directly or via a helper)."""
    return await resolve_scope_society(session, user)


get_scope.policy_kind = "scoped"  # type: ignore[attr-defined]
CallerScope = Annotated[Scope, Depends(get_scope)]


async def service_or_admin(user: CurrentUser) -> User:
    if user.role != "admin" and not user.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return user


service_or_admin.policy_kind = "service_or_admin"  # type: ignore[attr-defined]


StaffUser = Annotated[User, Depends(require_role("admin", "maintenance"))]
AdminUser = Annotated[User, Depends(require_role("admin"))]
ServiceOrAdmin = Annotated[User, Depends(service_or_admin)]

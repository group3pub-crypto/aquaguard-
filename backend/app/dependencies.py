from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User
from app.repositories import UserRepository
from app.runtime import Runtime
from app.services.auth import decode_token

bearer = HTTPBearer(auto_error=False)


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
    """None when no token is sent; 401 when a token is sent but invalid or the user is gone."""
    if creds is None:
        return None
    claims = decode_token(rt.settings, creds.credentials)
    if claims is None:
        raise _UNAUTH
    try:
        import uuid

        user = await UserRepository(session).get(uuid.UUID(claims.sub))
    except ValueError:
        raise _UNAUTH from None
    if user is None:
        raise _UNAUTH
    return user  # role is read from the DB, so role changes apply immediately


async def require_user(user: Annotated[User | None, Depends(get_current_user)]) -> User:
    if user is None:
        raise _UNAUTH
    return user


def require_role(*roles: str) -> Callable[..., User]:
    async def checker(user: Annotated[User, Depends(require_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user

    return checker  # type: ignore[return-value]


CurrentUser = Annotated[User, Depends(require_user)]
StaffUser = Annotated[User, Depends(require_role("admin", "maintenance"))]
AdminUser = Annotated[User, Depends(require_role("admin"))]

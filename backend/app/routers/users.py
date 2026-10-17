"""Admin user management (admin only) + the bot's username lookup (admin or service account)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status
from sqlalchemy.exc import IntegrityError

from app.dependencies import RT, AdminUser, ServiceOrAdmin, Session
from app.models import User
from app.repositories import AuditRepository, UserRepository
from app.schemas.admin import BindingUpdate, PasswordReset, UserCreate, UserUpdate
from app.schemas.auth import Role, UserOut
from app.schemas.common import Limit, Offset
from app.services import auth as auth_svc
from app.services import users as users_svc

router = APIRouter(prefix="/users", tags=["users"])


async def _user_or_404(session, user_id: uuid.UUID, *, lock: bool = False) -> User:
    repo = UserRepository(session)
    user = await (repo.get_for_update(user_id) if lock else repo.get(user_id))
    if user is None:
        raise HTTPException(404, "User not found")
    return user


def _audit(session, admin: User, user: User, action: str, details: dict | None = None):
    return AuditRepository(session).record(
        entity_type="user",
        entity_id=user.id,
        action=action,
        actor=admin.username,
        details=details,
    )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, admin: AdminUser, rt: RT, session: Session):
    flat_id, society_id = await users_svc.resolve_binding(
        session, body.role, body.flat_id, body.society_id
    )
    try:
        user = await auth_svc.create_user(
            session,
            rt.settings,
            username=body.username,
            password=body.password,
            name=body.name,
            role=body.role,
            flat_id=flat_id,
            society_id=society_id,
            is_service=body.is_service,
        )
    except IntegrityError:
        user = None
    if user is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    await _audit(
        session,
        admin,
        user,
        "user_created",
        {"username": user.username, "role": user.role, "is_service": user.is_service},
    )
    return user


@router.get("", response_model=list[UserOut])
async def list_users(
    admin: AdminUser,
    session: Session,
    role: Role | None = None,
    is_active: bool | None = None,
    society_id: uuid.UUID | None = None,
    flat_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=50)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await UserRepository(session).find(
        role=role,
        is_active=is_active,
        society_id=society_id,
        flat_id=flat_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/by-username/{username}", response_model=UserOut)
async def get_user_by_username(
    username: Annotated[str, Path(min_length=1, max_length=50)], caller: ServiceOrAdmin, session: Session
):
    """For the bot's /link flow: verify that a username exists. Admin or service account only."""
    user = await UserRepository(session).get_by_username(username)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, admin: AdminUser, session: Session):
    return await _user_or_404(session, user_id)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, body: UserUpdate, admin: AdminUser, session: Session):
    user = await _user_or_404(session, user_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    audit: dict = {}
    if changes.get("role") is not None:
        previous = user.role
        cleared = await users_svc.change_role(session, user, changes["role"])
        audit["role"] = {"from": previous, "to": user.role, "binding_cleared": cleared}
    if changes.get("is_active") is not None:
        await users_svc.set_active(session, user, changes["is_active"])
        audit["is_active"] = user.is_active
    if changes.get("name") is not None:
        user.name = changes["name"]
        audit["name"] = user.name
    if changes.get("is_service") is not None:
        user.is_service = changes["is_service"]
        audit["is_service"] = user.is_service
    if "telegram_id" in changes:
        user.telegram_id = changes["telegram_id"]
        audit["telegram_id"] = user.telegram_id
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "telegram_id already linked") from None
    await _audit(session, admin, user, "user_updated", audit)
    return user


@router.put("/{user_id}/binding", response_model=UserOut)
async def set_binding(user_id: uuid.UUID, body: BindingUpdate, admin: AdminUser, session: Session):
    user = await _user_or_404(session, user_id, lock=True)
    user.flat_id, user.society_id = await users_svc.resolve_binding(
        session, user.role, body.flat_id, body.society_id
    )
    await UserRepository(session).flush()
    await _audit(
        session,
        admin,
        user,
        "user_bound",
        {
            "flat_id": str(user.flat_id) if user.flat_id else None,
            "society_id": str(user.society_id) if user.society_id else None,
        },
    )
    return user


@router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: uuid.UUID, body: PasswordReset, admin: AdminUser, rt: RT, session: Session
):
    """Sets a new password, clears any lockout and signs the user out everywhere."""
    user = await _user_or_404(session, user_id, lock=True)
    await auth_svc.set_password(session, rt.settings, user, body.new_password)
    await _audit(session, admin, user, "password_reset")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{user_id}/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(user_id: uuid.UUID, admin: AdminUser, session: Session):
    user = await _user_or_404(session, user_id, lock=True)
    await auth_svc.revoke_all_sessions(session, user)
    await _audit(session, admin, user, "sessions_revoked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

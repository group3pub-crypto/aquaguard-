from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request, status

from app.dependencies import RT, AdminUser, CurrentUser, Session
from app.rate_limit import LOGIN_LIMIT, REGISTER_LIMIT, limiter
from app.repositories import AuditRepository, UserRepository
from app.schemas.auth import LoginRequest, RegisterRequest, RoleUpdate, TokenResponse, UserOut
from app.services import auth as auth_svc

router = APIRouter(prefix="/auth", tags=["auth"])
log = structlog.get_logger(__name__)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(REGISTER_LIMIT)
async def register(request: Request, body: RegisterRequest, rt: RT, session: Session):
    """Public self-registration. The role is always 'resident'."""
    user = await auth_svc.register_user(
        UserRepository(session),
        rt.settings,
        username=body.username,
        password=body.password,
        name=body.name,
    )
    if user is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    await AuditRepository(session).record(
        entity_type="user", entity_id=user.id, action="user_registered", actor=user.username
    )
    return user


@router.post("/login", response_model=TokenResponse)
@limiter.limit(LOGIN_LIMIT)
async def login(request: Request, body: LoginRequest, rt: RT, session: Session):
    user = await auth_svc.authenticate(UserRepository(session), body.username, body.password)
    if user is None:
        log.info("login_failed", username=body.username[:50])
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = auth_svc.create_access_token(rt.settings, user)
    return TokenResponse(access_token=token, role=user.role, expires_in=expires_in)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user


@router.put("/users/{user_id}/role", response_model=UserOut)
async def change_role(user_id: uuid.UUID, body: RoleUpdate, admin: AdminUser, session: Session):
    repo = UserRepository(session)
    target = await repo.get(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    previous = target.role
    await repo.set_role(target, body.role)
    await AuditRepository(session).record(
        entity_type="user",
        entity_id=target.id,
        action="role_changed",
        actor=admin.username,
        details={"from": previous, "to": body.role},
    )
    return target

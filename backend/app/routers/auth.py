from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status

from app.dependencies import (
    RT,
    AdminUser,
    CurrentUser,
    PublicRoute,
    SelfRoute,
    Session,
    resolve_scope_society,
)
from app.rate_limit import (
    LOGIN_IP_LIMIT,
    LOGIN_LIMIT,
    PASSWORD_LIMIT,
    REFRESH_LIMIT,
    REGISTER_LIMIT,
    TICKET_LIMIT,
    limiter,
)
from app.repositories import AuditRepository, UserRepository
from app.repositories.user import normalize_username
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    RoleUpdate,
    TokenResponse,
    UserOut,
    WsTicketResponse,
)
from app.services import auth as auth_svc
from app.services import users as users_svc

router = APIRouter(prefix="/auth", tags=["auth"])
log = structlog.get_logger(__name__)


def _client(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None), request.headers.get("user-agent")


def _tokens(pair: auth_svc.TokenPair, role: str) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        role=role,
        expires_in=pair.expires_in,
        refresh_expires_in=pair.refresh_expires_in,
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[PublicRoute],
)
async def register(request: Request, body: RegisterRequest, rt: RT, session: Session):
    """Self-registration (OFF unless ALLOW_SELF_REGISTRATION=true). Creates an UNBOUND resident
    that sees nothing until an admin binds it to a flat."""
    if not rt.settings.allow_self_registration:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    limiter.check(request, "register", REGISTER_LIMIT)
    user = await auth_svc.create_user(
        session, rt.settings, username=body.username, password=body.password, name=body.name
    )
    if user is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    await AuditRepository(session).record(
        entity_type="user", entity_id=user.id, action="user_registered", actor=user.username
    )
    return user


@router.post("/login", response_model=TokenResponse, dependencies=[PublicRoute])
async def login(request: Request, body: LoginRequest, rt: RT, session: Session):
    username = normalize_username(body.username)
    limiter.check(request, "login", LOGIN_LIMIT, username)  # per IP + username
    limiter.check(request, "login-ip", LOGIN_IP_LIMIT)  # per IP, any username
    ip, ua = _client(request)
    user = await auth_svc.authenticate(
        UserRepository(session), rt.settings, username, body.password
    )
    if user is None:
        await session.commit()  # keep the failure counters / lockout (we raise right after)
        log.info("login_failed", username=username[:50])
        # identical for unknown user / wrong password / inactive / locked
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    pair, _ = await auth_svc.issue_tokens(session, rt.settings, user, ip=ip, user_agent=ua)
    await AuditRepository(session).record(
        entity_type="user", entity_id=user.id, action="login", actor=user.username
    )
    return _tokens(pair, user.role)


@router.post("/refresh", response_model=TokenResponse, dependencies=[PublicRoute])
async def refresh(request: Request, body: RefreshRequest, rt: RT, session: Session):
    """Rotate: the presented refresh token is spent and a new pair returned. Presenting a spent
    token again revokes the whole token family (assumed stolen)."""
    limiter.check(request, "refresh", REFRESH_LIMIT)
    ip, ua = _client(request)
    out = await auth_svc.rotate_refresh_token(
        session, rt.settings, body.refresh_token, ip=ip, user_agent=ua
    )
    if out.kind != "ok" or out.pair is None or out.user is None:
        if out.kind == "reuse":
            await AuditRepository(session).record(
                entity_type="refresh_token",
                entity_id=out.family_id,
                action="refresh_reuse_detected",
                actor="system",
                details={"ip": ip},
            )
            log.warning("refresh_token_reuse", family=str(out.family_id))
        await session.commit()  # persist the family revocation
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _tokens(out.pair, out.user.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[PublicRoute])
async def logout(request: Request, body: RefreshRequest, session: Session):
    """Revoke the presented refresh token's family (this device). Always 204."""
    limiter.check(request, "logout", REFRESH_LIMIT)
    await auth_svc.revoke_refresh_token(session, body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, dependencies=[SelfRoute])
async def logout_all(user: CurrentUser, session: Session):
    """Sign out everywhere: all access tokens (token_version) and refresh tokens die."""
    await auth_svc.revoke_all_sessions(session, user)
    await AuditRepository(session).record(
        entity_type="user", entity_id=user.id, action="logout_all", actor=user.username
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", response_model=TokenResponse, dependencies=[SelfRoute])
async def change_password(
    request: Request, body: ChangePasswordRequest, user: CurrentUser, rt: RT, session: Session
):
    """Needs the current password. Every other session is signed out; fresh tokens returned."""
    limiter.check(request, "change-password", PASSWORD_LIMIT, str(user.id))
    locked = await UserRepository(session).get_for_update(user.id)
    assert locked is not None
    if not await auth_svc.verify_password_async(body.current_password, locked.hashed_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(422, "new password must differ from the current one")
    await auth_svc.set_password(session, rt.settings, locked, body.new_password)
    ip, ua = _client(request)
    pair, _ = await auth_svc.issue_tokens(session, rt.settings, locked, ip=ip, user_agent=ua)
    await AuditRepository(session).record(
        entity_type="user", entity_id=locked.id, action="password_changed", actor=locked.username
    )
    return _tokens(pair, locked.role)


@router.get("/me", response_model=UserOut, dependencies=[SelfRoute])
async def me(user: CurrentUser):
    return user


@router.post("/ws-ticket", response_model=WsTicketResponse, dependencies=[SelfRoute])
async def ws_ticket(request: Request, user: CurrentUser, rt: RT, session: Session):
    """Single-use, 30 s ticket for `/api/v1/ws/live?ticket=...` (keeps JWTs out of URLs)."""
    limiter.check(request, "ws-ticket", TICKET_LIMIT, str(user.id))
    scope = await resolve_scope_society(session, user)
    ticket, ttl = await auth_svc.issue_ws_ticket(session, rt.settings, user, scope)
    return WsTicketResponse(ticket=ticket, expires_in=ttl)


@router.put("/users/{user_id}/role", response_model=UserOut)
async def change_role(user_id: uuid.UUID, body: RoleUpdate, admin: AdminUser, session: Session):
    """Kept for compatibility; prefer PATCH /users/{id}. Clears the binding and revokes the
    user's sessions; refuses to demote the last active admin (409)."""
    target = await UserRepository(session).get_for_update(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    previous = target.role
    await users_svc.change_role(session, target, body.role)
    await AuditRepository(session).record(
        entity_type="user",
        entity_id=target.id,
        action="role_changed",
        actor=admin.username,
        details={"from": previous, "to": body.role},
    )
    return target

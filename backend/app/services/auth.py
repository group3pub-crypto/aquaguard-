"""Credentials, access tokens, rotating refresh tokens, lockout and WebSocket tickets."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import RefreshToken, User, WsTicket
from app.passwords import MAX_PASSWORD_BYTES, validate_password
from app.repositories import RefreshTokenRepository, UserRepository, WsTicketRepository
from app.scope import Scope

ALGORITHM = "HS256"
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


class LastAdminError(Exception):
    """Refusing an action that would leave the system without an active admin."""


@lru_cache(maxsize=8)
def _dummy_hash(rounds: int) -> str:
    return _pwd.hash("aquaguard-dummy-password", rounds=rounds)


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    username: str
    role: str
    exp: datetime
    tv: int = 0


def hash_password(password: str, rounds: int = 12) -> str:
    return _pwd.hash(password, rounds=rounds)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except ValueError:
        return False


async def hash_password_async(password: str, rounds: int = 12) -> str:
    """bcrypt is CPU-bound (~250 ms): never run it on the event loop."""
    return await asyncio.to_thread(hash_password, password, rounds)


async def verify_password_async(password: str, hashed: str) -> bool:
    return await asyncio.to_thread(verify_password, password, hashed)


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    settings: Settings, user: User, now: datetime | None = None
) -> tuple[str, int]:
    now = now or utcnow()
    exp = now + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": int(getattr(user, "token_version", 0) or 0),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, settings.access_token_minutes * 60


def decode_token(settings: Settings, token: str) -> TokenClaims | None:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return TokenClaims(
            sub=str(data["sub"]),
            username=str(data["username"]),
            role=str(data["role"]),
            exp=datetime.fromtimestamp(data["exp"], UTC),
            tv=int(data["tv"]),
        )
    except (JWTError, KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------ login
async def authenticate(
    repo: UserRepository,
    settings: Settings,
    username: str,
    password: str,
    now: datetime | None = None,
) -> User | None:
    """Check credentials; apply lockout bookkeeping. One bcrypt verification in every case.

    Returns the user only when the password is right, the account is active and not locked.
    Unknown user / wrong password / inactive / locked are indistinguishable to the caller. The
    caller must COMMIT afterwards even on failure: the failure counters live on the user row.
    """
    now = now or utcnow()
    user = await repo.get_by_username(username, for_update=True)
    too_long = len(password.encode("utf-8")) > MAX_PASSWORD_BYTES
    hashed = user.hashed_password if user else _dummy_hash(settings.bcrypt_rounds)
    ok = await verify_password_async(password, hashed) and not too_long
    if user is None:
        return None
    locked = user.locked_until is not None and user.locked_until > now
    if ok and user.is_active and not locked:
        user.failed_logins, user.last_failed_login_at, user.locked_until = 0, None, None
        return user
    if not ok and not locked:
        window = timedelta(minutes=settings.login_failure_window_minutes)
        if user.last_failed_login_at is None or now - user.last_failed_login_at > window:
            user.failed_logins = 0
        user.failed_logins += 1
        user.last_failed_login_at = now
        if user.failed_logins >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
    return None


# ------------------------------------------------------------------------------ refresh
def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)  # 384 bits


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int


async def issue_tokens(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> tuple[TokenPair, RefreshToken]:
    now = now or utcnow()
    access, expires_in = create_access_token(settings, user, now)
    raw = new_opaque_token()
    row = await RefreshTokenRepository(session).add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_token(raw),
            expires_at=now + timedelta(days=settings.refresh_token_days),
            created_at=now,
            ip=ip[:64] if ip else None,
            user_agent=user_agent[:300] if user_agent else None,
        )
    )
    pair = TokenPair(access, raw, expires_in, settings.refresh_token_days * 86400)
    return pair, row


RefreshKind = Literal["ok", "invalid", "reuse"]


@dataclass
class RefreshOutcome:
    kind: RefreshKind
    pair: TokenPair | None = None
    user: User | None = None
    family_id: uuid.UUID | None = None


async def rotate_refresh_token(
    session: AsyncSession,
    settings: Settings,
    raw: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> RefreshOutcome:
    """Exchange a refresh token for a new pair. Presenting an already-used token is treated as
    theft: the whole family is revoked. The caller commits before answering 401."""
    now = now or utcnow()
    repo = RefreshTokenRepository(session)
    row = await repo.get_by_hash(hash_token(raw))
    if row is None:
        return RefreshOutcome("invalid")
    if row.revoked_at is not None:
        await repo.revoke_family(row.family_id, now)
        return RefreshOutcome("reuse", family_id=row.family_id)
    if row.expires_at <= now:
        return RefreshOutcome("invalid")
    user = await UserRepository(session).get(row.user_id)
    if user is None or not user.is_active:
        await repo.revoke_family(row.family_id, now)
        return RefreshOutcome("invalid")
    if not await repo.claim(row.id, now):  # lost a race: a concurrent use is a reuse
        await repo.revoke_family(row.family_id, now)
        return RefreshOutcome("reuse", family_id=row.family_id)
    pair, new_row = await issue_tokens(
        session, settings, user, family_id=row.family_id, ip=ip, user_agent=user_agent, now=now
    )
    row.replaced_by = new_row.id
    await session.flush()
    return RefreshOutcome("ok", pair, user, row.family_id)


async def revoke_refresh_token(session: AsyncSession, raw: str, now: datetime | None = None) -> bool:
    """Logout: revoke the presented token's whole family. Unknown tokens are ignored."""
    repo = RefreshTokenRepository(session)
    row = await repo.get_by_hash(hash_token(raw))
    if row is None:
        return False
    await repo.revoke_family(row.family_id, now or utcnow())
    return True


async def revoke_all_sessions(
    session: AsyncSession, user: User, now: datetime | None = None
) -> None:
    """Instant global logout: invalidate access tokens (tv) and every refresh token."""
    await UserRepository(session).bump_token_version(user)
    await RefreshTokenRepository(session).revoke_all_for_user(user.id, now or utcnow())


async def set_password(
    session: AsyncSession, settings: Settings, user: User, new_password: str
) -> None:
    validate_password(new_password, user.username)
    user.hashed_password = await hash_password_async(new_password, settings.bcrypt_rounds)
    user.failed_logins, user.last_failed_login_at, user.locked_until = 0, None, None
    await revoke_all_sessions(session, user)


async def create_user(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
    name: str,
    role: str = "resident",
    flat_id: uuid.UUID | None = None,
    society_id: uuid.UUID | None = None,
    is_service: bool = False,
) -> User | None:
    """Create a user. Returns None when the (case-insensitive) username is taken."""
    repo = UserRepository(session)
    if await repo.get_by_username(username):
        return None
    validate_password(password, username)
    return await repo.create(
        id=uuid.uuid4(),
        username=username,
        name=name,
        role=role,
        flat_id=flat_id,
        society_id=society_id,
        is_service=is_service,
        hashed_password=await hash_password_async(password, settings.bcrypt_rounds),
    )


async def ensure_not_last_admin(session: AsyncSession, user: User) -> None:
    """Call BEFORE demoting / deactivating `user`. Locks the admin rows so two concurrent
    demotions cannot both pass."""
    if user.role != "admin" or not user.is_active:
        return
    admins = await UserRepository(session).active_admin_ids_for_update()
    if admins == [user.id] or not [a for a in admins if a != user.id]:
        raise LastAdminError("cannot demote, deactivate or delete the last active admin")


# ------------------------------------------------------------------------------ ws ticket
async def issue_ws_ticket(
    session: AsyncSession, settings: Settings, user: User, scope: Scope, now: datetime | None = None
) -> tuple[str, int]:
    now = now or utcnow()
    raw = secrets.token_urlsafe(32)  # 256 bits
    await WsTicketRepository(session).add(
        WsTicket(
            token_hash=hash_token(raw),
            user_id=user.id,
            token_version=user.token_version,
            scope=scope.to_dict(),
            expires_at=now + timedelta(seconds=settings.ws_ticket_ttl_s),
            created_at=now,
        )
    )
    return raw, settings.ws_ticket_ttl_s


async def redeem_ws_ticket(
    session: AsyncSession, raw: str, now: datetime | None = None
) -> WsTicket | None:
    if not raw or len(raw) > 200:
        return None
    return await WsTicketRepository(session).consume(hash_token(raw), now or utcnow())


def ticket_scope(ticket: WsTicket) -> Scope:
    data: Any = ticket.scope
    return Scope.from_dict(data)

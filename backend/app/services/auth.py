from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import Settings
from app.models import User
from app.repositories import UserRepository

ALGORITHM = "HS256"
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_DUMMY_HASH = _pwd.hash("aquaguard-dummy-password", rounds=12)


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    username: str
    role: str
    exp: datetime


def hash_password(password: str, rounds: int = 12) -> str:
    return _pwd.hash(password, rounds=rounds)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except ValueError:
        return False


def create_access_token(settings: Settings, user: User, now: datetime | None = None) -> tuple[str, int]:
    now = now or datetime.now(UTC)
    exp = now + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": str(user.id), "username": user.username, "role": user.role,
        "iat": int(now.timestamp()), "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM), settings.access_token_minutes * 60


def decode_token(settings: Settings, token: str) -> TokenClaims | None:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return TokenClaims(
            sub=str(data["sub"]), username=str(data["username"]), role=str(data["role"]),
            exp=datetime.fromtimestamp(data["exp"], UTC),
        )
    except (JWTError, KeyError, TypeError, ValueError):
        return None


async def authenticate(repo: UserRepository, username: str, password: str) -> User | None:
    """Verifies in roughly constant time whether or not the user exists."""
    user = await repo.get_by_username(username)
    ok = verify_password(password, user.hashed_password if user else _DUMMY_HASH)
    return user if (user and ok) else None


async def register_user(
    repo: UserRepository, settings: Settings, *, username: str, password: str, name: str
) -> User | None:
    """Always creates a 'resident'. Returns None if the username is taken."""
    if await repo.get_by_username(username):
        return None
    return await repo.create(
        id=uuid.uuid4(), username=username, name=name, role="resident",
        hashed_password=hash_password(password, settings.bcrypt_rounds),
    )

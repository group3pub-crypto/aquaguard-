"""Password policy (shared by the API schemas, the admin endpoints and the CLI)."""

from __future__ import annotations

PASSWORD_MIN_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates beyond this: refuse instead


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, username: str | None = None) -> str:
    """Raise PasswordPolicyError unless `password` satisfies the policy; return it unchanged."""
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_BYTES} bytes (UTF-8)")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(f"password must be at least {PASSWORD_MIN_LENGTH} characters")
    if not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
        raise PasswordPolicyError("password must contain at least one letter and one digit")
    if username and password.strip().lower() == username.strip().lower():
        raise PasswordPolicyError("password must not equal the username")
    return password

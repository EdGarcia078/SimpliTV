"""Authentication and session security primitives for SimpliTV."""

from __future__ import annotations

import hashlib
import math
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from app.core.config import settings

SESSION_COOKIE_NAME = "simplitv_session"
SESSION_EXPIRE_DAYS = settings.SESSION_EXPIRE_DAYS
SESSION_ABSOLUTE_MAX_DAYS = settings.SESSION_ABSOLUTE_MAX_DAYS
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
BCRYPT_MAX_PASSWORD_BYTES = 72
_SESSION_HASH_PREFIX = "sha256:"

# One process-wide valid bcrypt hash used when a username does not exist. This
# keeps the expensive password verification path comparable without creating a
# fresh hash for every failed request.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"simplitv-dummy-password-not-a-real-account",
    bcrypt.gensalt(rounds=12),
).decode("utf-8")


def validate_new_password(password: str) -> None:
    """Validate a password before hashing it with bcrypt.

    Existing accounts with older/shorter passwords remain usable. The stronger
    policy applies only when a new password is created or set.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"La contraseña no puede superar {MAX_PASSWORD_LENGTH} caracteres."
        )
    if len(password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            "La contraseña es demasiado larga para procesarse de forma segura con bcrypt."
        )


def hash_password(password: str) -> str:
    """Hash a plaintext password securely with bcrypt."""
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError("Password exceeds bcrypt's 72-byte input limit.")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash safely."""
    try:
        password_bytes = plain_password.encode("utf-8")
        if len(password_bytes) > BCRYPT_MAX_PASSWORD_BYTES:
            # Never allow a bcrypt implementation to truncate oversized input.
            return False
        return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False
    except Exception:
        return False


def perform_dummy_password_check(plain_password: str) -> None:
    """Perform one bcrypt comparison for nonexistent users to reduce timing leaks."""
    verify_password(plain_password, _DUMMY_PASSWORD_HASH)


def generate_session_token() -> str:
    """Generate a high-entropy, URL-safe random session token."""
    return secrets.token_urlsafe(48)


def session_token_key(token: str) -> str:
    """Return the non-reversible representation persisted in SQLite."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{_SESSION_HASH_PREFIX}{digest}"


def session_token_matches(stored_value: str, token: str) -> bool:
    """Compare a DB value with a raw token, including legacy plaintext rows."""
    expected = session_token_key(token)
    return secrets.compare_digest(stored_value, expected) or secrets.compare_digest(
        stored_value, token
    )


def as_utc(value: datetime) -> datetime:
    """Normalize SQLite-naive and timezone-aware datetimes to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_session_absolute_expiry(created_at: datetime | None = None) -> datetime:
    """Calculate the hard upper lifetime for a login session."""
    created = as_utc(created_at or datetime.now(timezone.utc))
    return created + timedelta(days=SESSION_ABSOLUTE_MAX_DAYS)


def get_session_expiry(
    days: int = SESSION_EXPIRE_DAYS,
    *,
    now: datetime | None = None,
    absolute_expiry: datetime | None = None,
) -> datetime:
    """Calculate the rolling expiration without crossing the absolute lifetime."""
    current = as_utc(now or datetime.now(timezone.utc))
    expiry = current + timedelta(days=days)
    if absolute_expiry is not None:
        expiry = min(expiry, as_utc(absolute_expiry))
    return expiry


def session_cookie_max_age(expires_at: datetime) -> int:
    """Return a non-negative cookie Max-Age aligned with server-side expiry."""
    seconds = math.ceil((as_utc(expires_at) - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(seconds, SESSION_EXPIRE_DAYS * 86400))

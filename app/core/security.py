import secrets
from datetime import datetime, timezone, timedelta
import bcrypt

SESSION_COOKIE_NAME = "simplitv_session"
SESSION_EXPIRE_DAYS = 7


def hash_password(password: str) -> str:
    """Hash a plaintext password securely with bcrypt."""
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        return False


def generate_session_token() -> str:
    """Generate a high-entropy, URL-safe random session token."""
    return secrets.token_urlsafe(48)


def get_session_expiry(days: int = SESSION_EXPIRE_DAYS) -> datetime:
    """Calculate the expiration datetime for a session."""
    return datetime.now(timezone.utc) + timedelta(days=days)

from datetime import datetime, timezone
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlmodel import Session, select

from app.core.security import (
    SESSION_COOKIE_NAME,
    as_utc,
    get_session_absolute_expiry,
    session_token_key,
)
from app.db.session import get_session
from app.models.user import User, UserSession


def _find_valid_user_session(db: Session, token: str) -> Optional[UserSession]:
    """Find a session by hashed token, transparently migrating legacy plaintext rows."""
    # Generated SimpliTV tokens are ~64 characters. Refuse absurd cookie/Bearer
    # values before hashing/querying them so attacker-controlled headers cannot
    # become oversized database lookup keys. The wider bound preserves any legacy
    # token formats used by older releases.
    if not token or len(token) > 256:
        return None

    now = datetime.now(timezone.utc)
    hashed = session_token_key(token)

    # Prefer the hashed representation. The plaintext fallback exists solely so
    # active sessions from pre-hardening releases survive the update.
    user_session = db.exec(
        select(UserSession).where(UserSession.session_token == hashed)
    ).first()
    legacy = False
    if user_session is None:
        user_session = db.exec(
            select(UserSession).where(UserSession.session_token == token)
        ).first()
        legacy = user_session is not None

    if user_session is None:
        return None

    expires_at = as_utc(user_session.expires_at)
    absolute_expiry = (
        as_utc(user_session.absolute_expires_at)
        if user_session.absolute_expires_at is not None
        else get_session_absolute_expiry(user_session.created_at)
    )
    if expires_at <= now or absolute_expiry <= now:
        db.delete(user_session)
        db.commit()
        return None

    changed = False
    if user_session.absolute_expires_at is None:
        user_session.absolute_expires_at = absolute_expiry
        changed = True
    if legacy:
        user_session.session_token = hashed
        changed = True
    if changed:
        db.add(user_session)
        db.commit()
        db.refresh(user_session)

    return user_session


def get_current_user_optional(
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_session),
) -> Optional[User]:
    """Return the active user for a cookie/Bearer session, or ``None``."""
    token = session_cookie
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()

    if not token:
        return None

    user_session = _find_valid_user_session(db, token)
    if not user_session:
        return None

    user = db.get(User, user_session.user_id)
    if not user or not user.is_active:
        return None

    return user


def get_current_user_unrestricted(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    """Require authentication but allow the mandatory first-password-change flow."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_user(
    user: User = Depends(get_current_user_unrestricted),
) -> User:
    """Require an active account that has completed first-run password setup."""
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debes cambiar la contraseña predeterminada antes de continuar.",
            headers={"X-SimpliTV-Password-Change-Required": "1"},
        )
    return user


def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Require authenticated user with the ``admin`` role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso restringido: Se requieren privilegios de administrador.",
        )
    return user

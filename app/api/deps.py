from datetime import datetime, timezone
from typing import Optional
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from app.core.security import SESSION_COOKIE_NAME
from app.db.session import get_session
from app.models.user import User, UserSession


def get_current_user_optional(
    request: Request,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_session),
) -> Optional[User]:
    """
    Extract session token from cookie or Authorization header and return the user if valid.
    Does not raise 401 if unauthenticated (returns None).
    """
    token = session_cookie
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()

    if not token:
        return None

    now = datetime.now(timezone.utc)

    # Query active session
    stmt = select(UserSession).where(
        UserSession.session_token == token,
        UserSession.expires_at > now,
    )
    user_session = db.exec(stmt).first()
    if not user_session:
        return None

    # Query user
    user = db.get(User, user_session.user_id)
    if not user or not user.is_active:
        return None

    return user


def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    """
    Require authenticated active user. Raises 401 if not authenticated.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida para acceder al canal.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    """
    Require authenticated user with 'admin' role. Raises 403 if not admin.
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso restringido: Se requieren privilegios de administrador.",
        )
    return user

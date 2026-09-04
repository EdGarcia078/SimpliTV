import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import (
    _find_valid_user_session,
    get_current_user,
    get_current_user_unrestricted,
)
from app.core.config import settings
from app.core.request_security import get_client_ip, login_rate_limiter
from app.core.security import (
    SESSION_COOKIE_NAME,
    as_utc,
    generate_session_token,
    get_session_absolute_expiry,
    get_session_expiry,
    hash_password,
    perform_dummy_password_check,
    session_cookie_max_age,
    session_token_key,
    session_token_matches,
    validate_new_password,
    verify_password,
)
from app.db.session import get_session
from app.models.channel import Channel
from app.models.user import LoginRequest, User, UserRead, UserSession
from app.services.access_realtime import build_user_access_fingerprint
from app.services.access import (
    channel_is_sensitive,
    get_group_channel_ids_expanded,
    get_or_create_user_preference,
    get_user_blocked_channel_ids,
    replace_user_blocked_channels,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


class ProfileUpdateRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    username: Optional[str] = Field(default=None, max_length=50)
    new_password: Optional[str] = Field(default=None, max_length=128)


class MandatoryPasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordVerificationRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)


class ViewerPreferencesUpdateRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    blocked_channel_ids: Optional[list[int]] = Field(default=None, max_length=10000)
    sensitive_content_enabled: Optional[bool] = None


def _user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,  # type: ignore[arg-type]
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )



def _request_session_token(
    session_cookie: Optional[str],
    authorization: Optional[str],
) -> Optional[str]:
    if session_cookie:
        return session_cookie
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip() or None
    return None

def _set_session_cookie(response: Response, token: str, expires_at: datetime) -> None:
    """Set a revocable session cookie compatible with HTTP LAN and optional HTTPS."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=session_cookie_max_age(expires_at),
        httponly=True,
        samesite="lax",
        secure=settings.SECURE_COOKIES,
        path="/",
    )


def _delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.SECURE_COOKIES,
    )


def _require_current_password(user: User, password: str) -> None:
    if not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La contraseña actual no es correcta.",
        )


def _validate_password_or_400(password: str) -> None:
    try:
        validate_new_password(password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def _viewer_preferences_payload(session: Session, user: User) -> dict:
    preference = get_or_create_user_preference(session, user)
    granted_ids = get_group_channel_ids_expanded(session, user)
    blocked_ids = get_user_blocked_channel_ids(session, user)

    if not granted_ids:
        channels = []
    else:
        channels = session.exec(
            select(Channel).where(Channel.id.in_(granted_ids)).order_by(Channel.id)
        ).all()

    if not preference.sensitive_content_enabled:
        channels = [channel for channel in channels if not channel_is_sensitive(channel)]

    visible_option_ids = {channel.id for channel in channels}
    return {
        "sensitive_content_enabled": bool(preference.sensitive_content_enabled),
        "blocked_channel_ids": sorted(blocked_ids & visible_option_ids),
        "channels": [
            {
                "id": channel.id,
                "name": channel.name,
                "blocked": channel.id in blocked_ids,
            }
            for channel in channels
        ],
    }


@router.post("/login", response_model=UserRead, summary="User Login")
def login(
    credentials: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> UserRead:
    """Authenticate with generic failures, brute-force controls and a hashed session."""
    username = credentials.username.strip()
    client_ip = get_client_ip(request)
    allowed, retry_after = login_rate_limiter.check(client_ip, username)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos de inicio de sesión. Intenta nuevamente más tarde.",
            headers={"Retry-After": str(retry_after)},
        )

    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        perform_dummy_password_check(credentials.password)
        login_rate_limiter.record_failure(client_ip, username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos.",
        )

    password_ok = verify_password(credentials.password, user.password_hash)
    if not password_ok or not user.is_active:
        login_rate_limiter.record_failure(client_ip, username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos.",
        )

    login_rate_limiter.record_success(client_ip, username)
    now = datetime.now(timezone.utc)
    user.last_login_at = now
    session.add(user)

    token = generate_session_token()
    absolute_expiry = get_session_absolute_expiry(now)
    expiry = get_session_expiry(now=now, absolute_expiry=absolute_expiry)
    user_session = UserSession(
        session_token=session_token_key(token),
        user_id=user.id,  # type: ignore[arg-type]
        created_at=now,
        expires_at=expiry,
        absolute_expires_at=absolute_expiry,
    )
    session.add(user_session)
    session.commit()
    session.refresh(user)

    _set_session_cookie(response, token, expiry)
    return _user_read(user)


@router.post("/logout", summary="User Logout")
def logout(
    response: Response,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    """Invalidate the current browser/Bearer session and remove its cookie."""
    token = _request_session_token(session_cookie, authorization)
    if token:
        active_session = _find_valid_user_session(session, token)
        if active_session:
            session.delete(active_session)
            session.commit()

    _delete_session_cookie(response)
    return {"message": "Sesión cerrada correctamente."}


@router.get("/me", response_model=UserRead, summary="Get Current User")
def get_me(
    response: Response,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    current_user: User = Depends(get_current_user_unrestricted),
    session: Session = Depends(get_session),
) -> UserRead:
    """Return the current user and renew cookie sessions up to their hard limit."""
    if session_cookie:
        active_session = _find_valid_user_session(session, session_cookie)
        if active_session is not None and active_session.user_id == current_user.id:
            absolute_expiry = (
                as_utc(active_session.absolute_expires_at)
                if active_session.absolute_expires_at is not None
                else get_session_absolute_expiry(active_session.created_at)
            )
            active_session.absolute_expires_at = absolute_expiry
            active_session.expires_at = get_session_expiry(
                absolute_expiry=absolute_expiry
            )
            session.add(active_session)
            session.commit()
            _set_session_cookie(response, session_cookie, active_session.expires_at)

    return _user_read(current_user)


@router.post(
    "/change-default-password",
    response_model=UserRead,
    summary="Complete Required First Password Change",
)
def change_default_password(
    payload: MandatoryPasswordChangeRequest,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user_unrestricted),
    session: Session = Depends(get_session),
) -> UserRead:
    """Unlock an automatically seeded administrator by replacing its known password."""
    if not current_user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta cuenta no tiene un cambio obligatorio de contraseña pendiente.",
        )

    _require_current_password(current_user, payload.current_password)
    _validate_password_or_400(payload.new_password)
    if verify_password(payload.new_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña debe ser diferente de la contraseña actual.",
        )

    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    session.add(current_user)

    # Keep only the browser completing the mandatory change. This prevents a
    # copied default-password session from surviving after the password changes.
    current_token = _request_session_token(session_cookie, authorization)
    active_sessions = session.exec(
        select(UserSession).where(UserSession.user_id == current_user.id)
    ).all()
    for active in active_sessions:
        if not current_token or not session_token_matches(active.session_token, current_token):
            session.delete(active)

    session.commit()
    session.refresh(current_user)
    return _user_read(current_user)


@router.get("/access-events", summary="Subscribe to realtime account access changes")
async def access_events(
    request: Request,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Push an SSE event whenever this account's channel authorization changes."""
    bind = session.get_bind()
    user_id = current_user.id
    last_fingerprint = build_user_access_fingerprint(session, current_user)

    async def event_stream():
        nonlocal last_fingerprint
        keep_alive_elapsed = 0.0
        yield "retry: 1000\n\n"
        initial_payload = json.dumps({"revision": last_fingerprint[:16], "initial": True})
        yield f"event: access-update\ndata: {initial_payload}\n\n"

        while True:
            if await request.is_disconnected():
                break

            await asyncio.sleep(0.5)
            keep_alive_elapsed += 0.5

            with Session(bind) as live_session:
                if not session_cookie:
                    yield "event: session-invalid\ndata: {}\n\n"
                    break

                active_session = _find_valid_user_session(live_session, session_cookie)
                live_user = live_session.get(User, user_id)
                if (
                    active_session is None
                    or active_session.user_id != user_id
                    or live_user is None
                    or not live_user.is_active
                    or live_user.must_change_password
                ):
                    yield "event: session-invalid\ndata: {}\n\n"
                    break

                fingerprint = build_user_access_fingerprint(live_session, live_user)

            if fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                payload = json.dumps({"revision": fingerprint[:16]})
                yield f"event: access-update\ndata: {payload}\n\n"
                keep_alive_elapsed = 0.0
            elif keep_alive_elapsed >= 15.0:
                yield ": keep-alive\n\n"
                keep_alive_elapsed = 0.0

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/me", response_model=UserRead, summary="Update Current User Profile")
def update_me(
    payload: ProfileUpdateRequest,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> UserRead:
    """Change username and/or password after verifying the current password."""
    _require_current_password(current_user, payload.current_password)

    changed = False
    if payload.username is not None:
        username = payload.username.strip()
        if len(username) < 3:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El nombre de usuario debe tener al menos 3 caracteres.",
            )
        duplicate = session.exec(
            select(User).where(User.username == username, User.id != current_user.id)
        ).first()
        if duplicate:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ese nombre de usuario ya está en uso.",
            )
        if username != current_user.username:
            current_user.username = username
            changed = True

    if payload.new_password is not None and payload.new_password != "":
        _validate_password_or_400(payload.new_password)
        if verify_password(payload.new_password, current_user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La nueva contraseña debe ser diferente de la contraseña actual.",
            )
        current_user.password_hash = hash_password(payload.new_password)
        changed = True

        current_token = _request_session_token(session_cookie, authorization)
        other_sessions = session.exec(
            select(UserSession).where(UserSession.user_id == current_user.id)
        ).all()
        for active in other_sessions:
            if not current_token or not session_token_matches(active.session_token, current_token):
                session.delete(active)

    if changed:
        session.add(current_user)
        session.commit()
        session.refresh(current_user)

    return _user_read(current_user)


@router.post("/preferences", summary="Unlock Viewer Preferences")
def read_viewer_preferences(
    payload: PasswordVerificationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Return the password-protected viewer channel preferences section."""
    _require_current_password(current_user, payload.current_password)
    return _viewer_preferences_payload(session, current_user)


@router.put("/preferences", summary="Update Viewer Preferences")
def update_viewer_preferences(
    payload: ViewerPreferencesUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Update personal blocked channels and/or sensitive-content mode."""
    _require_current_password(current_user, payload.current_password)

    preference = get_or_create_user_preference(session, current_user)
    if payload.blocked_channel_ids is not None:
        replace_user_blocked_channels(session, current_user, payload.blocked_channel_ids)
    if payload.sensitive_content_enabled is not None:
        preference.sensitive_content_enabled = payload.sensitive_content_enabled
        session.add(preference)

    session.commit()
    return _viewer_preferences_payload(session, current_user)

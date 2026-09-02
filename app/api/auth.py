import asyncio
import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.core.security import (
    SESSION_COOKIE_NAME,
    SESSION_EXPIRE_DAYS,
    generate_session_token,
    get_session_expiry,
    hash_password,
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
    current_password: str = Field(min_length=1)
    username: Optional[str] = Field(default=None, max_length=50)
    new_password: Optional[str] = None


class PasswordVerificationRequest(BaseModel):
    current_password: str = Field(min_length=1)


class ViewerPreferencesUpdateRequest(BaseModel):
    current_password: str = Field(min_length=1)
    blocked_channel_ids: Optional[list[int]] = None
    sensitive_content_enabled: Optional[bool] = None


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the browser cookie for a fresh seven-day session window."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_EXPIRE_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _require_current_password(user: User, password: str) -> None:
    if not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La contraseña actual no es correcta.",
        )


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

    # Sensitive channels are not disclosed anywhere in this section until the
    # user has explicitly enabled the mode. Once enabled, they can be blocked
    # exactly like any other channel granted by the access group.
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
    response: Response,
    session: Session = Depends(get_session),
    ) -> UserRead:
    """
    Authenticate user and set secure session cookie.
    Returns generic error message to prevent username enumeration.
    """
    user = session.exec(select(User).where(User.username == credentials.username.strip())).first()

    if not user or not verify_password(credentials.password, user.password_hash) or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos.",
        )

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    session.add(user)

    # Create session record
    token = generate_session_token()
    expiry = get_session_expiry()
    user_session = UserSession(
        session_token=token,
        user_id=user.id,  # type: ignore
        expires_at=expiry,
    )
    session.add(user_session)
    session.commit()
    session.refresh(user)

    # Set secure HttpOnly session cookie
    _set_session_cookie(response, token)

    return UserRead(
        id=user.id,  # type: ignore
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.post("/logout", summary="User Logout")
def logout(
    response: Response,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    session: Session = Depends(get_session),
):
    """
    Invalidate active session and remove session cookie.
    """
    if session_cookie:
        stmt = select(UserSession).where(UserSession.session_token == session_cookie)
        active_session = session.exec(stmt).first()
        if active_session:
            session.delete(active_session)
            session.commit()

    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
    )

    return {"message": "Sesión cerrada correctamente."}


@router.get("/me", response_model=UserRead, summary="Get Current User")
def get_me(
    response: Response,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> UserRead:
    """Return the current user and renew cookie sessions for another seven days."""
    if session_cookie:
        active_session = session.exec(
            select(UserSession).where(
                UserSession.session_token == session_cookie,
                UserSession.user_id == current_user.id,
                UserSession.expires_at > datetime.now(timezone.utc),
            )
        ).first()
        if active_session is not None:
            active_session.expires_at = get_session_expiry()
            session.add(active_session)
            session.commit()
            _set_session_cookie(response, session_cookie)

    return UserRead(
        id=current_user.id,  # type: ignore
        username=current_user.username,
        role=current_user.role,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        last_login_at=current_user.last_login_at,
    )

@router.get("/access-events", summary="Subscribe to realtime account access changes")
async def access_events(
    request: Request,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Push an SSE event whenever this account's channel authorization changes.

    A fresh database session is used for every check, so updates made by another
    device or by the administrator are observed without waiting for the player's
    normal channel resynchronization interval.  The fingerprint also reads the
    portable ``channel.yaml`` sensitive flag, so those edits propagate as well.
    """
    bind = session.get_bind()
    user_id = current_user.id
    last_fingerprint = build_user_access_fingerprint(session, current_user)

    async def event_stream():
        nonlocal last_fingerprint
        keep_alive_elapsed = 0.0
        # Tell EventSource to retry reasonably quickly after short LAN outages.
        yield "retry: 1000\n\n"
        # Always publish the current revision when a connection (or automatic
        # reconnection) is established. This closes the race where a browser was
        # offline exactly while its permissions changed.
        initial_payload = json.dumps({"revision": last_fingerprint[:16], "initial": True})
        yield f"event: access-update\ndata: {initial_payload}\n\n"

        while True:
            if await request.is_disconnected():
                break

            await asyncio.sleep(0.5)
            keep_alive_elapsed += 0.5

            with Session(bind) as live_session:
                # A password reset/change, logout from this session, account
                # deactivation or deletion must invalidate an already-open SSE
                # stream instead of letting a stale browser linger indefinitely.
                if not session_cookie:
                    yield "event: session-invalid\ndata: {}\n\n"
                    break

                now = datetime.now(timezone.utc)
                active_session = live_session.exec(
                    select(UserSession).where(
                        UserSession.session_token == session_cookie,
                        UserSession.user_id == user_id,
                        UserSession.expires_at > now,
                    )
                ).first()
                live_user = live_session.get(User, user_id)
                if active_session is None or live_user is None or not live_user.is_active:
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
        if len(payload.new_password) < 6:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La nueva contraseña debe tener al menos 6 caracteres.",
            )
        current_user.password_hash = hash_password(payload.new_password)
        changed = True

        # Keep the current browser signed in, but revoke other active sessions
        # because a password change is a security-sensitive account action.
        other_sessions = session.exec(
            select(UserSession).where(UserSession.user_id == current_user.id)
        ).all()
        for active in other_sessions:
            if not session_cookie or active.session_token != session_cookie:
                session.delete(active)

    if changed:
        session.add(current_user)
        session.commit()
        session.refresh(current_user)

    return UserRead(
        id=current_user.id,  # type: ignore[arg-type]
        username=current_user.username,
        role=current_user.role,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        last_login_at=current_user.last_login_at,
    )


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

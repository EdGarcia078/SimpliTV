from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response, status
from sqlmodel import Session

from app.api.deps import _find_valid_user_session, get_current_user
from app.core.security import SESSION_COOKIE_NAME
from app.db.session import get_session
from app.models.channel import Channel
from app.models.media import MediaItem
from app.models.user import User
from app.services.access import require_episode_access, user_can_access_channel
from app.services.streaming import create_media_stream_response, validate_file_safety

router = APIRouter(prefix="/stream", tags=["Streaming"])


def _request_session_token(session_cookie: Optional[str], authorization: Optional[str]) -> Optional[str]:
    if session_cookie:
        return session_cookie
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip() or None
    return None


def _live_stream_access_check(
    bind,
    *,
    token: Optional[str],
    user_id: int,
    channel_id: int,
    media_id: int,
):
    """Build a cheap periodic authorization check for an already-open stream."""
    def check() -> bool:
        if not token:
            return False
        with Session(bind) as live_session:
            active_session = _find_valid_user_session(live_session, token)
            if active_session is None or active_session.user_id != user_id:
                return False
            live_user = live_session.get(User, user_id)
            if (
                live_user is None
                or not live_user.is_active
                or live_user.must_change_password
            ):
                return False
            if live_session.get(Channel, channel_id) is None:
                return False
            media = live_session.get(MediaItem, media_id)
            if media is None or media.channel_id != channel_id:
                return False
            return user_can_access_channel(live_session, live_user, channel_id)

    return check


@router.head("/{episode_id}", summary="Get MediaItem Stream Metadata Headers")
def head_episode_stream(
    episode_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Return media headers only after server-side authorization."""
    episode = session.get(MediaItem, episode_id)
    if not episode:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MediaItem {episode_id} not found."
        )

    require_episode_access(session, user, episode)
    file_path = validate_file_safety(episode.file_path)
    file_size = file_path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Content-Type": episode.mime_type or "video/mp4",
        "Cache-Control": "private, no-store",
    }
    return Response(status_code=status.HTTP_200_OK, headers=headers)


@router.get("/{episode_id}", summary="Stream MediaItem Video")
async def stream_episode(
    episode_id: int,
    range: Optional[str] = Header(None),
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Stream authorized video with Range support and live access revocation."""
    episode = session.get(MediaItem, episode_id)
    if not episode:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MediaItem {episode_id} not found."
        )

    require_episode_access(session, user, episode)
    token = _request_session_token(session_cookie, authorization)
    access_check = _live_stream_access_check(
        session.get_bind(),
        token=token,
        user_id=user.id,  # type: ignore[arg-type]
        channel_id=episode.channel_id,
        media_id=episode.id,  # type: ignore[arg-type]
    )
    return create_media_stream_response(episode, range, access_check=access_check)

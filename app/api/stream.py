from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlmodel import Session
from app.api.deps import get_current_user
from app.db.session import get_session
from app.models.media import MediaItem
from app.models.user import User
from app.services.streaming import create_media_stream_response, validate_file_safety
from app.services.access import require_episode_access

router = APIRouter(prefix="/stream", tags=["Streaming"])


@router.head("/{episode_id}", summary="Get MediaItem Stream Metadata Headers")
def head_episode_stream(
    episode_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Return media metadata headers (Content-Length, Accept-Ranges, Content-Type)
    for HEAD requests from authenticated media clients.
    """
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
    }
    return Response(status_code=status.HTTP_200_OK, headers=headers)


@router.get("/{episode_id}", summary="Stream MediaItem Video")
async def stream_episode(
    episode_id: int,
    range: Optional[str] = Header(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Stream video content for the specified episode ID.
    Supports standard RFC 7233 HTTP Range Requests (206 Partial Content).
    Requires authenticated user session.
    """
    episode = session.get(MediaItem, episode_id)
    if not episode:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MediaItem {episode_id} not found."
        )

    require_episode_access(session, user, episode)

    return create_media_stream_response(episode, range)

import random
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.api.deps import get_current_user
from app.db.session import get_session
from app.models.media import MediaItem, MediaItemRead
from app.models.user import User
from app.services.access import get_player_channel_ids, require_episode_access

router = APIRouter(prefix="/episodes", tags=["MediaItems"])


def to_media_item_read(ep: MediaItem) -> MediaItemRead:
    """Convert an MediaItem DB instance to an MediaItemRead response."""
    return MediaItemRead(
        id=ep.id,  # type: ignore
        channel_id=ep.channel_id,
        media_title=ep.media_title,
        season_number=ep.season_number,
        episode_number=ep.episode_number,
        episode_title=ep.episode_title,
        media_type=ep.media_type,
        franchise=ep.franchise,
        file_size=ep.file_size,
        duration=ep.duration,
        mime_type=ep.mime_type,
        play_count=ep.play_count,
        last_played_at=ep.last_played_at,
        created_at=ep.created_at,
        stream_url=f"/api/stream/{ep.id}",
    )


@router.get("", response_model=List[MediaItemRead], summary="List MediaItems")
def list_episodes(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> List[MediaItemRead]:
    """Retrieve episodes visible to the authenticated user."""
    stmt = select(MediaItem).order_by(MediaItem.id)
    allowed = get_player_channel_ids(session, user)
    if not allowed:
        return []
    stmt = stmt.where(MediaItem.channel_id.in_(allowed))

    episodes = session.exec(stmt.offset(offset).limit(limit)).all()
    return [to_media_item_read(ep) for ep in episodes]


@router.get("/random", response_model=MediaItemRead, summary="Get Random MediaItem")
def get_random_episode(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MediaItemRead:
    """Select a random episode from channels visible to the user."""
    stmt = select(MediaItem)
    allowed = get_player_channel_ids(session, user)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No accessible media found in library.",
        )
    stmt = stmt.where(MediaItem.channel_id.in_(allowed))

    episodes = session.exec(stmt).all()
    if not episodes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No accessible media found in library.",
        )

    return to_media_item_read(random.choice(episodes))


@router.get("/{episode_id}", response_model=MediaItemRead, summary="Get MediaItem by ID")
def get_episode_by_id(
    episode_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MediaItemRead:
    """Retrieve episode metadata if the user can access its channel."""
    ep = session.get(MediaItem, episode_id)
    if not ep:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MediaItem {episode_id} not found.",
        )
    require_episode_access(session, user, ep)
    return to_media_item_read(ep)

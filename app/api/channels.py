import asyncio
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.api.deps import get_current_user
from app.db.session import get_session
from app.models.channel import Channel, ChannelRead, NowPlayingResponse
from app.models.user import User
from app.services.channel import channel_engine
from app.services.access import get_player_channel_ids, require_channel_access
from app.services.media_config import get_channel_dir, load_channel_config

router = APIRouter(prefix="/channels", tags=["Channels"])


def to_channel_read(channel: Channel) -> ChannelRead:
    channel_dir = get_channel_dir(channel.folder_name, channel.name)
    if channel_dir.exists():
        config = load_channel_config(channel_dir)
        return ChannelRead(
            id=channel.id,  # type: ignore[arg-type]
            name=channel.name,
            folder_name=channel.folder_name,
            batch_size=channel.batch_size,
            start_mode=channel.start_mode,
            loop=channel.loop,
            config_source="channel.yaml",
            schedule_default=list(config.schedule.default),
            schedule_slots=len(config.schedule.slots),
            sensitive_content=config.sensitive_content,
        )
    return ChannelRead.model_validate(channel)


@router.get(
    "",
    response_model=List[ChannelRead],
    summary="List all available channels",
)
async def list_channels(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> List[ChannelRead]:
    """Returns a list of all broadcasting channels, ordered by id."""
    stmt = select(Channel).order_by(Channel.id)
    allowed = get_player_channel_ids(session, user)
    if not allowed:
        return []
    stmt = stmt.where(Channel.id.in_(allowed))
    channels = session.exec(stmt).all()
    return [to_channel_read(c) for c in channels]


@router.get(
    "/{channel_id}/now-playing",
    response_model=NowPlayingResponse,
    summary="Get Channel State & Broadcast Position",
)
async def get_now_playing(
    channel_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> NowPlayingResponse:
    """
    Returns the live broadcast state for a specific channel.
    All authenticated viewers on this channel receive the exact same episode and synchronized playback position.
    """
    require_channel_access(session, user, channel_id)
    state = await channel_engine.get_current_state(session, channel_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active broadcast found for this channel.",
        )
    return state

@router.get(
    "/{channel_id}/events",
    summary="Subscribe to live channel changes",
)
async def channel_events(
    channel_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Server-Sent Events stream used to tell viewers that the channel state changed.

    The event intentionally contains only a revision number. Clients then request
    ``now-playing`` so that there remains a single authoritative representation of
    the episode and playback position.
    """
    require_channel_access(session, user, channel_id)

    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Canal no encontrado.",
        )

    queue = channel_engine.subscribe(channel_id)

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    revision = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: channel-update\ndata: {revision}\n\n"
                except asyncio.TimeoutError:
                    # Keep intermediaries from closing an otherwise idle SSE stream.
                    yield ": keep-alive\n\n"
        finally:
            channel_engine.unsubscribe(channel_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


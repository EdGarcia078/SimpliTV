import asyncio
from typing import List, Optional
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from app.api.deps import _find_valid_user_session, get_current_user
from app.core.security import SESSION_COOKIE_NAME
from app.db.session import get_session
from app.models.channel import Channel, ChannelRead, NowPlayingResponse
from app.models.user import User
from app.services.channel import channel_engine
from app.services.access import get_player_channel_ids, require_channel_access, user_can_access_channel
from app.services.media_config import get_channel_dir, load_channel_config
from app.services.scanner import get_library_revision

router = APIRouter(prefix="/channels", tags=["Channels"])


def _request_session_token(session_cookie, authorization):
    if session_cookie:
        return session_cookie
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip() or None
    return None


def _live_user_is_valid(bind, token, user_id, channel_id=None):
    if not token:
        return False
    with Session(bind) as live_session:
        active_session = _find_valid_user_session(live_session, token)
        if active_session is None or active_session.user_id != user_id:
            return False
        live_user = live_session.get(User, user_id)
        if live_user is None or not live_user.is_active or live_user.must_change_password:
            return False
        if channel_id is not None:
            channel = live_session.get(Channel, channel_id)
            if channel is None or not user_can_access_channel(live_session, live_user, channel_id):
                return False
        return True


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
    """Returns a list of all broadcasting channels, ordered by display order."""
    stmt = select(Channel).order_by(Channel.display_order, Channel.id)
    allowed = get_player_channel_ids(session, user)
    if not allowed:
        return []
    stmt = stmt.where(Channel.id.in_(allowed))
    channels = session.exec(stmt).all()
    return [to_channel_read(c) for c in channels]


@router.get("/catalog-events", summary="Subscribe to live library catalog changes")
async def catalog_events(
    request: Request,
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Publish a lightweight revision whenever the filesystem catalog changes."""
    bind = session.get_bind()
    user_id = user.id
    token = _request_session_token(session_cookie, authorization)
    last_revision = get_library_revision(session)

    async def event_stream():
        nonlocal last_revision
        yield "retry: 1000\n\n"
        yield f"event: catalog-update\ndata: {last_revision}\n\n"
        keep_alive = 0.0
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.5)
            keep_alive += 0.5
            if not _live_user_is_valid(bind, token, user_id):
                break
            with Session(bind) as live_session:
                revision = get_library_revision(live_session)
            if revision != last_revision:
                last_revision = revision
                yield f"event: catalog-update\ndata: {revision}\n\n"
                keep_alive = 0.0
            elif keep_alive >= 15.0:
                yield ": keep-alive\n\n"
                keep_alive = 0.0

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


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
    session_cookie: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    authorization: Optional[str] = Header(None),
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
    bind = session.get_bind()
    token = _request_session_token(session_cookie, authorization)
    user_id = user.id

    async def event_stream():
        keep_alive = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    break
                if not _live_user_is_valid(bind, token, user_id, channel_id):
                    break
                try:
                    revision = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"event: channel-update\ndata: {revision}\n\n"
                    keep_alive = 0.0
                except asyncio.TimeoutError:
                    keep_alive += 1.0
                    if keep_alive >= 15.0:
                        yield ": keep-alive\n\n"
                        keep_alive = 0.0
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

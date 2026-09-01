import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from sqlmodel import Session, select

from app.core.config import settings
from app.models.channel import Channel, ChannelState, NowPlayingResponse
from app.models.media import MediaItem
from app.api.media import to_media_item_read
from app.services.selector import is_item_eligible_for_selection, select_next_episode
from app.services.media_config import schedule_allows_item

logger = logging.getLogger(__name__)


def _same_series(left: MediaItem, right: MediaItem) -> bool:
    return (
        getattr(left, "media_type", "episode") == "episode"
        and getattr(right, "media_type", "episode") == "episode"
        and left.media_title == right.media_title
    )


def _media_label(item: MediaItem) -> str:
    if getattr(item, "media_type", "episode") == "movie":
        return f"movie '{item.media_title}'"
    return f"'{item.media_title}' E{item.episode_number}"


class PlaybackState:
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self._lock = asyncio.Lock()
        self._current_episode_id: Optional[int] = None
        self._consecutive_plays: int = 1
        self._started_at: Optional[datetime] = None
        self._duration: float = 0.0
        self._next_episode_id: Optional[int] = None
        self._revision: int = 0


class ChannelEngine:
    """
    Centralized, event-driven/lazy Channel Engine managing multiple channels.
    Maintains the single source of truth for all global broadcast channels.
    """

    def __init__(self):
        self._channels: Dict[int, PlaybackState] = {}
        self._initialized: bool = False
        self._global_lock = asyncio.Lock()
        self._subscribers: Dict[int, set[asyncio.Queue[int]]] = {}

    def reset(self) -> None:
        """Reset in-memory state (useful for test isolation)."""
        self._channels.clear()
        self._subscribers.clear()
        self._initialized = False

    def _get_playback_state(self, channel_id: int) -> PlaybackState:
        if channel_id not in self._channels:
            self._channels[channel_id] = PlaybackState(channel_id)
        return self._channels[channel_id]

    def subscribe(self, channel_id: int) -> asyncio.Queue[int]:
        """Subscribe a client to broadcast changes for one channel."""
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self._subscribers.setdefault(channel_id, set()).add(queue)

        # Emit the current revision immediately. This closes the race between
        # the client's first now-playing request and opening the event stream.
        pb = self._get_playback_state(channel_id)
        queue.put_nowait(pb._revision)
        return queue

    def unsubscribe(self, channel_id: int, queue: asyncio.Queue[int]) -> None:
        """Remove a channel-event subscriber."""
        subscribers = self._subscribers.get(channel_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(channel_id, None)

    def _publish_channel_changed(self, pb: PlaybackState) -> None:
        """Notify all connected viewers that the live broadcast changed."""
        pb._revision += 1
        for queue in tuple(self._subscribers.get(pb.channel_id, ())):
            # We only care about the newest state. If a slow client already has
            # a pending notification, replace it instead of growing a backlog.
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(pb._revision)
            except asyncio.QueueFull:
                pass

    async def initialize(self, session: Session) -> None:
        """
        Initialize or restore all channel states from database on server startup.
        """
        async with self._global_lock:
            if self._initialized:
                return
                
            channels = session.exec(select(Channel)).all()
            now = datetime.now(timezone.utc)

            for channel in channels:
                state = session.get(ChannelState, channel.id)
                pb = self._get_playback_state(channel.id) # type: ignore
                
                async with pb._lock:
                    if state:
                        current_ep = session.get(MediaItem, state.current_episode_id)
                        if current_ep:
                            started_at = state.started_at
                            if started_at.tzinfo is None:
                                started_at = started_at.replace(tzinfo=timezone.utc)

                            elapsed = (now - started_at).total_seconds()
                            duration = max(1.0, current_ep.duration or state.duration)

                            if elapsed < duration:
                                logger.info(
                                    f"Resuming broadcast on channel '{channel.name}': "
                                    f"'{current_ep.media_title}' (elapsed {elapsed:.1f}s / {duration:.1f}s)"
                                )
                                pb._current_episode_id = current_ep.id
                                pb._consecutive_plays = state.consecutive_plays
                                pb._started_at = started_at
                                pb._duration = duration
                                # IMPORTANT: never trust a next_episode_id persisted by an
                                # older selector implementation. Recalculate it on every
                                # backend start using the selector that is currently installed.
                                # This prevents a previously deterministic 1,2,3,4... schedule
                                # from surviving after switching back to random playback.
                                next_ep = select_next_episode(
                                    session,
                                    channel.id,
                                    pb._current_episode_id,
                                    pb._consecutive_plays,
                                    at_time=started_at + timedelta(seconds=duration),
                                )
                                pb._next_episode_id = next_ep.id if next_ep else None
                                state.next_episode_id = pb._next_episode_id
                                state.updated_at = now
                                session.add(state)
                                session.commit()
                                continue
                                
                    # No state or expired -> start fresh
                    await self._start_fresh_broadcast(session, pb, channel, now)

            self._initialized = True

    async def _start_fresh_broadcast(self, session: Session, pb: PlaybackState, channel: Channel, now: datetime) -> None:
        """Pick a new episode and start a fresh broadcast cycle for a channel."""
        first_ep = select_next_episode(session, pb.channel_id)
        if not first_ep:
            logger.warning(f"ChannelEngine: No episodes available in channel '{channel.name}'.")
            pb._current_episode_id = None
            pb._consecutive_plays = 1
            pb._started_at = None
            pb._duration = 0.0
            pb._next_episode_id = None
            return

        pb._current_episode_id = first_ep.id
        pb._consecutive_plays = 1
        pb._started_at = now
        pb._duration = max(1.0, first_ep.duration)

        first_ep.play_count += 1
        first_ep.last_played_at = now
        session.add(first_ep)

        next_ep = select_next_episode(
            session,
            pb.channel_id,
            first_ep.id,
            pb._consecutive_plays,
            at_time=now + timedelta(seconds=pb._duration),
        )
        pb._next_episode_id = next_ep.id if next_ep else None

        state = session.get(ChannelState, pb.channel_id)
        if not state:
            state = ChannelState(
                channel_id=pb.channel_id,
                current_episode_id=first_ep.id, # type: ignore
                consecutive_plays=pb._consecutive_plays,
                next_episode_id=pb._next_episode_id,
                started_at=now,
                duration=pb._duration,
                updated_at=now,
            )
        else:
            state.current_episode_id = first_ep.id # type: ignore
            state.consecutive_plays = pb._consecutive_plays
            state.next_episode_id = pb._next_episode_id
            state.started_at = now
            state.duration = pb._duration
            state.updated_at = now

        session.add(state)
        session.commit()
        self._publish_channel_changed(pb)

        logger.info(
            f"Started fresh broadcast on channel '{channel.name}': "
            f"{_media_label(first_ep)} (Duration: {pb._duration:.1f}s)"
        )

    async def _advance_episode_locked(self, session: Session, pb: PlaybackState, now: datetime) -> None:
        """
        Internal transition to the next episode for a specific channel, protected by its lock.
        """
        channel = session.get(Channel, pb.channel_id)
        if not channel:
            logger.warning(f"ChannelEngine: Cannot advance missing channel ID {pb.channel_id}.")
            return

        # ``_next_episode_id`` is a reservation, not merely a decorative
        # preview. Re-running the random/weighted selector here used to make the
        # UI advertise one valid item and then transmit a different valid item.
        # Keep the advertised reservation whenever it is still part of the
        # effective candidate set at the real transition boundary. Configuration
        # or library changes normally refresh the reservation proactively; this
        # validation is the final safeguard for deleted files, direct database
        # edits, schedule-boundary drift, and missed filesystem events.
        candidate = (
            session.get(MediaItem, pb._next_episode_id)
            if pb._next_episode_id is not None
            else None
        )
        reservation_valid = (
            candidate is not None
            and is_item_eligible_for_selection(
                session,
                channel,
                candidate,
                at_time=now,
            )
        )
        if not reservation_valid:
            if pb._next_episode_id is not None:
                logger.info(
                    "Discarding stale next reservation ID %s for channel '%s'.",
                    pb._next_episode_id,
                    channel.name,
                )
            candidate = select_next_episode(
                session,
                pb.channel_id,
                pb._current_episode_id,
                pb._consecutive_plays,
                at_time=now,
            )

        if not candidate:
            logger.warning(f"ChannelEngine: Cannot advance channel ID {pb.channel_id}, no episodes available.")
            pb._current_episode_id = None
            pb._consecutive_plays = 1
            pb._started_at = None
            pb._duration = 0.0
            pb._next_episode_id = None
            return

        # Check consecutive plays
        current_ep = session.get(MediaItem, pb._current_episode_id) if pb._current_episode_id else None
        if current_ep and _same_series(current_ep, candidate):
            pb._consecutive_plays += 1
        else:
            pb._consecutive_plays = 1

        pb._current_episode_id = candidate.id
        pb._started_at = now
        pb._duration = max(1.0, candidate.duration)

        candidate.play_count += 1
        candidate.last_played_at = now
        session.add(candidate)

        future_ep = select_next_episode(
            session,
            pb.channel_id,
            candidate.id,
            pb._consecutive_plays,
            at_time=now + timedelta(seconds=pb._duration),
        )
        pb._next_episode_id = future_ep.id if future_ep else None

        state = session.get(ChannelState, pb.channel_id)
        if not state:
            state = ChannelState(
                channel_id=pb.channel_id,
                current_episode_id=candidate.id, # type: ignore
                consecutive_plays=pb._consecutive_plays,
                next_episode_id=pb._next_episode_id,
                started_at=now,
                duration=pb._duration,
                updated_at=now,
            )
        else:
            state.current_episode_id = candidate.id # type: ignore
            state.consecutive_plays = pb._consecutive_plays
            state.next_episode_id = pb._next_episode_id
            state.started_at = now
            state.duration = pb._duration
            state.updated_at = now

        session.add(state)
        session.commit()
        self._publish_channel_changed(pb)

        logger.info(
            f"Advanced broadcast on channel '{channel.name}' to: "
            f"{_media_label(candidate)} (Duration: {pb._duration:.1f}s)"
        )

    async def notify_library_changed(self, session: Session) -> None:
        """
        Called when media library files are dynamically added, modified, or removed.
        Checks all channels.
        """
        if not self._initialized:
            await self.initialize(session)
            
        now = datetime.now(timezone.utc)
        channels = session.exec(select(Channel)).all()

        for channel in channels:
            pb = self._get_playback_state(channel.id) # type: ignore
            async with pb._lock:
                if not pb._current_episode_id:
                    await self._start_fresh_broadcast(session, pb, channel, now)
                    continue

                # Always recalculate the next episode. A newly added series or
                # episode can legitimately change the fairest next candidate even
                # when the previously scheduled episode still exists.
                transition_time = (
                    pb._started_at + timedelta(seconds=pb._duration)
                    if pb._started_at
                    else now
                )
                previous_next_id = pb._next_episode_id
                next_ep = select_next_episode(
                    session,
                    pb.channel_id,
                    pb._current_episode_id,
                    pb._consecutive_plays,
                    at_time=transition_time,
                )
                pb._next_episode_id = next_ep.id if next_ep else None

                state = session.get(ChannelState, pb.channel_id)
                if state:
                    state.next_episode_id = pb._next_episode_id
                    state.updated_at = now
                    session.add(state)
                    session.commit()

                if pb._next_episode_id != previous_next_id:
                    # The broadcast itself did not change, but the viewer-facing
                    # "next" preview did. Publish a lightweight revision so
                    # connected players re-fetch canonical state without showing
                    # the OSD or interrupting playback.
                    self._publish_channel_changed(pb)

                if pb._next_episode_id:
                    logger.info(
                        f"Updated next scheduled episode for channel {channel.name} "
                        f"to ID {pb._next_episode_id}"
                    )

    async def refresh_channel_schedule(self, session: Session, channel_id: int) -> None:
        """
        Recalculate the pre-scheduled next episode for one channel.

        This must be called after changing batch_size, start_mode or loop.
        Otherwise PlaybackState._next_episode_id may still contain a candidate
        calculated using the previous configuration.
        """
        if not self._initialized:
            await self.initialize(session)

        channel = session.get(Channel, channel_id)
        if not channel:
            return

        pb = self._get_playback_state(channel_id)
        now = datetime.now(timezone.utc)

        async with pb._lock:
            if not pb._current_episode_id:
                pb._next_episode_id = None
                return

            current_ep = session.get(MediaItem, pb._current_episode_id)
            if not current_ep:
                pb._current_episode_id = None
                pb._next_episode_id = None
                return

            transition_time = (
                pb._started_at + timedelta(seconds=pb._duration)
                if pb._started_at
                else now
            )
            next_ep = select_next_episode(
                session,
                channel_id,
                pb._current_episode_id,
                pb._consecutive_plays,
                at_time=transition_time,
            )
            pb._next_episode_id = next_ep.id if next_ep else None

            state = session.get(ChannelState, channel_id)
            if state:
                state.next_episode_id = pb._next_episode_id
                state.updated_at = now
                session.add(state)
                session.commit()

            # Configuration changes may alter only the cached "next" item while
            # the current broadcast keeps playing. Viewers must still be notified
            # immediately, otherwise their OSD can keep displaying an item that
            # the engine will never actually transmit. syncWithChannel() handles
            # this event silently (showInterface=False), so playback/UI visibility
            # is not disturbed.
            self._publish_channel_changed(pb)

            logger.info(
                f"Refreshed schedule for channel '{channel.name}' after settings change. "
                f"Next episode ID: {pb._next_episode_id}"
            )

    async def get_current_state(self, session: Session, channel_id: int) -> Optional[NowPlayingResponse]:
        """Get the current live channel state lazily."""
        if not self._initialized:
            await self.initialize(session)

        channel = session.get(Channel, channel_id)
        if not channel:
            return None

        pb = self._get_playback_state(channel_id)
        now = datetime.now(timezone.utc)

        if pb._current_episode_id:
            current_ep = session.get(MediaItem, pb._current_episode_id)
            if not current_ep:
                pb._current_episode_id = None

        if not pb._current_episode_id or not pb._started_at:
            async with pb._lock:
                await self._start_fresh_broadcast(session, pb, channel, now)
                if not pb._current_episode_id or not pb._started_at:
                    return None

        started_at = pb._started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        elapsed = (now - started_at).total_seconds()

        if elapsed >= pb._duration:
            async with pb._lock:
                recheck_started = pb._started_at
                if recheck_started and recheck_started.tzinfo is None:
                    recheck_started = recheck_started.replace(tzinfo=timezone.utc)
                if recheck_started:
                    recheck_elapsed = (now - recheck_started).total_seconds()
                    if recheck_elapsed >= pb._duration:
                        await self._advance_episode_locked(session, pb, now)

        current_ep = session.get(MediaItem, pb._current_episode_id)
        if not current_ep:
            return None

        next_ep = session.get(MediaItem, pb._next_episode_id) if pb._next_episode_id else None

        # Defense in depth for stale reservations (for example after an external
        # channel.yaml edit or an older persisted state). The transition consumes
        # this same reservation when it remains valid, so the API must never keep
        # advertising a movie when the next boundary only allows series, or vice
        # versa. We do not re-roll valid random candidates on every request; only
        # a reservation incompatible with the effective schedule is healed.
        transition_time = (
            (pb._started_at + timedelta(seconds=pb._duration))
            if pb._started_at
            else now
        )
        if next_ep is not None:
            strict_preview_valid = schedule_allows_item(channel, next_ep, transition_time)
            fallback_preview_valid = (
                strict_preview_valid
                or is_item_eligible_for_selection(
                    session, channel, next_ep, at_time=transition_time
                )
            )
            if not fallback_preview_valid:
                async with pb._lock:
                    # Re-read inside the lock in case another request already
                    # repaired or advanced the channel while we were waiting.
                    locked_next = (
                        session.get(MediaItem, pb._next_episode_id)
                        if pb._next_episode_id
                        else None
                    )
                    locked_valid = (
                        locked_next is not None
                        and (
                            schedule_allows_item(channel, locked_next, transition_time)
                            or is_item_eligible_for_selection(
                                session, channel, locked_next, at_time=transition_time
                            )
                        )
                    )
                    if locked_next is not None and not locked_valid:
                        repaired = select_next_episode(
                            session,
                            channel_id,
                            pb._current_episode_id,
                            pb._consecutive_plays,
                            at_time=transition_time,
                        )
                        repaired_id = repaired.id if repaired else None
                        if repaired_id != pb._next_episode_id:
                            pb._next_episode_id = repaired_id
                            persisted = session.get(ChannelState, channel_id)
                            if persisted:
                                persisted.next_episode_id = repaired_id
                                persisted.updated_at = now
                                session.add(persisted)
                                session.commit()
                            self._publish_channel_changed(pb)
                        next_ep = repaired
                    else:
                        next_ep = locked_next

        started_at = pb._started_at
        if started_at and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        current_offset = max(0.0, (now - started_at).total_seconds()) if started_at else 0.0
        current_offset = min(current_offset, pb._duration)
        remaining = max(0.0, pb._duration - current_offset)

        return NowPlayingResponse(
            channel_name=channel.name,
            episode=to_media_item_read(current_ep),
            started_at=started_at or now,
            server_time=now,
            current_time=round(current_offset, 2),
            duration=round(pb._duration, 2),
            remaining_time=round(remaining, 2),
            next_episode=to_media_item_read(next_ep) if next_ep else None,
        )

    def get_protected_media_paths(self, session: Session) -> set[Path]:
        """Return current and immediately-next media paths for every channel.

        The optimizer uses this read-only snapshot to avoid touching files that
        ChannelEngine is using or is about to use.
        """
        protected: set[Path] = set()
        for pb in self._channels.values():
            for episode_id in (pb._current_episode_id, pb._next_episode_id):
                if not episode_id:
                    continue
                episode = session.get(MediaItem, episode_id)
                if episode:
                    protected.add((settings.resolved_media_dir / episode.relative_path).resolve())
        return protected

    async def skip_episode(self, session: Session, channel_id: int) -> None:
        """Administratively advance the broadcast to the next episode."""
        if not self._initialized:
            await self.initialize(session)
            
        pb = self._get_playback_state(channel_id)
        now = datetime.now(timezone.utc)
        async with pb._lock:
            await self._advance_episode_locked(session, pb, now)


channel_engine = ChannelEngine()

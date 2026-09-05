from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from app.models.media import MediaItemRead


class Channel(SQLModel, table=True):
    """Broadcasting channel representing a folder of shows."""
    __tablename__ = "channels"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    # Physical folder name under media/. This lets channel.yaml provide a
    # portable display name without losing the stable filesystem location.
    folder_name: Optional[str] = Field(default=None, unique=True, index=True)
    batch_size: int = Field(default=1)
    start_mode: str = Field(default="any")
    loop: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Global presentation order. Independent from the durable channel identity.
    display_order: int = Field(default=0, index=True)


class ChannelIdentityCounter(SQLModel, table=True):
    """Durable allocator preventing a deleted channel ID from being reused."""
    __tablename__ = "channel_identity_counter"

    id: int = Field(default=1, primary_key=True)
    next_id: int = Field(default=1, ge=1)


class ChannelState(SQLModel, table=True):
    """Database model for persisting the global channel state across restarts."""
    __tablename__ = "channel_state"

    channel_id: int = Field(primary_key=True, foreign_key="channels.id")
    current_episode_id: int = Field(foreign_key="media_items.id")
    consecutive_plays: int = Field(default=1)
    next_episode_id: Optional[int] = Field(default=None, foreign_key="media_items.id")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    duration: float = Field(default=0.0)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class NowPlayingResponse(SQLModel):
    """Live broadcast state returned to all viewers."""
    channel_name: str
    episode: MediaItemRead
    started_at: datetime
    server_time: datetime
    current_time: float  # Current playback offset in seconds
    duration: float  # Total episode duration in seconds
    remaining_time: float  # Seconds left before transition
    next_episode: Optional[MediaItemRead] = None

class ChannelRead(SQLModel):
    id: int
    name: str
    folder_name: Optional[str] = None
    batch_size: int
    start_mode: str
    loop: bool
    config_source: str = "legacy"
    schedule_default: list[str] = Field(default_factory=list)
    schedule_slots: int = 0
    sensitive_content: bool = False

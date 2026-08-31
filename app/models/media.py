from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class MediaItemBase(SQLModel):
    """Base media model.

    Rows represent any playable item, including series episodes and movies.
    """
    channel_id: Optional[int] = Field(default=None, foreign_key="channels.id", index=True)
    media_title: str = Field(index=True)
    season_number: int = Field(default=1, index=True)
    episode_number: int = Field(index=True)
    episode_title: Optional[str] = Field(default=None)
    media_type: str = Field(default="episode", index=True)
    franchise: Optional[str] = Field(default=None, index=True)
    relative_path: str = Field(unique=True, index=True)
    file_path: str
    file_size: int = Field(default=0)
    duration: float = Field(default=0.0)  # in seconds
    mime_type: str = Field(default="video/mp4")
    video_codec: Optional[str] = Field(default=None)
    audio_codec: Optional[str] = Field(default=None)
    play_count: int = Field(default=0, index=True)
    last_played_at: Optional[datetime] = Field(default=None)


class MediaItem(MediaItemBase, table=True):
    """MediaItem database table."""
    __tablename__ = "media_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class MediaItemRead(SQLModel):
    """Public media response model."""
    id: int
    channel_id: Optional[int] = None
    media_title: str
    season_number: int
    episode_number: int
    episode_title: Optional[str] = None
    media_type: str = "episode"
    franchise: Optional[str] = None
    file_size: int
    duration: float
    mime_type: str
    play_count: int
    last_played_at: Optional[datetime] = None
    created_at: datetime
    stream_url: str


class ScanResult(SQLModel):
    """Library scan result summary."""
    scanned_count: int
    added_count: int
    updated_count: int
    deleted_count: int
    total_episodes: int
    duration_seconds: float

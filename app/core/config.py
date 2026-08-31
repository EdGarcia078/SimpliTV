import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    APP_NAME: str = "SimpliTV"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Base directory of the project
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent

    # Root of the channel media library (configurable via env MEDIA_DIR)
    MEDIA_DIR: Path = BASE_DIR / "media"

    # Database
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'simplitv.db'}"

    # Secret key for security/sessions
    SECRET_KEY: str = "simplitv-secret-key-change-in-production"

    # Supported video formats
    SUPPORTED_EXTENSIONS: tuple[str, ...] = (
        ".mp4",
        ".mkv",
        ".webm",
        ".avi",
        ".mov",
        ".m4v",
    )

    # Chunk size for video streaming (1MB)
    STREAM_CHUNK_SIZE: int = 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def resolved_media_dir(self) -> Path:
        """Return the media root, migrating the legacy default folder when possible."""
        media_dir = self.MEDIA_DIR.resolve()
        default_media_dir = (self.BASE_DIR / "media").resolve()
        legacy_dir = (self.BASE_DIR / "anime").resolve()

        if media_dir == default_media_dir and not media_dir.exists() and legacy_dir.exists():
            try:
                legacy_dir.rename(media_dir)
            except OSError:
                # Preserve existing installations even when the filesystem cannot rename it.
                return legacy_dir
        return media_dir


def _migrate_legacy_default_database(settings: Settings) -> None:
    """Rename the old default SQLite file without affecting custom DATABASE_URL values."""
    new_db = (settings.BASE_DIR / "simplitv.db").resolve()
    legacy_db = (settings.BASE_DIR / "anime_tv.db").resolve()
    expected_url = f"sqlite:///{new_db}"

    if settings.DATABASE_URL == expected_url and not new_db.exists() and legacy_db.exists():
        try:
            legacy_db.rename(new_db)
        except OSError:
            # Fall back to the legacy file only when an in-place rename is impossible.
            settings.DATABASE_URL = f"sqlite:///{legacy_db}"


settings = Settings()
_migrate_legacy_default_database(settings)

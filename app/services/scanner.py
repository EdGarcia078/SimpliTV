import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from sqlmodel import Session, select

from app.core.config import settings
from app.models.media import MediaItem, ScanResult
from app.models.channel import Channel
from app.services.media_config import (
    ensure_franchise_config,
    ensure_series_config,
    load_channel_config,
)
from app.services.metadata import extract_metadata

logger = logging.getLogger(__name__)

# Regex patterns for season and episode parsing
SEASON_DIR_PATTERN = re.compile(r"(?i)(?:season|temporada|s)[\s._-]*(\d{1,3})")
SXXEXX_PATTERN = re.compile(r"(?i)[sS](\d{1,3})[eE](\d{1,4})")
EP_PATTERN = re.compile(r"(?i)(?:ep(?:isode)?|e)[\s._-]*(\d{1,4})")
BRACKET_EP_PATTERN = re.compile(r"\[(\d{1,4})\]")
NUMERIC_EP_PATTERN = re.compile(r"(?:^|[-_\s]+)(\d{1,4})(?:[-_\s\.]|$)")


@dataclass(frozen=True)
class ParsedMedia:
    channel_folder: str
    channel_name: str
    media_type: str
    title: str
    season_number: int = 0
    episode_number: int = 0
    episode_title: Optional[str] = None
    franchise: Optional[str] = None
    series_dir: Optional[Path] = None
    franchise_dir: Optional[Path] = None


def _parse_episode_stem(filename_stem: str, season_number: int = 1) -> tuple[int, int, Optional[str]]:
    episode_number = 0
    episode_title: Optional[str] = None

    s_match = SXXEXX_PATTERN.search(filename_stem)
    if s_match:
        season_number = int(s_match.group(1))
        episode_number = int(s_match.group(2))
        after = filename_stem[s_match.end():].strip(" -_.")
        if after:
            episode_title = after
    else:
        ep_match = EP_PATTERN.search(filename_stem)
        bracket_match = BRACKET_EP_PATTERN.search(filename_stem)
        num_match = NUMERIC_EP_PATTERN.search(filename_stem)
        if ep_match:
            episode_number = int(ep_match.group(1))
            after = filename_stem[ep_match.end():].strip(" -_.")
            if after:
                episode_title = after
        elif bracket_match:
            episode_number = int(bracket_match.group(1))
            after = filename_stem[bracket_match.end():].strip(" -_.")
            if after:
                episode_title = after
        elif num_match:
            episode_number = int(num_match.group(1))

    if episode_title and episode_title.isdigit():
        episode_title = None

    return season_number, episode_number, episode_title


def parse_media_filename(file_path: Path, root_dir: Path) -> Tuple[str, str, int, int, Optional[str]]:
    """Backwards-compatible parser for the legacy series hierarchy.

    This public helper retains its original five-value return contract because it
    is useful to callers/tests. The scanner itself uses :func:`parse_media_path`,
    which understands the new Series/Movies hierarchy.
    """
    try:
        rel_path = file_path.relative_to(root_dir)
        parts = rel_path.parts
    except ValueError:
        parts = file_path.parts

    filename_stem = file_path.stem
    channel_name = "Default"
    show_name = "Uncategorized"
    season_number = 1

    if len(parts) >= 4:
        channel_name = parts[0]
        show_name = parts[1]
        season_match = SEASON_DIR_PATTERN.search(parts[2])
        if season_match:
            season_number = int(season_match.group(1))
    elif len(parts) == 3:
        channel_name = parts[0]
        show_name = parts[1]
    elif len(parts) == 2:
        show_name = parts[0]
    elif " - " in filename_stem:
        show_name = filename_stem.split(" - ")[0].strip()

    season_number, episode_number, episode_title = _parse_episode_stem(
        filename_stem,
        season_number,
    )
    return channel_name, show_name, season_number, episode_number, episode_title


def _canonical_folder_name(path: Path) -> str:
    return path.name.casefold()


def _prepare_channel_directory(channel_dir: Path) -> None:
    """Ensure portable phase 1-4 scaffolding exists without moving media."""
    load_channel_config(channel_dir)
    existing_dirs = [p for p in channel_dir.iterdir() if p.is_dir()]

    # Reuse case-insensitive user-created folders instead of creating both
    # ``series`` and ``Series`` on case-sensitive filesystems.
    series_root = next(
        (p for p in existing_dirs if _canonical_folder_name(p) == "series"),
        None,
    )
    if series_root is None:
        series_root = channel_dir / "Series"
        series_root.mkdir(exist_ok=True)

    movies_root = next(
        (p for p in existing_dirs if _canonical_folder_name(p) == "movies"),
        None,
    )
    if movies_root is None:
        movies_root = channel_dir / "Movies"
        movies_root.mkdir(exist_ok=True)

    # Canonical Series children are always series, even while empty.
    if series_root.exists():
        for series_dir in series_root.iterdir():
            if series_dir.is_dir():
                ensure_series_config(series_dir)

    # Canonical Movies children are franchises. Loose movie files need no config.
    if movies_root.exists():
        for franchise_dir in movies_root.iterdir():
            if franchise_dir.is_dir():
                ensure_franchise_config(franchise_dir)

    # Legacy series are direct children of the channel. Generate series.yaml so
    # they gain phase-2 behaviour immediately without forcing an on-disk move.
    for child in channel_dir.iterdir():
        if not child.is_dir() or child.name.casefold() in {"series", "movies"}:
            continue
        ensure_series_config(child)


def _resolve_channel_identity(channel_dir: Path) -> tuple[str, str]:
    config = load_channel_config(channel_dir)
    configured_name = config.name.strip() or channel_dir.name
    return channel_dir.name, configured_name


def _get_or_create_channel(session: Session, channel_folder: str, channel_name: str) -> Channel:
    channel = session.exec(
        select(Channel).where(Channel.folder_name == channel_folder)
    ).first()

    if channel is None:
        # Migration compatibility: older DB rows have name but no folder identity.
        channel = session.exec(select(Channel).where(Channel.name == channel_name)).first()
        if channel is None and channel_name != channel_folder:
            channel = session.exec(select(Channel).where(Channel.name == channel_folder)).first()

    if channel is None:
        channel = Channel(name=channel_name, folder_name=channel_folder)
        session.add(channel)
        session.commit()
        session.refresh(channel)
        return channel

    changed = False
    if channel.folder_name != channel_folder:
        channel.folder_name = channel_folder
        changed = True

    if channel.name != channel_name:
        name_conflict = session.exec(
            select(Channel).where(Channel.name == channel_name, Channel.id != channel.id)
        ).first()
        if name_conflict:
            logger.warning(
                "Channel config name '%s' in folder '%s' is already used. Keeping '%s'.",
                channel_name,
                channel_folder,
                channel.name,
            )
        else:
            channel.name = channel_name
            changed = True

    if changed:
        session.add(channel)
        session.commit()
        session.refresh(channel)
    return channel


def parse_media_path(file_path: Path, root_dir: Path) -> Optional[ParsedMedia]:
    """Parse either the canonical or legacy media hierarchy.

    Canonical:
        Channel/Series/Show/Season/MediaItem.ext
        Channel/Movies/Movie.ext
        Channel/Movies/Franchise/Movie.ext

    Legacy (kept for non-destructive migration):
        Channel/Show/Season/MediaItem.ext
    """
    try:
        rel = file_path.relative_to(root_dir)
    except ValueError:
        return None

    parts = rel.parts
    if len(parts) < 2:
        return None

    channel_dir = root_dir / parts[0]
    if not channel_dir.is_dir():
        return None

    channel_folder, channel_name = _resolve_channel_identity(channel_dir)
    category = parts[1].casefold()

    if category == "movies":
        # Channel/Movies/file.ext
        if len(parts) == 3:
            return ParsedMedia(
                channel_folder=channel_folder,
                channel_name=channel_name,
                media_type="movie",
                title=file_path.stem,
            )

        # Channel/Movies/Franchise/file.ext (nested folders below franchise are
        # tolerated and still belong to that first franchise folder).
        if len(parts) >= 4:
            franchise_dir = root_dir / parts[0] / parts[1] / parts[2]
            ensure_franchise_config(franchise_dir)
            franchise_name = load_channel_safe_franchise_name(franchise_dir)
            return ParsedMedia(
                channel_folder=channel_folder,
                channel_name=channel_name,
                media_type="movie",
                title=file_path.stem,
                franchise=franchise_name,
                franchise_dir=franchise_dir,
            )
        return None

    if category == "series":
        if len(parts) < 4:
            return None
        show_name = parts[2]
        series_dir = root_dir / parts[0] / parts[1] / parts[2]
        ensure_series_config(series_dir)
        season_number = 1
        # Season folder is normally parts[3]. If the episode is directly in the
        # series folder, filename parsing still supplies SxxExx when present.
        if len(parts) >= 5:
            season_match = SEASON_DIR_PATTERN.search(parts[3])
            if season_match:
                season_number = int(season_match.group(1))
        season_number, ep_num, ep_title = _parse_episode_stem(file_path.stem, season_number)
        return ParsedMedia(
            channel_folder=channel_folder,
            channel_name=channel_name,
            media_type="episode",
            title=show_name,
            season_number=season_number,
            episode_number=ep_num,
            episode_title=ep_title,
            series_dir=series_dir,
        )

    # Legacy Channel/Show/... hierarchy.
    show_name = parts[1]
    series_dir = root_dir / parts[0] / parts[1]
    ensure_series_config(series_dir)
    _, _, season_number, ep_num, ep_title = parse_media_filename(file_path, root_dir)
    return ParsedMedia(
        channel_folder=channel_folder,
        channel_name=channel_name,
        media_type="episode",
        title=show_name,
        season_number=season_number,
        episode_number=ep_num,
        episode_title=ep_title,
        series_dir=series_dir,
    )


def load_channel_safe_franchise_name(franchise_dir: Path) -> str:
    # Imported lazily to keep scanner imports focused and avoid unnecessary YAML
    # parsing for loose movies.
    from app.services.media_config import load_franchise_config

    config = load_franchise_config(franchise_dir)
    return config.name.strip() or franchise_dir.name


def _valid_media_file(path: Path) -> bool:
    return (
        path.is_file()
        and ".optimizing" not in path.stem
        and ".converting" not in path.stem
        and path.suffix.lower() in settings.SUPPORTED_EXTENSIONS
    )


def _sequential_episode_number(file_path: Path) -> int:
    siblings = sorted(
        f for f in file_path.parent.iterdir()
        if _valid_media_file(f)
    )
    try:
        return siblings.index(file_path) + 1
    except ValueError:
        return 1


async def upsert_episode_file(
    session: Session,
    file_path: Path,
    media_dir: Optional[Path] = None,
) -> Optional[MediaItem]:
    """Idempotently index one episode or movie from the filesystem."""
    target_dir = (media_dir or settings.resolved_media_dir).resolve()
    resolved_path = file_path.resolve()

    if not _valid_media_file(resolved_path):
        return None

    try:
        rel_path = resolved_path.relative_to(target_dir).as_posix()
    except ValueError:
        return None

    parsed = parse_media_path(resolved_path, target_dir)
    if parsed is None:
        logger.warning("Ignoring media outside supported hierarchy: %s", rel_path)
        return None

    channel_dir = target_dir / parsed.channel_folder
    _prepare_channel_directory(channel_dir)
    channel = _get_or_create_channel(session, parsed.channel_folder, parsed.channel_name)

    ep_num = parsed.episode_number
    if parsed.media_type == "episode" and ep_num == 0:
        ep_num = _sequential_episode_number(resolved_path)

    file_stat = resolved_path.stat()
    file_size = file_stat.st_size
    existing = session.exec(
        select(MediaItem).where(MediaItem.relative_path == rel_path)
    ).first()

    if existing is None:
        meta = await extract_metadata(resolved_path)
        item = MediaItem(
            channel_id=channel.id,
            media_title=parsed.title,
            season_number=parsed.season_number,
            episode_number=ep_num,
            episode_title=parsed.episode_title,
            media_type=parsed.media_type,
            franchise=parsed.franchise,
            relative_path=rel_path,
            file_path=str(resolved_path),
            file_size=file_size,
            duration=meta["duration"],
            mime_type=meta["mime_type"],
            video_codec=meta["video_codec"],
            audio_codec=meta["audio_codec"],
            play_count=0,
            last_played_at=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        logger.info("Dynamically indexed %s: %s", parsed.media_type, rel_path)
        return item

    changed = False
    if existing.file_size != file_size:
        meta = await extract_metadata(resolved_path)
        existing.file_size = file_size
        existing.duration = meta["duration"]
        existing.mime_type = meta["mime_type"]
        existing.video_codec = meta["video_codec"]
        existing.audio_codec = meta["audio_codec"]
        changed = True

    desired = {
        "channel_id": channel.id,
        "media_title": parsed.title,
        "season_number": parsed.season_number,
        "episode_number": ep_num,
        "episode_title": parsed.episode_title,
        "media_type": parsed.media_type,
        "franchise": parsed.franchise,
        "file_path": str(resolved_path),
    }
    for field, value in desired.items():
        if getattr(existing, field) != value:
            setattr(existing, field, value)
            changed = True

    if changed:
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        logger.info("Dynamically updated %s: %s", parsed.media_type, rel_path)

    return existing


def remove_episode_by_path(
    session: Session,
    file_path: Path,
    media_dir: Optional[Path] = None,
) -> bool:
    """Remove an indexed media row when its file has been deleted."""
    target_dir = (media_dir or settings.resolved_media_dir).resolve()
    try:
        rel_path = file_path.resolve().relative_to(target_dir).as_posix()
    except Exception:
        rel_path = str(file_path)

    stmt = select(MediaItem).where(
        (MediaItem.relative_path == rel_path) | (MediaItem.file_path == str(file_path.resolve()))
    )
    existing = session.exec(stmt).first()
    if existing:
        session.delete(existing)
        session.commit()
        logger.info("Removed deleted media from index: %s", rel_path)
        return True
    return False


async def scan_library(session: Session, media_dir: Optional[Path] = None) -> ScanResult:
    """Scan the complete portable library and synchronize its SQLite index."""
    start_time = time.time()
    target_dir = (media_dir or settings.resolved_media_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    scanned_count = 0
    added_count = 0
    updated_count = 0
    deleted_count = 0
    found_relative_paths: set[str] = set()

    # Prepare every channel first so config files and canonical folders always
    # exist even when a channel is currently empty.
    for channel_dir in sorted(p for p in target_dir.iterdir() if p.is_dir()):
        _prepare_channel_directory(channel_dir)
        folder, display_name = _resolve_channel_identity(channel_dir)
        _get_or_create_channel(session, folder, display_name)

    media_files: list[Path] = []
    for root, _, files in os.walk(target_dir):
        for file_name in sorted(files):
            file_path = Path(root) / file_name
            if _valid_media_file(file_path):
                media_files.append(file_path)

    for file_path in media_files:
        try:
            rel_path = file_path.relative_to(target_dir).as_posix()
        except ValueError:
            continue

        parsed = parse_media_path(file_path, target_dir)
        if parsed is None:
            logger.warning("Ignoring media outside supported hierarchy: %s", rel_path)
            continue

        scanned_count += 1
        found_relative_paths.add(rel_path)
        file_size = file_path.stat().st_size
        existing = session.exec(
            select(MediaItem).where(MediaItem.relative_path == rel_path)
        ).first()

        channel = _get_or_create_channel(session, parsed.channel_folder, parsed.channel_name)
        ep_num = parsed.episode_number
        if parsed.media_type == "episode" and ep_num == 0:
            ep_num = _sequential_episode_number(file_path)

        if existing is None:
            meta = await extract_metadata(file_path)
            session.add(MediaItem(
                channel_id=channel.id,
                media_title=parsed.title,
                season_number=parsed.season_number,
                episode_number=ep_num,
                episode_title=parsed.episode_title,
                media_type=parsed.media_type,
                franchise=parsed.franchise,
                relative_path=rel_path,
                file_path=str(file_path.resolve()),
                file_size=file_size,
                duration=meta["duration"],
                mime_type=meta["mime_type"],
                video_codec=meta["video_codec"],
                audio_codec=meta["audio_codec"],
                play_count=0,
                last_played_at=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ))
            added_count += 1
            continue

        changed = False
        if existing.file_size != file_size:
            meta = await extract_metadata(file_path)
            existing.file_size = file_size
            existing.duration = meta["duration"]
            existing.mime_type = meta["mime_type"]
            existing.video_codec = meta["video_codec"]
            existing.audio_codec = meta["audio_codec"]
            changed = True

        desired = {
            "channel_id": channel.id,
            "media_title": parsed.title,
            "season_number": parsed.season_number,
            "episode_number": ep_num,
            "episode_title": parsed.episode_title,
            "media_type": parsed.media_type,
            "franchise": parsed.franchise,
            "file_path": str(file_path.resolve()),
        }
        for field, value in desired.items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True

        if changed:
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
            updated_count += 1

    # Delete DB rows for media no longer present. Rows outside this scan root are
    # only relevant in tests that deliberately point the scanner at that root;
    # the historical behaviour is preserved.
    for db_item in session.exec(select(MediaItem)).all():
        if db_item.relative_path not in found_relative_paths:
            session.delete(db_item)
            deleted_count += 1

    session.commit()

    total_items = len(session.exec(select(MediaItem)).all())
    elapsed = round(time.time() - start_time, 3)
    logger.info(
        "Library scan complete in %.3fs: %s scanned, %s added, %s updated, %s deleted. Total: %s",
        elapsed,
        scanned_count,
        added_count,
        updated_count,
        deleted_count,
        total_items,
    )

    return ScanResult(
        scanned_count=scanned_count,
        added_count=added_count,
        updated_count=updated_count,
        deleted_count=deleted_count,
        total_episodes=total_items,
        duration_seconds=elapsed,
    )

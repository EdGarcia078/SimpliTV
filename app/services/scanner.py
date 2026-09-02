import asyncio
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
from app.models.access import GroupChannelAccess
from app.models.media import LibraryRevision, MediaIdentityCounter, MediaItem, ScanResult
from app.models.channel import Channel, ChannelIdentityCounter, ChannelState
from app.models.preferences import UserBlockedChannel
from app.services.media_config import (
    ensure_franchise_config,
    ensure_series_config,
    load_channel_config,
    load_series_config,
    save_channel_config,
)
from app.services.metadata import extract_metadata

logger = logging.getLogger(__name__)
library_sync_lock = asyncio.Lock()

# Regex patterns for season and episode parsing
SEASON_DIR_PATTERN = re.compile(r"(?i)^(?:season|temporada|s)[\s._-]*(\d{1,3})$")
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
    """Prepare the canonical channel scaffold without activating an empty channel."""
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

def _resolve_channel_identity(channel_dir: Path) -> tuple[str, str]:
    config = load_channel_config(channel_dir)
    configured_name = config.name.strip() or channel_dir.name
    return channel_dir.name, configured_name


def _get_or_create_channel(session: Session, channel_folder: str, channel_name: str) -> Channel:
    channel = session.exec(
        select(Channel).where(Channel.folder_name == channel_folder)
    ).first()

    if channel is None:
        name_conflict = next(
            (
                candidate
                for candidate in session.exec(select(Channel)).all()
                if candidate.name.casefold() == channel_name.casefold()
            ),
            None,
        )
        if name_conflict is not None:
            raise ValueError(
                f"El nombre de canal '{channel_name}' definido en channel.yaml "
                f"ya está en uso por la carpeta '{name_conflict.folder_name}'."
            )
        channel = Channel(
            id=_allocate_channel_id(session),
            name=channel_name,
            folder_name=channel_folder,
        )
        session.add(channel)
        session.flush()
        return channel

    changed = False
    if channel.folder_name != channel_folder:
        channel.folder_name = channel_folder
        changed = True

    if channel.name != channel_name:
        name_conflict = next(
            (
                candidate
                for candidate in session.exec(select(Channel)).all()
                if candidate.id != channel.id
                and candidate.name.casefold() == channel_name.casefold()
            ),
            None,
        )
        if name_conflict:
            raise ValueError(
                f"El nombre de canal '{channel_name}' definido en channel.yaml "
                f"ya está en uso por la carpeta '{name_conflict.folder_name}'."
            )
        channel.name = channel_name
        changed = True

    if changed:
        session.add(channel)
        session.flush()
    return channel


def _ensure_channel_identity_counter(session: Session) -> ChannelIdentityCounter:
    counter = session.get(ChannelIdentityCounter, 1)
    highest_existing = max(
        (channel.id or 0 for channel in session.exec(select(Channel)).all()),
        default=0,
    )
    required_next = highest_existing + 1
    if counter is None:
        counter = ChannelIdentityCounter(id=1, next_id=required_next)
        session.add(counter)
        session.flush()
    elif counter.next_id < required_next:
        counter.next_id = required_next
        session.add(counter)
        session.flush()
    return counter


def _allocate_channel_id(session: Session) -> int:
    counter = _ensure_channel_identity_counter(session)
    allocated = counter.next_id
    counter.next_id += 1
    session.add(counter)
    session.flush()
    return allocated


def _ensure_media_identity_counter(session: Session) -> MediaIdentityCounter:
    counter = session.get(MediaIdentityCounter, 1)
    highest_existing = max(
        (item.id or 0 for item in session.exec(select(MediaItem)).all()),
        default=0,
    )
    required_next = highest_existing + 1
    if counter is None:
        counter = MediaIdentityCounter(id=1, next_id=required_next)
        session.add(counter)
        session.flush()
    elif counter.next_id < required_next:
        counter.next_id = required_next
        session.add(counter)
        session.flush()
    return counter


def _allocate_media_id(session: Session) -> int:
    counter = _ensure_media_identity_counter(session)
    allocated = counter.next_id
    counter.next_id += 1
    session.add(counter)
    session.flush()
    return allocated


def parse_media_path(file_path: Path, root_dir: Path) -> Optional[ParsedMedia]:
    """Parse only the canonical SimpliTV media hierarchy.

    Canonical:
        Channel/Series/Show/Season/MediaItem.ext
        Channel/Movies/Movie.ext
        Channel/Movies/Franchise/Movie.ext

    A series may place episodes directly in its directory when it has no
    seasons. Arbitrary nesting and the former Channel/Show hierarchy are not
    accepted.
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

        # Channel/Movies/Franchise/file.ext
        if len(parts) == 4:
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
        if len(parts) not in {4, 5}:
            return None
        series_dir = root_dir / parts[0] / parts[1] / parts[2]
        ensure_series_config(series_dir)
        series_config = load_series_config(series_dir)
        show_name = series_config.name.strip() or series_dir.name

        if len(parts) == 4:
            # The physical hierarchy is authoritative: S01E01 directly in the
            # series directory is still an episode of a seasonless series.
            _, ep_num, ep_title = _parse_episode_stem(file_path.stem, 0)
            season_number = 0
        else:
            season_match = SEASON_DIR_PATTERN.search(parts[3])
            if not season_match:
                return None
            folder_season = int(season_match.group(1))
            _, ep_num, ep_title = _parse_episode_stem(file_path.stem, folder_season)
            # The season directory wins over a conflicting filename.
            season_number = folder_season
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

    return None


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


def bump_library_revision(session: Session) -> int:
    """Increment the durable catalog revision in the caller's transaction."""
    row = session.get(LibraryRevision, 1)
    if row is None:
        row = LibraryRevision(id=1, revision=1)
    else:
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.flush()
    return row.revision


def get_library_revision(session: Session) -> int:
    row = session.get(LibraryRevision, 1)
    return int(row.revision) if row else 0


def _replace_prefix(value: str, old_prefix: str, new_prefix: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized == old_prefix:
        return new_prefix
    marker = f"{old_prefix}/"
    if normalized.startswith(marker):
        return f"{new_prefix}/{normalized[len(marker):]}"
    return normalized


def remap_channel_config_paths(channel_dir: Path, old_prefix: str, new_prefix: str) -> bool:
    """Atomically remap every channel-relative selector reference after a move."""
    config = load_channel_config(channel_dir)
    changed = False

    def remap_list(values: list[str]) -> list[str]:
        nonlocal changed
        result = [_replace_prefix(value, old_prefix, new_prefix) for value in values]
        if result != values:
            changed = True
        return list(dict.fromkeys(result))

    for slot in config.schedule.slots:
        slot.programming.series.items = remap_list(slot.programming.series.items)
        slot.programming.movies.franchises = remap_list(slot.programming.movies.franchises)
        slot.programming.movies.movies = remap_list(slot.programming.movies.movies)

    remapped_weights: dict[str, int] = {}
    for path, weight in config.loose_movie_weights.items():
        remapped = _replace_prefix(path, old_prefix, new_prefix)
        changed = changed or remapped != path
        remapped_weights[remapped] = weight
    config.loose_movie_weights = remapped_weights

    if changed:
        save_channel_config(channel_dir, config)
    return changed


def prune_missing_channel_config_references(channel_dir: Path) -> bool:
    """Remove selector paths only when their physical file/folder is gone.

    Empty series and franchise folders deliberately remain valid dormant
    references, as requested.  A missing target is removed and an emptied
    only/except rule becomes ``all`` so the YAML remains valid.
    """
    config = load_channel_config(channel_dir)
    changed = False

    def valid_reference(value: str, kind: str) -> bool:
        relative = Path(value)
        parts = relative.parts
        if relative.is_absolute() or ".." in parts or len(parts) != 2:
            return False
        if kind == "series":
            expected_category, expected_directory = "series", True
        elif kind == "franchise":
            expected_category, expected_directory = "movies", True
        else:
            expected_category, expected_directory = "movies", False
        if parts[0].casefold() != expected_category:
            return False
        target = channel_dir / relative
        if expected_directory:
            return target.is_dir()
        return _valid_media_file(target)

    def keep_existing(values: list[str], kind: str) -> list[str]:
        nonlocal changed
        kept = [value for value in values if valid_reference(value, kind)]
        if kept != values:
            changed = True
        return kept

    for slot in config.schedule.slots:
        series = slot.programming.series
        series.items = keep_existing(series.items, "series")
        if series.mode in {"only", "except"} and not series.items:
            series.mode = "all"
            changed = True

        movies = slot.programming.movies
        movies.franchises = keep_existing(movies.franchises, "franchise")
        movies.movies = keep_existing(movies.movies, "movie")
        if movies.mode in {"only", "except"} and not movies.franchises and not movies.movies:
            movies.mode = "all"
            changed = True

    weights = {
        path: weight
        for path, weight in config.loose_movie_weights.items()
        if valid_reference(path, "movie")
    }
    if weights != config.loose_movie_weights:
        config.loose_movie_weights = weights
        changed = True

    if changed:
        save_channel_config(channel_dir, config)
    return changed


def _delete_channel_relations(session: Session, channel_id: int) -> None:
    state = session.get(ChannelState, channel_id)
    if state is not None:
        session.delete(state)
    for model in (UserBlockedChannel, GroupChannelAccess):
        rows = session.exec(select(model).where(model.channel_id == channel_id)).all()
        for row in rows:
            session.delete(row)


def _prepare_removed_media(session: Session, items: list[MediaItem]) -> set[int]:
    """Remove persisted playback states that point at rows about to disappear."""
    removed_ids = {item.id for item in items if item.id is not None}
    affected_channels = {item.channel_id for item in items if item.channel_id is not None}
    if not removed_ids:
        return affected_channels
    for state in session.exec(select(ChannelState)).all():
        if state.current_episode_id in removed_ids:
            session.delete(state)
        elif state.next_episode_id in removed_ids:
            state.next_episode_id = None
            state.updated_at = datetime.now(timezone.utc)
            session.add(state)
    return affected_channels


async def move_indexed_path(
    session: Session,
    source: Path,
    destination: Path,
    media_dir: Optional[Path] = None,
) -> bool:
    """Preserve DB identities for an unambiguous move within one channel/type."""
    async with library_sync_lock:
        root = (media_dir or settings.resolved_media_dir).resolve()
        try:
            source_rel = source.absolute().relative_to(root).as_posix()
            destination_rel = destination.absolute().relative_to(root).as_posix()
        except ValueError:
            return False

        source_parts = Path(source_rel).parts
        destination_parts = Path(destination_rel).parts
        if not source_parts or not destination_parts:
            return False

        channel_move = len(source_parts) == 1 and len(destination_parts) == 1
        same_channel_type = (
            source_parts[0] == destination_parts[0]
            and len(source_parts) >= 2
            and len(destination_parts) >= 2
            and source_parts[1].casefold() == destination_parts[1].casefold()
            and source_parts[1].casefold() in {"series", "movies"}
        )
        if not channel_move and not same_channel_type:
            return False

        rows = [
            item for item in session.exec(select(MediaItem)).all()
            if item.relative_path == source_rel or item.relative_path.startswith(f"{source_rel}/")
        ]

        channel: Optional[Channel] = None
        if channel_move:
            channel = session.exec(
                select(Channel).where(Channel.folder_name == source_parts[0])
            ).first()
            if channel is not None:
                conflict = session.exec(
                    select(Channel).where(
                        Channel.folder_name == destination_parts[0],
                        Channel.id != channel.id,
                    )
                ).first()
                if conflict is not None:
                    return False
                channel.folder_name = destination_parts[0]
                session.add(channel)

        if same_channel_type:
            channel = session.exec(
                select(Channel).where(Channel.folder_name == destination_parts[0])
            ).first()

        if not rows and channel is None:
            return False

        moving_ids = {item.id for item in rows}
        planned: list[tuple[MediaItem, Path, str, ParsedMedia]] = []
        for item in rows:
            suffix = item.relative_path[len(source_rel):].lstrip("/")
            new_rel = destination_rel if not suffix else f"{destination_rel}/{suffix}"
            new_path = root / new_rel
            if not new_path.is_file():
                return False
            parsed = parse_media_path(new_path, root)
            if parsed is None:
                return False
            conflict = session.exec(
                select(MediaItem).where(MediaItem.relative_path == new_rel)
            ).first()
            if conflict is not None and conflict.id not in moving_ids:
                return False
            planned.append((item, new_path, new_rel, parsed))

        for item, new_path, new_rel, parsed in planned:
            stat = new_path.stat()
            item.relative_path = new_rel
            item.file_path = str(new_path.resolve())
            item.file_size = stat.st_size
            item.file_mtime_ns = stat.st_mtime_ns
            item.media_title = parsed.title
            item.season_number = parsed.season_number
            item.episode_number = parsed.episode_number or _sequential_episode_number(new_path)
            item.episode_title = parsed.episode_title
            item.media_type = parsed.media_type
            item.franchise = parsed.franchise
            item.updated_at = datetime.now(timezone.utc)
            session.add(item)

        config_remapped = False
        if same_channel_type:
            channel_dir = root / destination_parts[0]
            old_inside = Path(*source_parts[1:]).as_posix()
            new_inside = Path(*destination_parts[1:]).as_posix()
            config_remapped = remap_channel_config_paths(
                channel_dir, old_inside, new_inside
            )

        if not planned and not channel_move and not config_remapped:
            return False

        bump_library_revision(session)
        session.commit()
        logger.info("Preserved indexed identities for move: %s -> %s", source_rel, destination_rel)
        return True


async def _upsert_episode_file_unlocked(
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
    file_mtime_ns = file_stat.st_mtime_ns
    existing = session.exec(
        select(MediaItem).where(MediaItem.relative_path == rel_path)
    ).first()

    if existing is None:
        meta = await extract_metadata(resolved_path)
        item = MediaItem(
            id=_allocate_media_id(session),
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
            file_mtime_ns=file_mtime_ns,
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
        bump_library_revision(session)
        session.commit()
        session.refresh(item)
        logger.info("Dynamically indexed %s: %s", parsed.media_type, rel_path)
        return item

    changed = False
    if existing.file_size != file_size or existing.file_mtime_ns != file_mtime_ns:
        meta = await extract_metadata(resolved_path)
        existing.file_size = file_size
        existing.file_mtime_ns = file_mtime_ns
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
        bump_library_revision(session)
        session.commit()
        session.refresh(existing)
        logger.info("Dynamically updated %s: %s", parsed.media_type, rel_path)

    return existing


async def upsert_episode_file(
    session: Session,
    file_path: Path,
    media_dir: Optional[Path] = None,
) -> Optional[MediaItem]:
    async with library_sync_lock:
        return await _upsert_episode_file_unlocked(session, file_path, media_dir)


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
        channel_id = existing.channel_id
        _prepare_removed_media(session, [existing])
        session.delete(existing)
        session.flush()
        if channel_id is not None:
            remaining = session.exec(
                select(MediaItem.id).where(MediaItem.channel_id == channel_id).limit(1)
            ).first()
            if remaining is None:
                channel = session.get(Channel, channel_id)
                _delete_channel_relations(session, channel_id)
                if channel is not None:
                    session.delete(channel)
        bump_library_revision(session)
        session.commit()
        logger.info("Removed deleted media from index: %s", rel_path)
        return True
    return False


async def remove_episode_by_path_locked(
    session: Session,
    file_path: Path,
    media_dir: Optional[Path] = None,
) -> bool:
    """Serialized watcher-facing variant of :func:`remove_episode_by_path`."""
    async with library_sync_lock:
        try:
            return remove_episode_by_path(session, file_path, media_dir)
        except Exception:
            session.rollback()
            raise


async def _scan_library_unlocked(session: Session, media_dir: Optional[Path] = None) -> ScanResult:
    """Build a complete canonical snapshot and reconcile it atomically."""
    start_time = time.time()
    target_dir = (media_dir or settings.resolved_media_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    scanned_count = 0
    added_count = 0
    updated_count = 0
    deleted_count = 0
    channels_added = 0
    channels_deleted = 0
    found_relative_paths: set[str] = set()
    _ensure_channel_identity_counter(session)
    _ensure_media_identity_counter(session)

    # Every top-level directory is a prepared channel candidate. It receives the
    # portable scaffold immediately but is activated in SQLite only after at
    # least one canonical media file is found.
    for channel_dir in sorted(p for p in target_dir.iterdir() if p.is_dir()):
        _prepare_channel_directory(channel_dir)

    media_files: list[Path] = []
    walk_errors: list[OSError] = []
    for root, _, files in os.walk(target_dir, onerror=walk_errors.append):
        for file_name in sorted(files):
            file_path = Path(root) / file_name
            if _valid_media_file(file_path):
                media_files.append(file_path)

    if walk_errors:
        detail = "; ".join(str(error) for error in walk_errors[:3])
        raise OSError(f"Library scan was incomplete; refusing destructive reconciliation: {detail}")

    parsed_files: list[tuple[Path, str, ParsedMedia]] = []
    active_channel_folders: set[str] = set()
    for file_path in media_files:
        try:
            rel_path = file_path.relative_to(target_dir).as_posix()
        except ValueError:
            continue
        parsed = parse_media_path(file_path, target_dir)
        if parsed is None:
            logger.warning("Ignoring media outside the canonical hierarchy: %s", rel_path)
            continue
        parsed_files.append((file_path, rel_path, parsed))
        active_channel_folders.add(parsed.channel_folder)

    # channel.yaml is authoritative, so two active folders cannot safely claim
    # the same visible name. Abort before deleting or rewriting any DB rows.
    active_names: dict[str, str] = {}
    for _, _, parsed in parsed_files:
        normalized_name = parsed.channel_name.casefold()
        previous_folder = active_names.get(normalized_name)
        if previous_folder is not None and previous_folder != parsed.channel_folder:
            raise ValueError(
                f"Las carpetas '{previous_folder}' y '{parsed.channel_folder}' "
                f"definen el mismo nombre de canal '{parsed.channel_name}'."
            )
        active_names[normalized_name] = parsed.channel_folder

    initial_channel_ids = {channel.id for channel in session.exec(select(Channel)).all()}

    # Remove channels which are no longer active before creating replacements.
    # This also releases unique display names for a newly created channel. A
    # paired live rename is remapped by move_indexed_path before this scan.
    for channel in session.exec(select(Channel)).all():
        folder = channel.folder_name or channel.name
        if folder in active_channel_folders:
            continue
        old_items = session.exec(
            select(MediaItem).where(MediaItem.channel_id == channel.id)
        ).all()
        _prepare_removed_media(session, list(old_items))
        for item in old_items:
            session.delete(item)
            deleted_count += 1
        _delete_channel_relations(session, channel.id)
        session.delete(channel)
        channels_deleted += 1
    session.flush()

    for file_path, rel_path, parsed in parsed_files:
        scanned_count += 1
        found_relative_paths.add(rel_path)
        file_stat = file_path.stat()
        file_size = file_stat.st_size
        file_mtime_ns = file_stat.st_mtime_ns
        existing = session.exec(
            select(MediaItem).where(MediaItem.relative_path == rel_path)
        ).first()

        channel = _get_or_create_channel(session, parsed.channel_folder, parsed.channel_name)
        if channel.id not in initial_channel_ids:
            initial_channel_ids.add(channel.id)
            channels_added += 1
        ep_num = parsed.episode_number
        if parsed.media_type == "episode" and ep_num == 0:
            ep_num = _sequential_episode_number(file_path)

        if existing is None:
            meta = await extract_metadata(file_path)
            session.add(MediaItem(
                id=_allocate_media_id(session),
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
                file_mtime_ns=file_mtime_ns,
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
        if existing.file_size != file_size or existing.file_mtime_ns != file_mtime_ns:
            meta = await extract_metadata(file_path)
            existing.file_size = file_size
            existing.file_mtime_ns = file_mtime_ns
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

    remaining_stale = [
        db_item for db_item in session.exec(select(MediaItem)).all()
        if db_item.relative_path not in found_relative_paths
    ]
    _prepare_removed_media(session, remaining_stale)
    for db_item in remaining_stale:
        if db_item.relative_path not in found_relative_paths:
            session.delete(db_item)
            deleted_count += 1

    session.flush()

    config_pruned = False

    # Defensive second pass for historical orphan channels.
    for channel in session.exec(select(Channel)).all():
        has_media = session.exec(
            select(MediaItem.id).where(MediaItem.channel_id == channel.id).limit(1)
        ).first()
        if has_media is not None:
            channel_dir = target_dir / (channel.folder_name or channel.name)
            if channel_dir.is_dir():
                config_pruned = (
                    prune_missing_channel_config_references(channel_dir)
                    or config_pruned
                )
            continue
        _delete_channel_relations(session, channel.id)
        session.delete(channel)
        channels_deleted += 1

    changed = bool(
        added_count
        or updated_count
        or deleted_count
        or channels_added
        or channels_deleted
        or config_pruned
    )
    if changed:
        bump_library_revision(session)
    session.commit()

    total_items = len(session.exec(select(MediaItem)).all())
    elapsed = round(time.time() - start_time, 3)
    logger.info(
        "Library scan complete in %.3fs: %s scanned, %s added, %s updated, %s deleted, "
        "%s channels added, %s channels deleted. Total: %s",
        elapsed,
        scanned_count,
        added_count,
        updated_count,
        deleted_count,
        channels_added,
        channels_deleted,
        total_items,
    )

    return ScanResult(
        scanned_count=scanned_count,
        added_count=added_count,
        updated_count=updated_count,
        deleted_count=deleted_count,
        total_episodes=total_items,
        duration_seconds=elapsed,
        channels_added=channels_added,
        channels_deleted=channels_deleted,
    )


async def scan_library(session: Session, media_dir: Optional[Path] = None) -> ScanResult:
    """Serialize all complete reconciliations in this application process."""
    async with library_sync_lock:
        try:
            return await _scan_library_unlocked(session, media_dir)
        except Exception:
            session.rollback()
            raise

import logging
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from app.models.channel import Channel
from app.models.media import MediaItem
from app.services.media_config import (
    CHANNEL_CONFIG_FILENAME,
    effective_schedule_for_channel,
    get_channel_dir,
    get_franchise_relative_dir,
    get_loose_movie_relative_path,
    get_series_relative_dir,
    load_franchise_config_for_movie,
    load_series_config_for_episode,
    loose_movie_weight_for_channel,
    schedule_allows_item,
)

logger = logging.getLogger(__name__)


def _weighted_choice(values: list, weights: list[int]):
    if not values:
        return None
    normalized = [max(1, int(weight)) for weight in weights]
    # Preserve the old random.choice path when weights are equal. Apart from being
    # cheaper, this keeps legacy deterministic tests/monkeypatches compatible.
    if len(set(normalized)) <= 1:
        return random.choice(values)
    return random.choices(values, weights=normalized, k=1)[0]


def _random_episode(
    episodes: List[MediaItem],
    *,
    exclude_episode_id: Optional[int] = None,
    start_mode: str = "any",
) -> Optional[MediaItem]:
    if not episodes:
        return None

    candidates = list(episodes)
    if exclude_episode_id is not None:
        alternatives = [ep for ep in candidates if ep.id != exclude_episode_id]
        if alternatives:
            candidates = alternatives

    normalized_mode = start_mode if start_mode in {"any", "even", "odd"} else "any"
    if normalized_mode != "any":
        parity = 0 if normalized_mode == "even" else 1
        parity_candidates = [ep for ep in candidates if ep.episode_number % 2 == parity]
        if parity_candidates:
            candidates = parity_candidates

    return random.choice(candidates) if candidates else None


def _ordered_episodes(episodes: List[MediaItem]) -> List[MediaItem]:
    return sorted(
        episodes,
        key=lambda ep: (ep.season_number, ep.episode_number, ep.id or 0),
    )


def _next_chronological_episode(
    episodes: List[MediaItem],
    current_episode: MediaItem,
    *,
    loop: bool,
) -> Optional[MediaItem]:
    if not episodes:
        return None

    ordered = _ordered_episodes(episodes)
    current_key = (current_episode.season_number, current_episode.episode_number)
    for episode in ordered:
        if (episode.season_number, episode.episode_number) > current_key:
            return episode
    return ordered[0] if loop else None


def _sequential_series_start(
    episodes: List[MediaItem],
    *,
    start_mode: str,
    loop: bool,
) -> Optional[MediaItem]:
    """Continue a series from the episode played most recently.

    Playback progress is runtime/server state (play_count + last_played_at), not
    portable configuration. On a fresh import with no history, sequential mode
    begins from the first chronological episode compatible with start_mode.
    """
    if not episodes:
        return None

    played = [episode for episode in episodes if episode.last_played_at is not None]
    if played:
        latest = max(played, key=lambda episode: episode.last_played_at)
        return _next_chronological_episode(episodes, latest, loop=loop)

    ordered = _ordered_episodes(episodes)
    normalized_mode = start_mode if start_mode in {"any", "even", "odd"} else "any"
    if normalized_mode != "any":
        parity = 0 if normalized_mode == "even" else 1
        matching = [episode for episode in ordered if episode.episode_number % 2 == parity]
        if matching:
            ordered = matching
    return ordered[0] if ordered else None


def _ordered_movies(movies: List[MediaItem]) -> List[MediaItem]:
    # Franchise order is intentionally filesystem/title based. Users can control
    # chronology predictably by naming files with numeric prefixes when needed.
    return sorted(
        movies,
        key=lambda movie: (movie.media_title.casefold(), movie.relative_path.casefold(), movie.id or 0),
    )


def _sequential_franchise_movie(movies: List[MediaItem]) -> Optional[MediaItem]:
    if not movies:
        return None
    ordered = _ordered_movies(movies)
    played = [movie for movie in movies if movie.last_played_at is not None]
    if not played:
        return ordered[0]
    latest = max(played, key=lambda movie: movie.last_played_at)
    for index, movie in enumerate(ordered):
        if movie.id == latest.id:
            return ordered[(index + 1) % len(ordered)]
    return ordered[0]


def _media_kind(item: MediaItem) -> str:
    return "movies" if getattr(item, "media_type", "episode") == "movie" else "series"


def _program_key(item: MediaItem) -> Tuple[str, object]:
    if _media_kind(item) == "movies":
        franchise_rel = get_franchise_relative_dir(item.relative_path, item.media_type)
        if franchise_rel is not None:
            return ("franchise", franchise_rel)
        loose_rel = get_loose_movie_relative_path(item.relative_path, item.media_type)
        return ("movie", loose_rel or item.id or item.relative_path)
    return ("series", get_series_relative_dir(item.relative_path, item.media_type) or item.media_title)


def _effective_loop(channel: Channel) -> bool:
    channel_dir = get_channel_dir(getattr(channel, "folder_name", None), channel.name)
    if (channel_dir / CHANNEL_CONFIG_FILENAME).exists():
        return True
    return bool(channel.loop)


def _eligible_items(
    session: Session,
    channel: Channel,
    *,
    at_time: Optional[datetime] = None,
) -> list[MediaItem]:
    items = list(
        session.exec(select(MediaItem).where(MediaItem.channel_id == channel.id)).all()
    )
    if not items:
        return []

    if not _effective_loop(channel):
        items = [item for item in items if item.play_count == 0]

    scheduled = [
        item for item in items
        if schedule_allows_item(channel, item, at_time)
    ]
    if scheduled:
        return scheduled

    # Keep the channel on-air if an explicit only/except rule references content
    # that was removed: first relax only that selection while preserving type.
    typed_fallback = [
        item for item in items
        if schedule_allows_item(channel, item, at_time, ignore_filters=True)
    ]
    if typed_fallback:
        logger.warning(
            "Channel '%s' schedule selection matches no indexed media; falling back "
            "to the allowed content type(s).",
            channel.name,
        )
        return typed_fallback

    if items:
        logger.warning(
            "Channel '%s' schedule has no media of its allowed type(s); falling "
            "back to any available content.",
            channel.name,
        )
    return items


def is_item_eligible_for_selection(
    session: Session,
    channel: Channel,
    item: MediaItem,
    *,
    at_time: Optional[datetime] = None,
) -> bool:
    """Return whether *item* belongs to the selector's effective candidate set.

    Unlike ``schedule_allows_item`` this includes the channel's safe fallback
    behaviour when configured filters point to media that no longer exists.
    """
    if item.channel_id != channel.id:
        return False
    return any(candidate.id == item.id for candidate in _eligible_items(session, channel, at_time=at_time))


def _select_series_start(
    episodes: list[MediaItem],
    channel: Channel,
    *,
    current_item: Optional[MediaItem],
) -> Optional[MediaItem]:
    if not episodes:
        return None
    config = load_series_config_for_episode(episodes[0], fallback_channel=channel)
    if config.playback.mode == "sequential":
        return _sequential_series_start(
            episodes,
            start_mode=config.start_episode.mode,
            loop=_effective_loop(channel),
        )

    exclude_id = None
    if current_item is not None and _program_key(current_item) == _program_key(episodes[0]):
        exclude_id = current_item.id
    return _random_episode(
        episodes,
        exclude_episode_id=exclude_id,
        start_mode=config.start_episode.mode,
    )


def _select_franchise_movie(
    movies: list[MediaItem],
    *,
    current_item: Optional[MediaItem],
) -> Optional[MediaItem]:
    if not movies:
        return None
    config = load_franchise_config_for_movie(movies[0])
    if config is not None and config.playback.mode == "sequential":
        return _sequential_franchise_movie(movies)

    candidates = list(movies)
    if current_item is not None and _program_key(current_item) == _program_key(movies[0]):
        alternatives = [movie for movie in candidates if movie.id != current_item.id]
        if alternatives:
            candidates = alternatives
    return random.choice(candidates) if candidates else None


def _select_new_program(
    items: list[MediaItem],
    channel: Channel,
    *,
    current_item: Optional[MediaItem],
    at_time: Optional[datetime] = None,
) -> Optional[MediaItem]:
    """Select a weighted series, franchise, or loose movie program."""
    if not items:
        return None

    series: Dict[str, List[MediaItem]] = {}
    franchises: Dict[str, List[MediaItem]] = {}
    loose_movies: Dict[str, MediaItem] = {}

    for item in items:
        if _media_kind(item) == "series":
            key = get_series_relative_dir(item.relative_path, item.media_type) or item.media_title
            series.setdefault(key, []).append(item)
            continue
        franchise_key = get_franchise_relative_dir(item.relative_path, item.media_type)
        if franchise_key is not None:
            franchises.setdefault(franchise_key, []).append(item)
        else:
            loose_key = get_loose_movie_relative_path(item.relative_path, item.media_type)
            loose_movies[loose_key or str(item.id or item.relative_path)] = item

    programs: list[tuple[str, str]] = []
    program_weights: dict[tuple[str, str], int] = {}
    program_total_plays: dict[tuple[str, str], int] = {}

    for key, episodes in series.items():
        program = ("series", key)
        programs.append(program)
        config = load_series_config_for_episode(episodes[0], fallback_channel=channel)
        program_weights[program] = config.selection_weight
        program_total_plays[program] = sum(max(0, int(ep.play_count or 0)) for ep in episodes)

    for key, movies in franchises.items():
        program = ("franchise", key)
        programs.append(program)
        config = load_franchise_config_for_movie(movies[0])
        program_weights[program] = config.selection_weight if config is not None else 1
        program_total_plays[program] = sum(max(0, int(movie.play_count or 0)) for movie in movies)

    for key, movie in loose_movies.items():
        program = ("movie", key)
        programs.append(program)
        program_weights[program] = loose_movie_weight_for_channel(channel, movie)
        program_total_plays[program] = max(0, int(movie.play_count or 0))

    if not programs:
        return None

    current_key = _program_key(current_item) if current_item is not None else None
    alternatives = [program for program in programs if program != current_key]
    if alternatives:
        programs = alternatives

    # Preserve the existing dynamic-library behaviour: a program that has never
    # aired gets one first opportunity before already-seen programs compete again.
    # When several programs are still unseen, the configured weights decide among
    # them normally. This makes newly imported content discoverable immediately
    # without turning play_count into a permanent override of user weights.
    unseen_programs = [program for program in programs if program_total_plays.get(program, 0) == 0]
    if unseen_programs:
        programs = unseen_programs

    # Type weights are applied first; per-series/franchise/movie weights are
    # applied only among programs of the selected type.
    rule = effective_schedule_for_channel(channel, at_time)
    by_type = {
        "series": [program for program in programs if program[0] == "series"],
        "movies": [program for program in programs if program[0] in {"franchise", "movie"}],
    }
    available_types = [kind for kind, values in by_type.items() if values]
    if not available_types:
        return None
    type_weights = [
        rule.series_weight if kind == "series" else rule.movies_weight
        for kind in available_types
    ]
    selected_type = _weighted_choice(available_types, type_weights)
    candidates = by_type[selected_type]
    selected_program = _weighted_choice(
        candidates,
        [program_weights[program] for program in candidates],
    )
    if selected_program is None:
        return None

    selected_kind, selected_key = selected_program
    if selected_kind == "series":
        return _select_series_start(
            series.get(selected_key, []),
            channel,
            current_item=current_item,
        )
    if selected_kind == "franchise":
        return _select_franchise_movie(
            franchises.get(selected_key, []),
            current_item=current_item,
        )
    return loose_movies.get(selected_key)


def select_next_episode(
    session: Session,
    channel_id: int,
    current_episode_id: Optional[int] = None,
    consecutive_plays: int = 0,
    *,
    at_time: Optional[datetime] = None,
) -> Optional[MediaItem]:
    """Select the next media item using the portable channel/series/movie rules.

    Implemented advanced rules:
      * random or sequential series playback;
      * random or sequential franchise playback;
      * weekday-aware schedule slots, including cross-midnight windows;
      * per-slot mutually-exclusive off/all/only/except programming rules;
      * per-slot franchise and loose-movie selection;
      * series-vs-movies type weights;
      * per-series, franchise and loose-movie program weights.
    """
    channel = session.get(Channel, channel_id)
    if not channel:
        return None

    items = _eligible_items(session, channel, at_time=at_time)
    if not items:
        return None

    current_item = (
        session.get(MediaItem, current_episode_id)
        if current_episode_id is not None
        else None
    )
    if current_item is not None and current_item.channel_id != channel_id:
        current_item = None

    # A series block continues chronologically regardless of random/sequential
    # start mode, but only while that same series remains valid for the next slot.
    if current_item is not None and _media_kind(current_item) == "series":
        if schedule_allows_item(channel, current_item, at_time):
            same_show = [
                item for item in items
                if _media_kind(item) == "series"
                and _program_key(item) == _program_key(current_item)
            ]
            series_config = load_series_config_for_episode(
                current_item,
                fallback_channel=channel,
            )
            batch_size = max(1, series_config.episodes_per_airing)
            if 0 < consecutive_plays < batch_size:
                chronological = _next_chronological_episode(
                    same_show,
                    current_item,
                    loop=_effective_loop(channel),
                )
                if chronological is not None:
                    return chronological

    return _select_new_program(
        items,
        channel,
        current_item=current_item,
        at_time=at_time,
    )

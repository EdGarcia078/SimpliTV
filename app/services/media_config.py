"""Portable YAML configuration for channels, series and movie franchises.

The filesystem is the source of truth for behaviour. Missing configuration files
are created automatically with safe defaults, while malformed files never stop a
library scan: they are logged and default values are used for that read.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.core.config import settings

logger = logging.getLogger(__name__)

CONFIG_VERSION = 1
CHANNEL_CONFIG_FILENAME = "channel.yaml"
SERIES_CONFIG_FILENAME = "series.yaml"
FRANCHISE_CONFIG_FILENAME = "franchise.yaml"

ContentType = Literal["series", "movies"]
StartMode = Literal["any", "even", "odd"]
PlaybackMode = Literal["random", "sequential"]
Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]
ALL_WEEKDAYS: tuple[str, ...] = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
)


def _unique_strings(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip().replace("\\", "/")
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned


class StartMediaItemConfig(BaseModel):
    mode: StartMode = "any"


class PlaybackConfig(BaseModel):
    mode: PlaybackMode = "random"


class SeriesConfig(BaseModel):
    version: int = CONFIG_VERSION
    name: str = ""
    episodes_per_airing: int = Field(default=1, ge=1, le=100)
    start_episode: StartMediaItemConfig = Field(default_factory=StartMediaItemConfig)
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)
    # Relative weight used only when this series competes with other series.
    selection_weight: int = Field(default=1, ge=1, le=1000)


class FranchiseConfig(BaseModel):
    version: int = CONFIG_VERSION
    name: str
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)
    # Relative weight used when this franchise competes with other movie programs.
    selection_weight: int = Field(default=1, ge=1, le=1000)


class ContentWeights(BaseModel):
    """Relative type weights; values do not need to sum to 100."""

    series: int = Field(default=1, ge=1, le=1000)
    movies: int = Field(default=1, ge=1, le=1000)


SelectionMode = Literal["off", "all", "only", "except"]


class SeriesProgramming(BaseModel):
    """One mutually-exclusive rule controlling series eligibility in a slot."""

    mode: SelectionMode = "all"
    items: list[str] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        return _unique_strings(value)

    @model_validator(mode="after")
    def validate_mode_items(self):
        if self.mode in {"off", "all"}:
            self.items = []
        elif not self.items:
            raise ValueError(f"series mode '{self.mode}' requires at least one selected item")
        return self


class MoviesProgramming(BaseModel):
    """One mutually-exclusive rule controlling movie eligibility in a slot."""

    mode: SelectionMode = "all"
    franchises: list[str] = Field(default_factory=list)
    movies: list[str] = Field(default_factory=list)

    @field_validator("franchises", "movies")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        return _unique_strings(value)

    @model_validator(mode="after")
    def validate_mode_items(self):
        if self.mode in {"off", "all"}:
            self.franchises = []
            self.movies = []
        elif not self.franchises and not self.movies:
            raise ValueError(f"movies mode '{self.mode}' requires at least one selected item")
        return self


class SlotProgramming(BaseModel):
    series: SeriesProgramming = Field(default_factory=SeriesProgramming)
    movies: MoviesProgramming = Field(default_factory=MoviesProgramming)

    @model_validator(mode="after")
    def validate_something_enabled(self):
        if self.series.mode == "off" and self.movies.mode == "off":
            raise ValueError("a schedule slot must enable series, movies, or both")
        return self


class ScheduleSlot(BaseModel):
    start: str
    end: str
    # Days describe when the slot STARTS. A Friday 22:00 -> 02:00 slot therefore
    # also matches Saturday 01:00, because that broadcast window began Friday.
    days: list[Weekday] = Field(default_factory=lambda: list(ALL_WEEKDAYS))
    programming: SlotProgramming = Field(default_factory=SlotProgramming)
    weights: ContentWeights = Field(default_factory=ContentWeights)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_filters(cls, value):
        """Convert the old parallel include/exclude format into one selection mode.

        This keeps existing exported channels working while ensuring the next
        rewrite uses the unambiguous ``programming`` structure.
        """
        if not isinstance(value, dict) or "programming" in value:
            return value

        data = dict(value)
        content = set(data.get("content") or ["series", "movies"])

        series_include = _unique_strings(list(data.get("series_include") or []))
        series_exclude = set(_unique_strings(list(data.get("series_exclude") or [])))
        if "series" not in content:
            series = {"mode": "off", "items": []}
        elif series_include:
            # In the legacy format exclusion had priority. Collapse both lists
            # into the resulting positive selection. If they cancel completely,
            # preserve the old selector's safe-fallback behaviour as ``all``.
            selected = [item for item in series_include if item not in series_exclude]
            series = {"mode": "only", "items": selected} if selected else {"mode": "all", "items": []}
        elif series_exclude:
            series = {"mode": "except", "items": sorted(series_exclude)}
        else:
            series = {"mode": "all", "items": []}

        franchise_include = _unique_strings(list(data.get("franchise_include") or []))
        movie_include = _unique_strings(list(data.get("movie_include") or []))
        if "movies" not in content:
            movies = {"mode": "off", "franchises": [], "movies": []}
        elif franchise_include or movie_include:
            movies = {
                "mode": "only",
                "franchises": franchise_include,
                "movies": movie_include,
            }
        else:
            movies = {"mode": "all", "franchises": [], "movies": []}

        for legacy_key in (
            "content", "series_include", "series_exclude",
            "franchise_include", "movie_include",
        ):
            data.pop(legacy_key, None)
        data["programming"] = {"series": series, "movies": movies}
        return data

    @field_validator("start", "end")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        _parse_clock(value)
        return value

    @field_validator("days")
    @classmethod
    def validate_days(cls, value: list[Weekday]) -> list[Weekday]:
        if not value:
            raise ValueError("days must contain at least one weekday")
        return list(dict.fromkeys(value))


class ChannelScheduleConfig(BaseModel):
    default: list[ContentType] = Field(default_factory=lambda: ["series", "movies"])
    default_weights: ContentWeights = Field(default_factory=ContentWeights)
    slots: list[ScheduleSlot] = Field(default_factory=list)

    @field_validator("default")
    @classmethod
    def validate_default(cls, value: list[ContentType]) -> list[ContentType]:
        if not value:
            raise ValueError("schedule.default must contain at least one type")
        return list(dict.fromkeys(value))


class ChannelConfig(BaseModel):
    version: int = CONFIG_VERSION
    name: str
    sensitive_content: bool = False
    schedule: ChannelScheduleConfig = Field(default_factory=ChannelScheduleConfig)
    # Loose movies intentionally do not receive movie.yaml yet. Their relative
    # selection weights live in the channel config so the complete channel remains
    # portable while avoiding one config file per standalone movie.
    loose_movie_weights: dict[str, int] = Field(default_factory=dict)

    @field_validator("loose_movie_weights")
    @classmethod
    def validate_loose_movie_weights(cls, value: dict[str, int]) -> dict[str, int]:
        cleaned: dict[str, int] = {}
        for raw_key, raw_weight in value.items():
            key = str(raw_key).strip().replace("\\", "/")
            weight = int(raw_weight)
            if not key:
                continue
            if weight < 1 or weight > 1000:
                raise ValueError("loose movie weights must be between 1 and 1000")
            cleaned[key] = weight
        return cleaned


@dataclass(frozen=True)
class EffectiveSchedule:
    content: frozenset[str]
    series_mode: SelectionMode = "all"
    series_items: frozenset[str] = frozenset()
    movies_mode: SelectionMode = "all"
    franchise_items: frozenset[str] = frozenset()
    movie_items: frozenset[str] = frozenset()
    series_weight: int = 1
    movies_weight: int = 1

    @property
    def has_media_filters(self) -> bool:
        return self.series_mode in {"only", "except"} or self.movies_mode in {"only", "except"}


def default_channel_config(channel_name: str) -> ChannelConfig:
    return ChannelConfig(name=channel_name)


def default_series_config(series_name: str) -> SeriesConfig:
    return SeriesConfig(name=series_name)


def default_franchise_config(franchise_name: str) -> FranchiseConfig:
    return FranchiseConfig(name=franchise_name)


def _parse_clock(value: str) -> time:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return time(hour=hour, minute=minute)
    except Exception as exc:
        raise ValueError(f"Invalid time '{value}', expected HH:MM") from exc


def _yaml_dict(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


def _write_yaml(path: Path, model: BaseModel) -> Path:
    """Atomically persist a validated portable configuration file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(
                _yaml_dict(model),
                handle,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        tmp_path.replace(path)
        logger.info("Saved portable media config: %s", path)
        return path
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _write_yaml_if_missing(path: Path, model: BaseModel) -> bool:
    if path.exists():
        return False
    _write_yaml(path, model)
    logger.info("Created default media config: %s", path)
    return True


def ensure_channel_config(channel_dir: Path) -> Path:
    path = channel_dir / CHANNEL_CONFIG_FILENAME
    _write_yaml_if_missing(path, default_channel_config(channel_dir.name))
    return path


def ensure_series_config(series_dir: Path) -> Path:
    path = series_dir / SERIES_CONFIG_FILENAME
    _write_yaml_if_missing(path, default_series_config(series_dir.name))
    return path


def ensure_franchise_config(franchise_dir: Path) -> Path:
    path = franchise_dir / FRANCHISE_CONFIG_FILENAME
    _write_yaml_if_missing(path, default_franchise_config(franchise_dir.name))
    return path


def _needs_defaults_rewrite(raw: object, model: BaseModel) -> bool:
    """Return True when a valid older file is missing fields now shown in the UI.

    Unknown/manual fields alone do not trigger a rewrite. This keeps migrations
    conservative while ensuring every generated portable file becomes explicit.
    """
    if not isinstance(raw, dict):
        return True
    expected = model.model_dump(mode="json")
    if isinstance(model, ChannelConfig):
        raw_schedule = raw.get("schedule")
        if isinstance(raw_schedule, dict):
            raw_slots = raw_schedule.get("slots")
            if isinstance(raw_slots, list):
                slot_defaults = ScheduleSlot(start="00:00", end="00:00").model_dump(mode="json")
                for slot in raw_slots:
                    if not isinstance(slot, dict):
                        return True
                    if "programming" not in slot:
                        return True
                    if any(key not in slot for key in slot_defaults):
                        return True
                    if any(key in slot for key in (
                        "content", "series_include", "series_exclude",
                        "franchise_include", "movie_include",
                    )):
                        return True
    for key, expected_value in expected.items():
        if key not in raw:
            return True
        raw_value = raw.get(key)
        if isinstance(expected_value, dict) and isinstance(raw_value, dict):
            if any(sub_key not in raw_value for sub_key in expected_value):
                return True
        if isinstance(expected_value, list) and isinstance(raw_value, list) and key == "slots":
            # Each existing slot must gain the advanced fields as well.
            slot_defaults = ScheduleSlot(
                start="00:00", end="00:00"
            ).model_dump(mode="json")
            for slot in raw_value:
                if isinstance(slot, dict) and any(sub_key not in slot for sub_key in slot_defaults):
                    return True
    return False


def _load_yaml_model(path: Path, model_type, fallback: BaseModel, *, rewrite_defaults: bool = False):
    if not path.exists():
        return fallback.model_copy(deep=True)

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        model = model_type.model_validate(raw)
        if getattr(model, "version", CONFIG_VERSION) != CONFIG_VERSION:
            logger.warning(
                "Unsupported config version in %s (got %s, supported %s). Using defaults.",
                path,
                getattr(model, "version", None),
                CONFIG_VERSION,
            )
            return fallback.model_copy(deep=True)
        if rewrite_defaults and _needs_defaults_rewrite(raw, model):
            _write_yaml(path, model)
        return model
    except (OSError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        logger.warning("Invalid config %s: %s. Using defaults.", path, exc)
        return fallback.model_copy(deep=True)


def load_channel_config(channel_dir: Path) -> ChannelConfig:
    path = ensure_channel_config(channel_dir)
    return _load_yaml_model(
        path,
        ChannelConfig,
        default_channel_config(channel_dir.name),
        rewrite_defaults=True,
    )


def load_series_config(series_dir: Path) -> SeriesConfig:
    path = ensure_series_config(series_dir)
    config = _load_yaml_model(
        path,
        SeriesConfig,
        default_series_config(series_dir.name),
        rewrite_defaults=True,
    )
    # Migrate pre-name series.yaml files while the original folder name is still
    # available, so a later physical rename cannot silently change presentation.
    if not config.name.strip():
        config.name = series_dir.name
        _write_yaml(path, config)
    return config


def load_franchise_config(franchise_dir: Path) -> FranchiseConfig:
    path = ensure_franchise_config(franchise_dir)
    return _load_yaml_model(
        path,
        FranchiseConfig,
        default_franchise_config(franchise_dir.name),
        rewrite_defaults=True,
    )


def save_channel_config(channel_dir: Path, config: ChannelConfig) -> Path:
    validated = ChannelConfig.model_validate(config.model_dump())
    return _write_yaml(channel_dir / CHANNEL_CONFIG_FILENAME, validated)


def save_series_config(series_dir: Path, config: SeriesConfig) -> Path:
    validated = SeriesConfig.model_validate(config.model_dump())
    return _write_yaml(series_dir / SERIES_CONFIG_FILENAME, validated)


def save_franchise_config(franchise_dir: Path, config: FranchiseConfig) -> Path:
    validated = FranchiseConfig.model_validate(config.model_dump())
    return _write_yaml(franchise_dir / FRANCHISE_CONFIG_FILENAME, validated)


def get_channel_dir(folder_name: Optional[str], channel_name: str, media_dir: Optional[Path] = None) -> Path:
    root = (media_dir or settings.resolved_media_dir).resolve()
    return root / (folder_name or channel_name)


def get_series_relative_dir(relative_path: str, media_type: str = "episode") -> Optional[str]:
    if media_type != "episode":
        return None
    parts = Path(relative_path).parts
    if len(parts) < 3:
        # DB-only/legacy tests sometimes use synthetic paths such as A/1.mp4.
        # Those do not encode the channel/show hierarchy, so callers must fall
        # back to media_title instead of treating each filename as a series.
        return None
    if parts[1].casefold() != "series" or len(parts) not in {4, 5}:
        return None
    return Path(*parts[1:3]).as_posix()


def get_franchise_relative_dir(relative_path: str, media_type: str = "episode") -> Optional[str]:
    if media_type != "movie":
        return None
    parts = Path(relative_path).parts
    if len(parts) >= 4 and parts[1].casefold() == "movies":
        return Path(*parts[1:3]).as_posix()
    return None


def get_loose_movie_relative_path(relative_path: str, media_type: str = "episode") -> Optional[str]:
    if media_type != "movie":
        return None
    parts = Path(relative_path).parts
    if len(parts) == 3 and parts[1].casefold() == "movies":
        return Path(*parts[1:]).as_posix()
    return None


def get_series_dir_from_relative_path(relative_path: str, media_type: str = "episode", media_dir: Optional[Path] = None) -> Optional[Path]:
    relative = get_series_relative_dir(relative_path, media_type)
    if relative is None:
        return None
    root = (media_dir or settings.resolved_media_dir).resolve()
    channel_folder = Path(relative_path).parts[0]
    return root / channel_folder / relative


def get_franchise_dir_from_relative_path(relative_path: str, media_type: str = "episode", media_dir: Optional[Path] = None) -> Optional[Path]:
    relative = get_franchise_relative_dir(relative_path, media_type)
    if relative is None:
        return None
    root = (media_dir or settings.resolved_media_dir).resolve()
    channel_folder = Path(relative_path).parts[0]
    return root / channel_folder / relative


def load_series_config_for_episode(episode, *, fallback_channel=None, media_dir: Optional[Path] = None) -> SeriesConfig:
    series_dir = get_series_dir_from_relative_path(
        episode.relative_path,
        getattr(episode, "media_type", "episode"),
        media_dir,
    )
    if series_dir is not None and series_dir.exists():
        return load_series_config(series_dir)

    if fallback_channel is not None:
        mode = getattr(fallback_channel, "start_mode", "any")
        if mode not in {"any", "even", "odd"}:
            mode = "any"
        return SeriesConfig(
            episodes_per_airing=max(1, int(getattr(fallback_channel, "batch_size", 1) or 1)),
            start_episode=StartMediaItemConfig(mode=mode),
            playback=PlaybackConfig(mode="random"),
            selection_weight=1,
        )
    series_name = str(getattr(episode, "media_title", "") or "Sin nombre")
    return default_series_config(series_name)


def load_franchise_config_for_movie(movie, *, media_dir: Optional[Path] = None) -> Optional[FranchiseConfig]:
    franchise_dir = get_franchise_dir_from_relative_path(
        movie.relative_path,
        getattr(movie, "media_type", "episode"),
        media_dir,
    )
    if franchise_dir is None or not franchise_dir.exists():
        return None
    return load_franchise_config(franchise_dir)


def _weekday_name(moment: datetime) -> str:
    return ALL_WEEKDAYS[moment.weekday()]


def _slot_contains(slot: ScheduleSlot, moment: datetime) -> bool:
    current = moment.timetz().replace(tzinfo=None)
    start = _parse_clock(slot.start)
    end = _parse_clock(slot.end)
    allowed_days = set(slot.days)

    if start == end:
        return _weekday_name(moment) in allowed_days

    if start < end:
        return _weekday_name(moment) in allowed_days and start <= current < end

    # Cross-midnight. Before ``end`` belongs to the slot that started yesterday.
    if current >= start:
        return _weekday_name(moment) in allowed_days
    if current < end:
        previous_day = _weekday_name(moment - timedelta(days=1))
        return previous_day in allowed_days
    return False


def _default_effective_schedule(content, weights) -> EffectiveSchedule:
    allowed = frozenset(content)
    return EffectiveSchedule(
        content=allowed,
        series_mode="all" if "series" in allowed else "off",
        movies_mode="all" if "movies" in allowed else "off",
        series_weight=weights.series,
        movies_weight=weights.movies,
    )


def _slot_effective_schedule(slot: ScheduleSlot) -> EffectiveSchedule:
    programming = slot.programming
    content: set[str] = set()
    if programming.series.mode != "off":
        content.add("series")
    if programming.movies.mode != "off":
        content.add("movies")
    return EffectiveSchedule(
        content=frozenset(content),
        series_mode=programming.series.mode,
        series_items=frozenset(programming.series.items),
        movies_mode=programming.movies.mode,
        franchise_items=frozenset(programming.movies.franchises),
        movie_items=frozenset(programming.movies.movies),
        series_weight=slot.weights.series,
        movies_weight=slot.weights.movies,
    )


def effective_schedule_for_channel(channel, moment: Optional[datetime] = None, media_dir: Optional[Path] = None) -> EffectiveSchedule:
    """Resolve the first schedule slot that applies at *moment*.

    A slot has exactly one mutually-exclusive mode per media type: off, all,
    only selected, or all except selected. Identifiers are channel-relative paths,
    so the rule survives visible-name changes and full-channel exports.
    """
    channel_dir = get_channel_dir(
        getattr(channel, "folder_name", None),
        channel.name,
        media_dir,
    )
    if not channel_dir.exists():
        return EffectiveSchedule(content=frozenset({"series", "movies"}))

    config = load_channel_config(channel_dir)
    when = moment or datetime.now().astimezone()
    if when.tzinfo is None:
        when = when.astimezone()
    else:
        when = when.astimezone()

    for slot in config.schedule.slots:
        if _slot_contains(slot, when):
            return _slot_effective_schedule(slot)
    return _default_effective_schedule(config.schedule.default, config.schedule.default_weights)


def allowed_content_for_channel(channel, moment: Optional[datetime] = None, media_dir: Optional[Path] = None) -> set[str]:
    return set(effective_schedule_for_channel(channel, moment, media_dir).content)


def schedule_allows_item(channel, item, moment: Optional[datetime] = None, media_dir: Optional[Path] = None, *, ignore_filters: bool = False) -> bool:
    rule = effective_schedule_for_channel(channel, moment, media_dir)
    media_type = getattr(item, "media_type", "episode")

    if media_type == "movie":
        if rule.movies_mode == "off":
            return False
        if ignore_filters or rule.movies_mode == "all":
            return True
        franchise_rel = get_franchise_relative_dir(item.relative_path, media_type)
        loose_rel = get_loose_movie_relative_path(item.relative_path, media_type)
        selected = (
            (franchise_rel is not None and franchise_rel in rule.franchise_items)
            or (loose_rel is not None and loose_rel in rule.movie_items)
        )
        return selected if rule.movies_mode == "only" else not selected

    if rule.series_mode == "off":
        return False
    if ignore_filters or rule.series_mode == "all":
        return True
    series_rel = get_series_relative_dir(item.relative_path, media_type)
    # Synthetic/legacy DB rows without a filesystem series path cannot match an
    # explicit list, but they remain valid for an "except" rule.
    selected = series_rel is not None and series_rel in rule.series_items
    return selected if rule.series_mode == "only" else not selected


def loose_movie_weight_for_channel(channel, item, media_dir: Optional[Path] = None) -> int:
    relative = get_loose_movie_relative_path(
        item.relative_path, getattr(item, "media_type", "episode")
    )
    if relative is None:
        return 1
    channel_dir = get_channel_dir(getattr(channel, "folder_name", None), channel.name, media_dir)
    if not channel_dir.exists():
        return 1
    config = load_channel_config(channel_dir)
    return max(1, int(config.loose_movie_weights.get(relative, 1)))

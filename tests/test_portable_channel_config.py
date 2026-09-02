from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlmodel import select

from app.models.channel import Channel
from app.models.media import MediaItem
from app.core.config import settings
from app.services.media_config import (
    load_channel_config,
    load_franchise_config,
    load_series_config,
)
from app.services.scanner import scan_library
from app.services.selector import select_next_episode


def _dummy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-a-real-video")


@pytest.mark.asyncio
async def test_scan_creates_portable_phase_1_to_4_structure(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "portable_library"
    monkeypatch.setattr(settings, "MEDIA_DIR", root)
    episode = root / "Canal Retro" / "Series" / "JoJo" / "Season 1" / "S01E01 - Dio.mp4"
    movie = root / "Canal Retro" / "Movies" / "Harry Potter" / "Philosopher's Stone.mp4"
    loose_movie = root / "Canal Retro" / "Movies" / "Akira.mp4"
    _dummy(episode)
    _dummy(movie)
    _dummy(loose_movie)

    async def fake_metadata(_path):
        return {
            "duration": 60.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr("app.services.scanner.extract_metadata", fake_metadata)
    result = await scan_library(test_db, root)

    assert result.total_episodes == 3
    channel_dir = root / "Canal Retro"
    assert (channel_dir / "channel.yaml").exists()
    assert (channel_dir / "Series").exists()
    assert (channel_dir / "Movies").exists()
    assert (channel_dir / "Series" / "JoJo" / "series.yaml").exists()
    assert (channel_dir / "Movies" / "Harry Potter" / "franchise.yaml").exists()

    series_cfg = load_series_config(channel_dir / "Series" / "JoJo")
    assert series_cfg.episodes_per_airing == 1
    assert series_cfg.start_episode.mode == "any"
    assert series_cfg.playback.mode == "random"

    franchise_cfg = load_franchise_config(channel_dir / "Movies" / "Harry Potter")
    assert franchise_cfg.name == "Harry Potter"

    rows = test_db.exec(select(MediaItem).order_by(MediaItem.relative_path)).all()
    movies = [row for row in rows if row.media_type == "movie"]
    episodes = [row for row in rows if row.media_type == "episode"]
    assert len(episodes) == 1
    assert len(movies) == 2
    assert episodes[0].media_title == "JoJo"
    hp = next(row for row in movies if row.franchise == "Harry Potter")
    assert hp.media_title == "Philosopher's Stone"
    akira = next(row for row in movies if row.media_title == "Akira")
    assert akira.franchise is None


@pytest.mark.asyncio
async def test_series_yaml_controls_batch_and_start_parity(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "series_rules"
    monkeypatch.setattr(settings, "MEDIA_DIR", root)
    show = root / "Canal 1" / "Series" / "Show" / "Season 1"
    for number in range(1, 5):
        _dummy(show / f"S01E{number:02d}.mp4")

    async def fake_metadata(_path):
        return {
            "duration": 60.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr("app.services.scanner.extract_metadata", fake_metadata)
    await scan_library(test_db, root)

    config_path = root / "Canal 1" / "Series" / "Show" / "series.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "episodes_per_airing": 3,
                "start_episode": {"mode": "odd"},
                "playback": {"mode": "random"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal 1")).one()
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    first = select_next_episode(test_db, channel.id)
    assert first is not None
    assert first.episode_number % 2 == 1

    second = select_next_episode(test_db, channel.id, first.id, consecutive_plays=1)
    assert second is not None
    assert second.episode_number == first.episode_number + 1

    third = select_next_episode(test_db, channel.id, second.id, consecutive_plays=2)
    assert third is not None
    assert third.episode_number == second.episode_number + 1


@pytest.mark.asyncio
async def test_channel_schedule_filters_series_and_movies(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "schedule_rules"
    monkeypatch.setattr(settings, "MEDIA_DIR", root)
    _dummy(root / "Night Channel" / "Series" / "Show" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Night Channel" / "Movies" / "Movie.mp4")

    async def fake_metadata(_path):
        return {
            "duration": 60.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr("app.services.scanner.extract_metadata", fake_metadata)
    await scan_library(test_db, root)

    channel_dir = root / "Night Channel"
    config_path = channel_dir / "channel.yaml"
    cfg = load_channel_config(channel_dir)
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": cfg.name,
                "schedule": {
                    "default": ["series"],
                    "slots": [
                        {"start": "20:00", "end": "06:00", "content": ["movies"]},
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Night Channel")).one()
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    # Naive datetimes are interpreted as local server time, which matches how a
    # human writes the channel schedule.
    daytime = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 12, 0))
    nighttime = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 22, 0))

    assert daytime is not None and daytime.media_type == "episode"
    assert nighttime is not None and nighttime.media_type == "movie"


@pytest.mark.asyncio
async def test_legacy_hierarchy_is_ignored_but_never_moved(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "legacy_library"
    monkeypatch.setattr(settings, "MEDIA_DIR", root)
    legacy_file = root / "Canal Legacy" / "JoJo" / "Season 1" / "S01E01.mp4"
    _dummy(legacy_file)

    async def fake_metadata(_path):
        return {
            "duration": 60.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr("app.services.scanner.extract_metadata", fake_metadata)
    await scan_library(test_db, root)

    assert legacy_file.exists(), "The scanner must never move legacy media automatically"
    assert not (root / "Canal Legacy" / "JoJo" / "series.yaml").exists()
    assert (root / "Canal Legacy" / "Series").is_dir()
    assert (root / "Canal Legacy" / "Movies").is_dir()
    assert test_db.exec(select(Channel)).all() == []
    assert test_db.exec(select(MediaItem)).all() == []

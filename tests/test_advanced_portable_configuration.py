from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import select

from app.core.config import settings
from app.models.channel import Channel
from app.models.media import MediaItem
from app.services.media_config import (
    ChannelConfig,
    FranchiseConfig,
    PlaybackConfig,
    SeriesConfig,
    load_channel_config,
    load_franchise_config,
    load_series_config,
    save_channel_config,
    save_franchise_config,
    save_series_config,
)
from app.services.scanner import scan_library
from app.services.selector import select_next_episode


def _dummy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"dummy")


async def _scan(test_db, root: Path, monkeypatch) -> None:
    async def fake_metadata(_path):
        return {
            "duration": 60.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr(settings, "MEDIA_DIR", root)
    monkeypatch.setattr("app.services.scanner.extract_metadata", fake_metadata)
    await scan_library(test_db, root)


@pytest.mark.asyncio
async def test_series_sequential_continues_after_last_played(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_series_sequential"
    for number in range(1, 5):
        _dummy(root / "Canal" / "Series" / "Show" / "Season 1" / f"S01E{number:02d}.mp4")
    await _scan(test_db, root, monkeypatch)

    series_dir = root / "Canal" / "Series" / "Show"
    cfg = load_series_config(series_dir)
    cfg.playback.mode = "sequential"
    cfg.episodes_per_airing = 1
    save_series_config(series_dir, cfg)

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    rows = test_db.exec(
        select(MediaItem).where(MediaItem.channel_id == channel.id).order_by(MediaItem.episode_number)
    ).all()
    rows[1].last_played_at = datetime(2026, 8, 30, 20, 0)
    rows[1].play_count = 1
    test_db.add(rows[1])
    test_db.commit()

    selected = select_next_episode(test_db, channel.id)
    assert selected is not None
    assert selected.episode_number == 3


@pytest.mark.asyncio
async def test_franchise_sequential_uses_file_title_order(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_franchise_sequential"
    for number in range(1, 4):
        _dummy(root / "Canal" / "Movies" / "Saga" / f"0{number} Movie.mp4")
    await _scan(test_db, root, monkeypatch)

    channel_dir = root / "Canal"
    channel_cfg = load_channel_config(channel_dir)
    channel_cfg.schedule.default = ["movies"]
    save_channel_config(channel_dir, channel_cfg)

    franchise_dir = channel_dir / "Movies" / "Saga"
    franchise_cfg = load_franchise_config(franchise_dir)
    franchise_cfg.playback.mode = "sequential"
    save_franchise_config(franchise_dir, franchise_cfg)

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    movies = test_db.exec(
        select(MediaItem).where(MediaItem.channel_id == channel.id).order_by(MediaItem.media_title)
    ).all()
    movies[0].last_played_at = datetime(2026, 8, 30, 20, 0)
    movies[0].play_count = 1
    test_db.add(movies[0])
    test_db.commit()

    selected = select_next_episode(test_db, channel.id)
    assert selected is not None
    assert selected.media_title == "02 Movie"


@pytest.mark.asyncio
async def test_cross_midnight_slot_uses_start_weekday(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_days"
    _dummy(root / "Canal" / "Series" / "Show" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal" / "Movies" / "Movie.mp4")
    await _scan(test_db, root, monkeypatch)

    channel_dir = root / "Canal"
    cfg = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["series"],
            "slots": [{
                "start": "22:00",
                "end": "02:00",
                "content": ["movies"],
                "days": ["friday"],
            }],
        },
    })
    save_channel_config(channel_dir, cfg)
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()

    # 2026-09-04 is Friday; 2026-09-05 01:00 is Saturday but still belongs
    # to the Friday slot that started at 22:00.
    friday_night = select_next_episode(test_db, channel.id, at_time=datetime(2026, 9, 4, 23, 0))
    saturday_early = select_next_episode(test_db, channel.id, at_time=datetime(2026, 9, 5, 1, 0))
    saturday_late = select_next_episode(test_db, channel.id, at_time=datetime(2026, 9, 5, 3, 0))

    assert friday_night is not None and friday_night.media_type == "movie"
    assert saturday_early is not None and saturday_early.media_type == "movie"
    assert saturday_late is not None and saturday_late.media_type == "episode"


@pytest.mark.asyncio
async def test_schedule_series_include_and_exclude(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_series_filters"
    _dummy(root / "Canal" / "Series" / "A" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal" / "Series" / "B" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal" / "Series" / "C" / "Season 1" / "S01E01.mp4")
    await _scan(test_db, root, monkeypatch)

    cfg = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["series"],
            "slots": [{
                "start": "00:00", "end": "00:00", "content": ["series"],
                "series_include": ["Series/A", "Series/B"],
                "series_exclude": ["Series/B"],
            }],
        },
    })
    save_channel_config(root / "Canal", cfg)
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    selected = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 12, 0))
    assert selected is not None
    assert selected.media_title == "A"


@pytest.mark.asyncio
async def test_schedule_can_limit_franchises_and_specific_loose_movies(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_movie_filters"
    _dummy(root / "Canal" / "Movies" / "Allowed Saga" / "Film.mp4")
    _dummy(root / "Canal" / "Movies" / "Blocked Saga" / "Film.mp4")
    _dummy(root / "Canal" / "Movies" / "Allowed Loose.mp4")
    _dummy(root / "Canal" / "Movies" / "Blocked Loose.mp4")
    await _scan(test_db, root, monkeypatch)

    cfg = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["movies"],
            "slots": [{
                "start": "00:00", "end": "00:00", "content": ["movies"],
                "franchise_include": ["Movies/Allowed Saga"],
                "movie_include": ["Movies/Allowed Loose.mp4"],
            }],
        },
    })
    save_channel_config(root / "Canal", cfg)
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    for _ in range(5):
        selected = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 12, 0))
        assert selected is not None
        assert (
            selected.franchise == "Allowed Saga"
            or selected.media_title == "Allowed Loose"
        )


@pytest.mark.asyncio
async def test_type_weights_drive_series_vs_movies(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_type_weights"
    _dummy(root / "Canal" / "Series" / "Show" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal" / "Movies" / "Movie.mp4")
    await _scan(test_db, root, monkeypatch)

    cfg = load_channel_config(root / "Canal")
    cfg.schedule.default = ["series", "movies"]
    cfg.schedule.default_weights.series = 1
    cfg.schedule.default_weights.movies = 100
    save_channel_config(root / "Canal", cfg)

    def choose_highest(population, weights=None, k=1):
        assert weights is not None
        best = max(range(len(population)), key=lambda index: weights[index])
        return [population[best]]

    monkeypatch.setattr("app.services.selector.random.choices", choose_highest)
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    selected = select_next_episode(test_db, channel.id)
    assert selected is not None and selected.media_type == "movie"


@pytest.mark.asyncio
async def test_series_program_weights_are_read_from_series_yaml(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_series_weights"
    _dummy(root / "Canal" / "Series" / "A" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal" / "Series" / "B" / "Season 1" / "S01E01.mp4")
    await _scan(test_db, root, monkeypatch)

    channel_cfg = load_channel_config(root / "Canal")
    channel_cfg.schedule.default = ["series"]
    save_channel_config(root / "Canal", channel_cfg)
    a_cfg = load_series_config(root / "Canal" / "Series" / "A")
    b_cfg = load_series_config(root / "Canal" / "Series" / "B")
    a_cfg.selection_weight = 1
    b_cfg.selection_weight = 50
    save_series_config(root / "Canal" / "Series" / "A", a_cfg)
    save_series_config(root / "Canal" / "Series" / "B", b_cfg)

    def choose_highest(population, weights=None, k=1):
        best = max(range(len(population)), key=lambda index: weights[index])
        return [population[best]]

    monkeypatch.setattr("app.services.selector.random.choices", choose_highest)
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    selected = select_next_episode(test_db, channel.id)
    assert selected is not None and selected.media_title == "B"


@pytest.mark.asyncio
async def test_franchise_and_loose_movie_weights_compete(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "advanced_movie_weights"
    _dummy(root / "Canal" / "Movies" / "Saga" / "Film.mp4")
    _dummy(root / "Canal" / "Movies" / "Loose.mp4")
    await _scan(test_db, root, monkeypatch)

    channel_cfg = load_channel_config(root / "Canal")
    channel_cfg.schedule.default = ["movies"]
    channel_cfg.loose_movie_weights = {"Movies/Loose.mp4": 80}
    save_channel_config(root / "Canal", channel_cfg)
    franchise_cfg = load_franchise_config(root / "Canal" / "Movies" / "Saga")
    franchise_cfg.selection_weight = 1
    save_franchise_config(root / "Canal" / "Movies" / "Saga", franchise_cfg)

    def choose_highest(population, weights=None, k=1):
        best = max(range(len(population)), key=lambda index: weights[index])
        return [population[best]]

    monkeypatch.setattr("app.services.selector.random.choices", choose_highest)
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    selected = select_next_episode(test_db, channel.id)
    assert selected is not None
    assert selected.media_title == "Loose"


def test_admin_persists_advanced_configuration_to_yaml(client, test_db, sample_media_dir):
    # Add one franchise and one standalone movie so all advanced movie controls
    # have real stable identifiers to persist.
    franchise_dir = sample_media_dir / "Canal 1" / "Movies" / "Saga"
    franchise_dir.mkdir(parents=True, exist_ok=True)
    (franchise_dir / "01 Film.mp4").write_bytes(b"dummy")
    loose = sample_media_dir / "Canal 1" / "Movies" / "Standalone.mp4"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"dummy")

    scan = client.post("/api/library/scan")
    assert scan.status_code == 200
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal 1")).one()
    current = client.get(f"/api/admin/channels/{channel.id}/configuration")
    assert current.status_code == 200
    payload = current.json()

    jojo = next(item for item in payload["series"] if item["name"] == "JoJo")
    saga = next(item for item in payload["franchises"] if item["folder_name"] == "Saga")
    standalone = next(item for item in payload["loose_movie_items"] if item["name"] == "Standalone")

    save_channel = client.put(
        f"/api/admin/channels/{channel.id}/configuration",
        json={
            "version": 1,
            "name": "Canal 1",
            "sensitive_content": False,
            "loose_movie_weights": {standalone["relative_path"]: 9},
            "schedule": {
                "default": ["series", "movies"],
                "default_weights": {"series": 70, "movies": 30},
                "slots": [{
                    "start": "20:00",
                    "end": "02:00",
                    "content": ["series", "movies"],
                    "days": ["friday", "saturday"],
                    "series_include": [jojo["relative_dir"]],
                    "series_exclude": [],
                    "franchise_include": [saga["relative_dir"]],
                    "movie_include": [standalone["relative_path"]],
                    "weights": {"series": 25, "movies": 75},
                }],
            },
        },
    )
    assert save_channel.status_code == 200, save_channel.text

    channel_yaml = __import__("yaml").safe_load(
        (sample_media_dir / "Canal 1" / "channel.yaml").read_text(encoding="utf-8")
    )
    assert channel_yaml["schedule"]["default_weights"] == {"series": 70, "movies": 30}
    assert channel_yaml["schedule"]["slots"][0]["days"] == ["friday", "saturday"]
    programming = channel_yaml["schedule"]["slots"][0]["programming"]
    assert programming["series"] == {"mode": "only", "items": [jojo["relative_dir"]]}
    assert programming["movies"]["mode"] == "only"
    assert programming["movies"]["franchises"] == [saga["relative_dir"]]
    assert programming["movies"]["movies"] == [standalone["relative_path"]]
    assert channel_yaml["loose_movie_weights"][standalone["relative_path"]] == 9

    save_series = client.put(
        f"/api/admin/channels/{channel.id}/series/configuration",
        json={
            "relative_dir": jojo["relative_dir"],
            "config": {
                "version": 1,
                "episodes_per_airing": 2,
                "start_episode": {"mode": "odd"},
                "playback": {"mode": "sequential"},
                "selection_weight": 11,
            },
        },
    )
    assert save_series.status_code == 200, save_series.text
    series_yaml = __import__("yaml").safe_load(
        (sample_media_dir / "Canal 1" / "JoJo" / "series.yaml").read_text(encoding="utf-8")
    )
    assert series_yaml["playback"]["mode"] == "sequential"
    assert series_yaml["selection_weight"] == 11

    save_franchise = client.put(
        f"/api/admin/channels/{channel.id}/franchises/configuration",
        json={
            "relative_dir": saga["relative_dir"],
            "config": {
                "version": 1,
                "name": "Saga",
                "playback": {"mode": "sequential"},
                "selection_weight": 13,
            },
        },
    )
    assert save_franchise.status_code == 200, save_franchise.text
    franchise_yaml = __import__("yaml").safe_load(
        (sample_media_dir / "Canal 1" / "Movies" / "Saga" / "franchise.yaml").read_text(encoding="utf-8")
    )
    assert franchise_yaml["playback"]["mode"] == "sequential"
    assert franchise_yaml["selection_weight"] == 13


def test_existing_portable_files_gain_advanced_defaults_without_manual_edit(test_temp_dir):
    import yaml

    root = test_temp_dir / "advanced_config_migration"
    channel_dir = root / "Canal"
    series_dir = channel_dir / "Series" / "Show"
    franchise_dir = channel_dir / "Movies" / "Saga"
    series_dir.mkdir(parents=True, exist_ok=True)
    franchise_dir.mkdir(parents=True, exist_ok=True)

    (channel_dir / "channel.yaml").write_text(
        yaml.safe_dump({
            "version": 1,
            "name": "Canal",
            "schedule": {
                "default": ["series", "movies"],
                "slots": [{"start": "20:00", "end": "22:00", "content": ["movies"]}],
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    (series_dir / "series.yaml").write_text(
        yaml.safe_dump({
            "version": 1,
            "episodes_per_airing": 1,
            "start_episode": {"mode": "any"},
            "playback": {"mode": "random"},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (franchise_dir / "franchise.yaml").write_text(
        yaml.safe_dump({"version": 1, "name": "Saga"}, sort_keys=False),
        encoding="utf-8",
    )

    load_channel_config(channel_dir)
    load_series_config(series_dir)
    load_franchise_config(franchise_dir)

    channel_raw = yaml.safe_load((channel_dir / "channel.yaml").read_text(encoding="utf-8"))
    series_raw = yaml.safe_load((series_dir / "series.yaml").read_text(encoding="utf-8"))
    franchise_raw = yaml.safe_load((franchise_dir / "franchise.yaml").read_text(encoding="utf-8"))

    assert channel_raw["schedule"]["default_weights"] == {"series": 1, "movies": 1}
    assert channel_raw["schedule"]["slots"][0]["days"] == [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]
    assert channel_raw["schedule"]["slots"][0]["weights"] == {"series": 1, "movies": 1}
    assert channel_raw["schedule"]["slots"][0]["programming"]["series"]["mode"] == "off"
    assert channel_raw["schedule"]["slots"][0]["programming"]["movies"]["mode"] == "all"
    assert "content" not in channel_raw["schedule"]["slots"][0]
    assert "series_include" not in channel_raw["schedule"]["slots"][0]
    assert "series_exclude" not in channel_raw["schedule"]["slots"][0]
    assert channel_raw["loose_movie_weights"] == {}
    assert series_raw["selection_weight"] == 1
    assert franchise_raw["playback"]["mode"] == "random"
    assert franchise_raw["selection_weight"] == 1

@pytest.mark.asyncio
async def test_unified_programming_modes_are_mutually_exclusive_and_effective(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "unified_programming_series"
    _dummy(root / "Canal" / "Series" / "A" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal" / "Series" / "B" / "Season 1" / "S01E01.mp4")
    await _scan(test_db, root, monkeypatch)

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    only_a = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["series"],
            "slots": [{
                "start": "00:00",
                "end": "00:00",
                "programming": {
                    "series": {"mode": "only", "items": ["Series/A"]},
                    "movies": {"mode": "off", "franchises": [], "movies": []},
                },
            }],
        },
    })
    save_channel_config(root / "Canal", only_a)
    selected = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 12, 0))
    assert selected is not None and selected.media_title == "A"

    except_a = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["series"],
            "slots": [{
                "start": "00:00",
                "end": "00:00",
                "programming": {
                    "series": {"mode": "except", "items": ["Series/A"]},
                    "movies": {"mode": "off", "franchises": [], "movies": []},
                },
            }],
        },
    })
    save_channel_config(root / "Canal", except_a)
    selected = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 12, 0))
    assert selected is not None and selected.media_title == "B"


@pytest.mark.asyncio
async def test_unified_movie_except_rule_uses_one_selection_list(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "unified_programming_movies"
    _dummy(root / "Canal" / "Movies" / "Saga" / "Film.mp4")
    _dummy(root / "Canal" / "Movies" / "Loose.mp4")
    await _scan(test_db, root, monkeypatch)

    cfg = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["movies"],
            "slots": [{
                "start": "00:00",
                "end": "00:00",
                "programming": {
                    "series": {"mode": "off", "items": []},
                    "movies": {
                        "mode": "except",
                        "franchises": ["Movies/Saga"],
                        "movies": [],
                    },
                },
            }],
        },
    })
    save_channel_config(root / "Canal", cfg)
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal")).one()
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    selected = select_next_episode(test_db, channel.id, at_time=datetime(2026, 8, 31, 12, 0))
    assert selected is not None
    assert selected.media_type == "movie"
    assert selected.franchise is None
    assert selected.media_title == "Loose"

def test_legacy_conflicting_include_exclude_migrates_without_ambiguity():
    cfg = ChannelConfig.model_validate({
        "version": 1,
        "name": "Canal",
        "schedule": {
            "default": ["series"],
            "slots": [{
                "start": "18:00",
                "end": "20:00",
                "content": ["series"],
                "series_include": ["Series/JoJo"],
                "series_exclude": ["Series/JoJo"],
            }],
        },
    })

    slot = cfg.schedule.slots[0]
    assert slot.programming.series.mode == "all"
    assert slot.programming.series.items == []
    assert slot.programming.movies.mode == "off"

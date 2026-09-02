from pathlib import Path

import pytest
import yaml
from sqlmodel import Session, select

from app.api.media import to_media_item_read
from app.models.access import AccessGroup, GroupChannelAccess
from app.models.channel import Channel
from app.models.media import MediaItem
from app.services.channel import ChannelEngine
from app.services.media_config import load_channel_config, load_series_config
from app.services.scanner import move_indexed_path, scan_library
from app.services.watcher import MediaWatcher


def _media(path: Path, payload: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def fake_metadata(monkeypatch):
    async def extract(_path):
        return {
            "duration": 60.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr("app.services.scanner.extract_metadata", extract)


@pytest.mark.asyncio
async def test_empty_channel_is_prepared_but_not_activated(test_db, test_temp_dir, fake_metadata):
    root = test_temp_dir / "empty_candidate"
    channel_dir = root / "Canal vacío"
    channel_dir.mkdir(parents=True)

    result = await scan_library(test_db, root)

    assert result.total_episodes == 0
    assert (channel_dir / "channel.yaml").is_file()
    assert (channel_dir / "Series").is_dir()
    assert (channel_dir / "Movies").is_dir()
    assert test_db.exec(select(Channel)).all() == []


@pytest.mark.asyncio
async def test_watcher_prepares_new_empty_channel_while_running(
    test_db, test_temp_dir, fake_metadata
):
    root = test_temp_dir / "watched_empty_candidate"
    root.mkdir()
    watcher = MediaWatcher(
        media_dir=root,
        debounce_seconds=0.05,
        audit_seconds=60,
        session_factory=lambda: Session(test_db.bind),
    )
    watcher.start()
    try:
        channel_dir = root / "Creado en vivo"
        channel_dir.mkdir()
        watcher.queue_change(channel_dir, is_directory=True)
        import asyncio
        await asyncio.sleep(0.8)

        assert (channel_dir / "channel.yaml").is_file()
        assert (channel_dir / "Series").is_dir()
        assert (channel_dir / "Movies").is_dir()
        with Session(test_db.bind) as session:
            assert session.exec(select(Channel)).all() == []
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_emptying_and_repopulating_channel_creates_new_identity_without_grant(
    test_db, test_temp_dir, fake_metadata
):
    root = test_temp_dir / "channel_lifecycle"
    episode = _media(root / "Folder" / "Series" / "Show" / "E01.mp4")
    await scan_library(test_db, root)
    original = test_db.exec(select(Channel)).one()

    group = AccessGroup(name="Viewers")
    test_db.add(group)
    test_db.flush()
    test_db.add(GroupChannelAccess(group_id=group.id, channel_id=original.id))
    test_db.commit()

    engine = ChannelEngine()
    await engine.initialize(test_db)
    assert await engine.get_current_state(test_db, original.id) is not None

    episode.unlink()
    result = await scan_library(test_db, root)
    await engine.notify_library_changed(test_db)

    assert result.channels_deleted == 1
    assert test_db.get(Channel, original.id) is None
    assert test_db.exec(select(GroupChannelAccess)).all() == []
    assert await engine.get_current_state(test_db, original.id) is None
    assert (root / "Folder" / "channel.yaml").is_file()

    _media(root / "Folder" / "Series" / "Show" / "E02.mp4")
    await scan_library(test_db, root)
    replacement = test_db.exec(select(Channel)).one()
    assert replacement.id != original.id
    assert test_db.exec(select(GroupChannelAccess)).all() == []


@pytest.mark.asyncio
async def test_channel_folder_rename_preserves_channel_media_history_and_grants(
    test_db, test_temp_dir, fake_metadata
):
    root = test_temp_dir / "channel_rename"
    old_dir = root / "physical-old"
    episode = _media(old_dir / "Series" / "Show" / "E01.mp4")
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "channel.yaml").write_text(
        yaml.safe_dump({"version": 1, "name": "Nombre estable"}, sort_keys=False),
        encoding="utf-8",
    )
    await scan_library(test_db, root)
    channel = test_db.exec(select(Channel)).one()
    item = test_db.exec(select(MediaItem)).one()
    item.play_count = 8
    test_db.add(item)
    group = AccessGroup(name="Allowed")
    test_db.add(group)
    test_db.flush()
    test_db.add(GroupChannelAccess(group_id=group.id, channel_id=channel.id))
    test_db.commit()

    new_dir = root / "physical-new"
    old_dir.rename(new_dir)
    assert await move_indexed_path(test_db, old_dir, new_dir, root) is True
    await scan_library(test_db, root)

    renamed_channel = test_db.exec(select(Channel)).one()
    renamed_item = test_db.exec(select(MediaItem)).one()
    grant = test_db.exec(select(GroupChannelAccess)).one()
    assert renamed_channel.id == channel.id
    assert renamed_channel.folder_name == "physical-new"
    assert renamed_channel.name == "Nombre estable"
    assert renamed_item.id == item.id
    assert renamed_item.play_count == 8
    assert renamed_item.relative_path == "physical-new/Series/Show/E01.mp4"
    assert grant.channel_id == channel.id


@pytest.mark.asyncio
async def test_series_folder_rename_preserves_name_identity_history_and_yaml_reference(
    test_db, test_temp_dir, fake_metadata
):
    root = test_temp_dir / "series_rename"
    old_series = root / "Canal" / "Series" / "old-folder"
    _media(old_series / "S01E01.mp4")
    old_series.mkdir(parents=True, exist_ok=True)
    (old_series / "series.yaml").write_text(
        yaml.safe_dump({"version": 1, "name": "Nombre visible"}, sort_keys=False),
        encoding="utf-8",
    )
    await scan_library(test_db, root)
    channel = test_db.exec(select(Channel)).one()
    item = test_db.exec(select(MediaItem)).one()
    item.play_count = 5
    test_db.add(item)

    channel_dir = root / "Canal"
    config = load_channel_config(channel_dir)
    config.schedule.slots = []
    config.loose_movie_weights = {}
    raw = config.model_dump(mode="json")
    raw["schedule"]["slots"] = [{
        "start": "00:00",
        "end": "00:00",
        "programming": {
            "series": {"mode": "only", "items": ["Series/old-folder"]},
            "movies": {"mode": "off", "franchises": [], "movies": []},
        },
        "weights": {"series": 1, "movies": 1},
    }]
    (channel_dir / "channel.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
    )
    test_db.commit()

    new_series = channel_dir / "Series" / "new-folder"
    old_series.rename(new_series)
    assert await move_indexed_path(test_db, old_series, new_series, root) is True

    moved = test_db.exec(select(MediaItem)).one()
    config = load_channel_config(channel_dir)
    assert moved.id == item.id
    assert moved.channel_id == channel.id
    assert moved.play_count == 5
    assert moved.media_title == "Nombre visible"
    assert load_series_config(new_series).name == "Nombre visible"
    assert config.schedule.slots[0].programming.series.items == ["Series/new-folder"]


@pytest.mark.asyncio
async def test_cross_channel_move_is_delete_and_create(test_db, test_temp_dir, fake_metadata):
    root = test_temp_dir / "cross_channel"
    source = _media(root / "A" / "Series" / "Show" / "E01.mp4")
    _media(root / "A" / "Movies" / "Keep A.mp4")
    _media(root / "B" / "Movies" / "Keep B.mp4")
    await scan_library(test_db, root)
    original = test_db.exec(
        select(MediaItem).where(MediaItem.relative_path == "A/Series/Show/E01.mp4")
    ).one()
    original.play_count = 12
    test_db.add(original)
    test_db.commit()

    destination = root / "B" / "Series" / "Show" / "E01.mp4"
    destination.parent.mkdir(parents=True)
    source.rename(destination)
    assert await move_indexed_path(test_db, source, destination, root) is False
    await scan_library(test_db, root)

    replacement = test_db.exec(
        select(MediaItem).where(MediaItem.relative_path == "B/Series/Show/E01.mp4")
    ).one()
    assert replacement.id != original.id
    assert replacement.play_count == 0


@pytest.mark.asyncio
async def test_seasonless_episode_uses_zero_only_in_storage(test_db, test_temp_dir, fake_metadata):
    root = test_temp_dir / "seasonless"
    _media(root / "Canal" / "Series" / "No Seasons" / "S01E07 - Finale.mp4")
    await scan_library(test_db, root)

    item = test_db.exec(select(MediaItem)).one()
    assert item.season_number == 0
    assert item.episode_number == 7
    assert item.episode_title == "Finale"
    assert to_media_item_read(item).season_number is None


@pytest.mark.asyncio
async def test_empty_series_rename_keeps_dormant_reference(test_db, test_temp_dir, fake_metadata):
    root = test_temp_dir / "empty_series_rename"
    channel_dir = root / "Canal"
    _media(channel_dir / "Movies" / "Keep.mp4")
    empty_old = channel_dir / "Series" / "Dormant"
    empty_old.mkdir(parents=True)
    await scan_library(test_db, root)

    config_path = channel_dir / "channel.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["schedule"]["slots"] = [{
        "start": "00:00",
        "end": "00:00",
        "programming": {
            "series": {"mode": "only", "items": ["Series/Dormant"]},
            "movies": {"mode": "off", "franchises": [], "movies": []},
        },
        "weights": {"series": 1, "movies": 1},
        "days": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    }]
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    empty_new = channel_dir / "Series" / "Dormant Renamed"
    empty_old.rename(empty_new)
    assert await move_indexed_path(test_db, empty_old, empty_new, root) is True
    config = load_channel_config(channel_dir)
    assert config.schedule.slots[0].programming.series.items == ["Series/Dormant Renamed"]
    assert (empty_new / "series.yaml").is_file()

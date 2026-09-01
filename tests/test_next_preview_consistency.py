from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from sqlmodel import select

from app.core.config import settings
from app.models.channel import Channel, ChannelState
from app.models.media import MediaItem
from app.services.channel import ChannelEngine
from app.services.scanner import scan_library


def _dummy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-a-real-video")


async def _portable_mixed_channel(test_db, root: Path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_DIR", root)
    _dummy(root / "Canal Mix" / "Series" / "Show A" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal Mix" / "Series" / "Show B" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal Mix" / "Series" / "Show C" / "Season 1" / "S01E01.mp4")
    _dummy(root / "Canal Mix" / "Movies" / "Movie One.mp4")

    async def fake_metadata(_path):
        return {
            "duration": 600.0,
            "mime_type": "video/mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    monkeypatch.setattr("app.services.scanner.extract_metadata", fake_metadata)
    await scan_library(test_db, root)

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal Mix")).one()
    config_path = root / "Canal Mix" / "channel.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "Canal Mix",
                "schedule": {"default": ["series"], "slots": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return channel


@pytest.mark.asyncio
async def test_now_playing_never_exposes_disallowed_cached_movie(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "next_preview_heal"
    channel = await _portable_mixed_channel(test_db, root, monkeypatch)

    engine = ChannelEngine()
    await engine.initialize(test_db)
    pb = engine._channels[channel.id]

    current = test_db.get(MediaItem, pb._current_episode_id)
    assert current is not None and current.media_type == "episode"

    movie = test_db.exec(
        select(MediaItem).where(MediaItem.channel_id == channel.id, MediaItem.media_type == "movie")
    ).one()

    # Simulate the exact stale-preview condition reported by the UI: the cached
    # next item still points to a movie even though the channel default is series-only.
    pb._next_episode_id = movie.id
    persisted = test_db.get(ChannelState, channel.id)
    assert persisted is not None
    persisted.next_episode_id = movie.id
    test_db.add(persisted)
    test_db.commit()

    state = await engine.get_current_state(test_db, channel.id)

    assert state is not None
    assert state.next_episode is not None
    assert state.next_episode.media_type == "episode"
    assert pb._next_episode_id != movie.id


@pytest.mark.asyncio
async def test_refresh_channel_schedule_notifies_viewers_of_preview_change(test_db, test_temp_dir, monkeypatch):
    root = test_temp_dir / "next_preview_event"
    channel = await _portable_mixed_channel(test_db, root, monkeypatch)

    engine = ChannelEngine()
    await engine.initialize(test_db)
    pb = engine._channels[channel.id]

    movie = test_db.exec(
        select(MediaItem).where(MediaItem.channel_id == channel.id, MediaItem.media_type == "movie")
    ).one()
    pb._next_episode_id = movie.id

    persisted = test_db.get(ChannelState, channel.id)
    assert persisted is not None
    persisted.next_episode_id = movie.id
    test_db.add(persisted)
    test_db.commit()

    queue = engine.subscribe(channel.id)
    initial_revision = queue.get_nowait()

    await engine.refresh_channel_schedule(test_db, channel.id)

    pushed_revision = queue.get_nowait()
    assert pushed_revision > initial_revision

    next_item = test_db.get(MediaItem, pb._next_episode_id)
    assert next_item is not None
    assert next_item.media_type == "episode"


@pytest.mark.asyncio
async def test_natural_transition_plays_the_advertised_next_item(
    test_db, test_temp_dir, monkeypatch
):
    root = test_temp_dir / "next_preview_natural_transition"
    channel = await _portable_mixed_channel(test_db, root, monkeypatch)

    engine = ChannelEngine()
    await engine.initialize(test_db)
    before = await engine.get_current_state(test_db, channel.id)
    assert before is not None
    assert before.next_episode is not None
    advertised_id = before.next_episode.id

    # If the engine re-runs the random selector at the boundary, force that
    # second roll to disagree with the already advertised reservation.
    different = test_db.exec(
        select(MediaItem).where(
            MediaItem.channel_id == channel.id,
            MediaItem.media_type == "episode",
            MediaItem.id != advertised_id,
        )
    ).first()
    assert different is not None
    selector_calls = 0

    def disagreeing_selector(*_args, **_kwargs):
        nonlocal selector_calls
        selector_calls += 1
        return different

    monkeypatch.setattr("app.services.channel.select_next_episode", disagreeing_selector)
    pb = engine._channels[channel.id]
    pb._started_at = datetime.now(timezone.utc) - timedelta(seconds=pb._duration + 1)

    after = await engine.get_current_state(test_db, channel.id)

    assert after is not None
    assert after.episode.id == advertised_id
    # One selector call is still expected to reserve the item after the one
    # that has just started. There must not be a second roll for the transition.
    assert selector_calls == 1


@pytest.mark.asyncio
async def test_admin_skip_plays_the_advertised_next_item(
    test_db, test_temp_dir, monkeypatch
):
    root = test_temp_dir / "next_preview_admin_skip"
    channel = await _portable_mixed_channel(test_db, root, monkeypatch)

    engine = ChannelEngine()
    await engine.initialize(test_db)
    before = await engine.get_current_state(test_db, channel.id)
    assert before is not None
    assert before.next_episode is not None
    advertised_id = before.next_episode.id

    different = test_db.exec(
        select(MediaItem).where(
            MediaItem.channel_id == channel.id,
            MediaItem.media_type == "episode",
            MediaItem.id != advertised_id,
        )
    ).first()
    assert different is not None
    selector_calls = 0

    def disagreeing_selector(*_args, **_kwargs):
        nonlocal selector_calls
        selector_calls += 1
        return different

    monkeypatch.setattr("app.services.channel.select_next_episode", disagreeing_selector)

    await engine.skip_episode(test_db, channel.id)
    after = await engine.get_current_state(test_db, channel.id)

    assert after is not None
    assert after.episode.id == advertised_id
    assert selector_calls == 1


@pytest.mark.asyncio
async def test_transition_replaces_a_stale_disallowed_reservation(
    test_db, test_temp_dir, monkeypatch
):
    root = test_temp_dir / "next_preview_stale_transition"
    channel = await _portable_mixed_channel(test_db, root, monkeypatch)

    engine = ChannelEngine()
    await engine.initialize(test_db)
    pb = engine._channels[channel.id]
    movie = test_db.exec(
        select(MediaItem).where(
            MediaItem.channel_id == channel.id,
            MediaItem.media_type == "movie",
        )
    ).one()
    replacement = test_db.exec(
        select(MediaItem).where(
            MediaItem.channel_id == channel.id,
            MediaItem.media_type == "episode",
        )
    ).first()
    assert replacement is not None

    pb._next_episode_id = movie.id
    pb._started_at = datetime.now(timezone.utc) - timedelta(seconds=pb._duration + 1)
    selector_calls = 0

    def replacement_selector(*_args, **_kwargs):
        nonlocal selector_calls
        selector_calls += 1
        return replacement

    monkeypatch.setattr("app.services.channel.select_next_episode", replacement_selector)

    after = await engine.get_current_state(test_db, channel.id)

    assert after is not None
    assert after.episode.id == replacement.id
    assert after.episode.media_type == "episode"
    # One call replaces the invalid reservation and another reserves the item
    # after the replacement that has just started.
    assert selector_calls == 2

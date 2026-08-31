"""
Tests for scanner: parse_media_filename and scan_library.

Official hierarchy: media/<CHANNEL>/<SHOW>/<SEASON>/<EPISODE>

parse_media_filename returns:
    channel_name, show_name, season_number, episode_number, episode_title
"""
from pathlib import Path
import pytest
from app.services.scanner import parse_media_filename, scan_library
from app.models.media import MediaItem
from sqlmodel import select


def test_parse_media_filename_patterns():
    root = Path("/media/library")

    # Pattern 1: Channel/Show/Season/S01E01 — 4-level hierarchy
    f1 = root / "Canal 1" / "Bocchi the Rock" / "Season 1" / "S01E01.mkv"
    channel, title, s, ep, ep_title = parse_media_filename(f1, root)
    assert channel == "Canal 1"
    assert title == "Bocchi the Rock"
    assert s == 1
    assert ep == 1
    assert ep_title is None

    # Pattern 2: Channel/Show/Season/S02E15 + episode title
    f2 = root / "Canal 1" / "JoJo" / "Season 2" / "S02E15 - Wheel of Fortune.mp4"
    channel, title, s, ep, ep_title = parse_media_filename(f2, root)
    assert channel == "Canal 1"
    assert title == "JoJo"
    assert s == 2
    assert ep == 15
    assert ep_title == "Wheel of Fortune"

    # Pattern 3: Channel/Show/Season/bracket format
    f3 = root / "Canal 1" / "Chainsaw Man" / "Season 1" / "[05] Gun Devil.mp4"
    channel, title, s, ep, ep_title = parse_media_filename(f3, root)
    assert channel == "Canal 1"
    assert title == "Chainsaw Man"
    assert s == 1
    assert ep == 5
    assert ep_title == "Gun Devil"

    # Pattern 4: Channel/Show/Season hierarchy with dash format
    f4 = root / "Canal 2" / "Defensas" / "Season 1" / "Defensas - 03.mp4"
    channel, title, s, ep, ep_title = parse_media_filename(f4, root)
    assert channel == "Canal 2"
    assert title == "Defensas"
    assert s == 1
    assert ep == 3

    # Pattern 5: Season number from folder (no S01E01 in filename)
    f5 = root / "Canal 1" / "JoJo" / "Season 2" / "JoJo - 07.mp4"
    channel, title, s, ep, ep_title = parse_media_filename(f5, root)
    assert channel == "Canal 1"
    assert title == "JoJo"
    assert s == 2
    assert ep == 7

    # Pattern 6: 3-level (no season folder) — channel/show/episode
    f6 = root / "Canal 1" / "Frieren" / "S01E04.mkv"
    channel, title, s, ep, ep_title = parse_media_filename(f6, root)
    assert channel == "Canal 1"
    assert title == "Frieren"
    assert s == 1
    assert ep == 4


@pytest.mark.asyncio
async def test_scan_library_execution(test_db, sample_media_dir):
    result = await scan_library(test_db, sample_media_dir)

    assert result.scanned_count >= 2
    assert result.added_count >= 2
    assert result.deleted_count == 0
    assert result.total_episodes >= 2

    # Query DB — check titles
    episodes = test_db.exec(select(MediaItem)).all()
    titles = [e.media_title for e in episodes]
    assert "Bocchi the Rock" in titles
    assert "JoJo" in titles

    # Check channel_id is assigned
    for ep in episodes:
        assert ep.channel_id is not None, f"MediaItem {ep.media_title} has no channel_id"

    # Scan again -> idempotent, no changes
    result_second = await scan_library(test_db, sample_media_dir)
    assert result_second.added_count == 0
    assert result_second.updated_count == 0


@pytest.mark.asyncio
async def test_scan_assigns_correct_hierarchy(test_db, sample_media_dir):
    """Verify channel_id, media_title, season_number, episode_number are all correct."""
    from app.models.channel import Channel
    result = await scan_library(test_db, sample_media_dir)
    assert result.total_episodes >= 2

    episodes = test_db.exec(select(MediaItem)).all()

    # Every episode must belong to a channel
    for ep in episodes:
        assert ep.channel_id is not None
        channel = test_db.get(Channel, ep.channel_id)
        assert channel is not None
        assert channel.name == "Canal 1"

    bocchi = next((e for e in episodes if e.media_title == "Bocchi the Rock"), None)
    assert bocchi is not None
    assert bocchi.season_number == 1
    assert bocchi.episode_number == 1

    jojo = next((e for e in episodes if e.media_title == "JoJo"), None)
    assert jojo is not None
    assert jojo.season_number == 1
    assert jojo.episode_number == 2
    assert jojo.episode_title == "The Prophecy"

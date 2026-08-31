"""
Tests for ChannelEngine: lazy transitions, reboot recovery, concurrency, multi-channel isolation.
All tests use the multi-channel API: get_current_state(session, channel_id).
"""
import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from sqlmodel import Session, select
from app.models.media import MediaItem
from app.models.channel import Channel, ChannelState
from app.services.channel import ChannelEngine
from app.services.scanner import scan_library


def _make_channel(session: Session, name: str = "Test Channel", batch_size: int = 1) -> Channel:
    """Helper: create and persist a Channel, returning it with an ID."""
    channel = Channel(name=name, batch_size=batch_size, loop=True)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _make_episode(
    session: Session,
    channel_id: int,
    title: str,
    season: int = 1,
    ep_num: int = 1,
    duration: float = 10.0,
    play_count: int = 0,
    rel_suffix: str = "",
) -> MediaItem:
    """Helper: create and persist an MediaItem."""
    suffix = rel_suffix or f"{title.replace(' ', '_')}_S{season}E{ep_num}"
    ep = MediaItem(
        channel_id=channel_id,
        media_title=title,
        season_number=season,
        episode_number=ep_num,
        relative_path=f"Chan/{title}/{suffix}.mp4",
        file_path=f"/media/library/Chan/{title}/{suffix}.mp4",
        duration=duration,
        play_count=play_count,
    )
    session.add(ep)
    session.commit()
    session.refresh(ep)
    return ep


@pytest.mark.asyncio
async def test_two_clients_get_same_episode_and_position(client, test_db, sample_media_dir):
    """Two independent client requests receive identical episode and synchronized time."""
    await scan_library(test_db, sample_media_dir)

    # Get the first channel that was created by the scan
    from app.models.channel import Channel
    from sqlmodel import select
    channel = test_db.exec(select(Channel)).first()
    assert channel is not None, "No channel was created by scan"

    res1 = client.get(f"/api/channels/{channel.id}/now-playing")
    res2 = client.get(f"/api/channels/{channel.id}/now-playing")

    assert res1.status_code == 200, res1.text
    assert res2.status_code == 200, res2.text

    data1 = res1.json()
    data2 = res2.json()

    # Identical episode
    assert data1["episode"]["id"] == data2["episode"]["id"]
    assert data1["episode"]["media_title"] == data2["episode"]["media_title"]
    assert data1["started_at"] == data2["started_at"]
    assert data1["duration"] == data2["duration"]

    # Current time drift is minimal (< 0.1s)
    assert abs(data1["current_time"] - data2["current_time"]) < 0.1


@pytest.mark.asyncio
async def test_lazy_transition_when_episode_finishes(test_db: Session):
    """Lazy evaluation: when time exceeds duration, next request triggers transition."""
    engine = ChannelEngine()

    channel = _make_channel(test_db, "Lazy Test Channel")
    ep1 = _make_episode(test_db, channel.id, "Show A", ep_num=1, duration=10.0, rel_suffix="A_01")
    ep2 = _make_episode(test_db, channel.id, "Show B", ep_num=1, duration=15.0, rel_suffix="B_01")

    # Initialize engine
    await engine.initialize(test_db)
    state1 = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert state1 is not None
    initial_ep_id = state1.episode.id

    # Simulate that started_at happened 20 seconds ago (> 10s duration)
    pb = engine._channels[channel.id]  # type: ignore
    pb._started_at = datetime.now(timezone.utc) - timedelta(seconds=20)

    # Next call should lazily advance to next episode
    state2 = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert state2 is not None
    assert state2.episode.id != initial_ep_id
    assert state2.current_time < 2.0  # Reset offset in new episode


@pytest.mark.asyncio
async def test_reboot_recovery_active_broadcast(test_db: Session):
    """If server restarts while episode is mid-broadcast, resume at exact elapsed offset."""
    channel = _make_channel(test_db, "Recovery Active Channel")
    ep = _make_episode(
        test_db, channel.id, "Steins Gate", duration=100.0, play_count=1,  # type: ignore
        rel_suffix="SG_01"
    )

    # Create persisted state started 40s ago
    past_started = datetime.now(timezone.utc) - timedelta(seconds=40)
    state = ChannelState(
        channel_id=channel.id,  # type: ignore
        current_episode_id=ep.id,  # type: ignore
        next_episode_id=ep.id,  # type: ignore
        started_at=past_started,
        duration=100.0,
        updated_at=past_started,
    )
    test_db.add(state)
    test_db.commit()

    # New engine instance simulating server restart
    new_engine = ChannelEngine()
    await new_engine.initialize(test_db)

    result = await new_engine.get_current_state(test_db, channel.id)  # type: ignore
    assert result is not None
    assert result.episode.id == ep.id
    # Position should be approx 40s
    assert 39.0 <= result.current_time <= 43.0


@pytest.mark.asyncio
async def test_reboot_recovery_expired_broadcast(test_db: Session):
    """If server restarts after broadcast already expired, start a fresh broadcast immediately."""
    channel = _make_channel(test_db, "Recovery Expired Channel")
    ep = _make_episode(
        test_db, channel.id, "Durarara", duration=30.0, play_count=1,  # type: ignore
        rel_suffix="DRR_01"
    )

    # Persisted state started 5 hours ago (well past 30s duration)
    ancient_started = datetime.now(timezone.utc) - timedelta(hours=5)
    state = ChannelState(
        channel_id=channel.id,  # type: ignore
        current_episode_id=ep.id,  # type: ignore
        next_episode_id=ep.id,  # type: ignore
        started_at=ancient_started,
        duration=30.0,
        updated_at=ancient_started,
    )
    test_db.add(state)
    test_db.commit()

    new_engine = ChannelEngine()
    await new_engine.initialize(test_db)

    result = await new_engine.get_current_state(test_db, channel.id)  # type: ignore
    assert result is not None
    # Position should be fresh start (< 2s)
    assert result.current_time < 2.0


@pytest.mark.asyncio
async def test_concurrency_during_transition(test_db: Session):
    """Concurrent calls during transition do not cause race conditions or duplicate advances."""
    engine = ChannelEngine()

    channel = _make_channel(test_db, "Concurrency Test Channel")
    ep1 = _make_episode(test_db, channel.id, "Show One", ep_num=1, duration=10.0, rel_suffix="One_01")  # type: ignore
    ep2 = _make_episode(test_db, channel.id, "Show Two", ep_num=1, duration=10.0, rel_suffix="Two_01")  # type: ignore

    await engine.initialize(test_db)

    # Force expiration
    pb = engine._channels[channel.id]  # type: ignore
    pb._started_at = datetime.now(timezone.utc) - timedelta(seconds=15)

    # Launch 20 concurrent requests
    tasks = [engine.get_current_state(test_db, channel.id) for _ in range(20)]  # type: ignore
    results = await asyncio.gather(*tasks)

    # All 20 requests must return the exact same episode ID
    episode_ids = [r.episode.id for r in results if r is not None]
    assert len(episode_ids) == 20
    assert len(set(episode_ids)) == 1


@pytest.mark.asyncio
async def test_channel_with_empty_library(test_db: Session):
    """Empty library: get_current_state returns None gracefully."""
    channel = _make_channel(test_db, "Empty Channel")
    engine = ChannelEngine()
    await engine.initialize(test_db)
    res = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert res is None


@pytest.mark.asyncio
async def test_channel_isolation(test_db: Session):
    """Two channels must have fully independent broadcast states."""
    engine = ChannelEngine()

    ch1 = _make_channel(test_db, "Channel Isolation 1")
    ch2 = _make_channel(test_db, "Channel Isolation 2")

    ep1 = _make_episode(test_db, ch1.id, "Show Alpha", rel_suffix="Alpha_01", duration=300.0)  # type: ignore
    ep2 = _make_episode(test_db, ch2.id, "Show Beta", rel_suffix="Beta_01", duration=300.0)  # type: ignore

    await engine.initialize(test_db)

    state1 = await engine.get_current_state(test_db, ch1.id)  # type: ignore
    state2 = await engine.get_current_state(test_db, ch2.id)  # type: ignore

    assert state1 is not None
    assert state2 is not None

    # Different episodes, different channels
    assert state1.episode.id == ep1.id
    assert state2.episode.id == ep2.id
    assert state1.episode.id != state2.episode.id

    # Skip channel 1 — channel 2 must not be affected
    await engine.skip_episode(test_db, ch1.id)  # type: ignore

    state1_after = await engine.get_current_state(test_db, ch1.id)  # type: ignore
    state2_after = await engine.get_current_state(test_db, ch2.id)  # type: ignore

    # Channel 2 unchanged
    assert state2_after is not None
    assert state2_after.episode.id == ep2.id
    assert state2_after.started_at == state2.started_at

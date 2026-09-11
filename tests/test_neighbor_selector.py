import pytest
from unittest.mock import MagicMock, patch

from app.models.channel import Channel, ChannelState
from app.models.media import MediaItem
from app.services.channel import channel_engine
from app.services.mp4_inspector import MP4HeaderInfo


@pytest.mark.asyncio
async def test_neighbor_states_batched_query(test_db):
    # Setup test channels and states
    channel1 = Channel(name="Channel 1", folder_name="ch1", display_order=1)
    channel2 = Channel(name="Channel 2", folder_name="ch2", display_order=2)
    channel3 = Channel(name="Channel 3", folder_name="ch3", display_order=3)
    test_db.add(channel1)
    test_db.add(channel2)
    test_db.add(channel3)
    test_db.commit()

    ep1 = MediaItem(channel_id=channel1.id, relative_path="ep1.mp4", media_title="Ep 1", episode_number=1, file_path="/tmp/ep1.mp4", duration=100.0)
    ep2 = MediaItem(channel_id=channel2.id, relative_path="ep2.mp4", media_title="Ep 2", episode_number=1, file_path="/tmp/ep2.mp4", duration=200.0)
    ep3 = MediaItem(channel_id=channel3.id, relative_path="ep3.mp4", media_title="Ep 3", episode_number=1, file_path="/tmp/ep3.mp4", duration=300.0)
    test_db.add(ep1)
    test_db.add(ep2)
    test_db.add(ep3)
    test_db.commit()

    state1 = ChannelState(channel_id=channel1.id, current_episode_id=ep1.id, duration=100.0)
    state2 = ChannelState(channel_id=channel2.id, current_episode_id=ep2.id, duration=200.0)
    state3 = ChannelState(channel_id=channel3.id, current_episode_id=ep3.id, duration=300.0)
    test_db.add(state1)
    test_db.add(state2)
    test_db.add(state3)
    test_db.commit()

    await channel_engine.initialize(test_db)
    states = await channel_engine.get_neighbor_states_batched(test_db, current_channel_id=channel1.id)

    # Should return states for neighbor channels (channel2, channel3)
    assert len(states) >= 1
    neighbor_names = [s.channel_name for s in states]
    assert "Channel 2" in neighbor_names or "Channel 3" in neighbor_names


@pytest.mark.asyncio
async def test_warm_best_neighbor_candidate_selection(test_db, tmp_path):
    # Create mock media files for candidates
    file1 = tmp_path / "small.mp4"
    file2 = tmp_path / "large_moov_end.mp4"
    file1.write_bytes(b"\x00" * 1000)
    file2.write_bytes(b"\x00" * 5000)

    channel1 = Channel(name="Ch 1", folder_name="ch1", display_order=1)
    channel2 = Channel(name="Ch 2", folder_name="ch2", display_order=2)
    test_db.add(channel1)
    test_db.add(channel2)
    test_db.commit()

    ep1 = MediaItem(channel_id=channel1.id, relative_path=str(file1.name), media_title="Ep 1", episode_number=1, file_path=str(file1), duration=100.0)
    ep2 = MediaItem(channel_id=channel2.id, relative_path=str(file2.name), media_title="Ep 2", episode_number=1, file_path=str(file2), duration=300.0)
    test_db.add(ep1)
    test_db.add(ep2)
    test_db.commit()

    state1 = ChannelState(channel_id=channel1.id, current_episode_id=ep1.id, duration=100.0)
    state2 = ChannelState(channel_id=channel2.id, current_episode_id=ep2.id, duration=300.0)
    test_db.add(state1)
    test_db.add(state2)
    test_db.commit()

    await channel_engine.initialize(test_db)

    with patch("app.services.channel.inspect_mp4", return_value=MP4HeaderInfo(is_mp4=True, moov_at_end=True)):
        with patch("app.services.cache_warmer.cache_warmer.warm_file_cache_async", return_value=True) as mock_warm:
            selected_path = await channel_engine.warm_best_neighbor_candidate(test_db, current_channel_id=channel1.id)
            assert selected_path is not None
            mock_warm.assert_called_once()

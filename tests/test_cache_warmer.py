import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.cache_warmer import CacheWarmer, WarmingRange, cache_warmer
from app.services.mp4_inspector import MP4HeaderInfo


def test_calculate_warming_ranges_max_cap(tmp_path):
    warmer = CacheWarmer()
    dummy_file = tmp_path / "video.mp4"
    dummy_file.write_bytes(b"\x00" * 10_000_000)  # 10 MB dummy file

    header_info = MP4HeaderInfo(
        is_mp4=True,
        moov_offset=5_000_000,
        moov_size=1_000_000,
        moov_at_end=False,
        file_size=10_000_000,
    )

    ranges = warmer.calculate_warming_ranges(
        file_path=dummy_file,
        file_size=10_000_000,
        estimated_offset_bytes=2_000_000,
        header_info=header_info,
    )

    total_bytes = sum(r.length for r in ranges)
    assert total_bytes <= 3 * 1024 * 1024  # Enforce <= 3 MB max
    assert len(ranges) >= 1
    assert ranges[0].offset == 0
    assert ranges[0].length == 256 * 1024  # Head ~256 KB


def test_ttl_deduplication(tmp_path):
    warmer = CacheWarmer()
    dummy_file = tmp_path / "video.mp4"
    dummy_file.write_bytes(b"\x00" * 1000)

    assert not warmer.is_warmed_recently(dummy_file)
    warmer.mark_warmed(dummy_file)
    assert warmer.is_warmed_recently(dummy_file)


@pytest.mark.asyncio
async def test_single_task_lock_and_cancellation(tmp_path):
    warmer = CacheWarmer()
    file1 = tmp_path / "file1.mp4"
    file2 = tmp_path / "file2.mp4"
    file1.write_bytes(b"\x00" * 1000)
    file2.write_bytes(b"\x00" * 1000)

    async def mock_warming(f, o):
        await asyncio.sleep(10)

    with patch("app.services.cache_warmer.validate_file_safety", side_effect=lambda p: p):
        with patch.object(warmer, "_execute_warming_job", side_effect=mock_warming):
            res1 = await warmer.warm_file_cache_async(file1)
            assert res1 is True
            task1 = warmer._current_task

            res2 = await warmer.warm_file_cache_async(file2)
            assert res2 is True
            task2 = warmer._current_task

            assert task1 != task2
            # Allow event loop step for cancellation to register
            await asyncio.sleep(0)
            assert task1.cancelled() or task1.done() or (hasattr(task1, "cancelling") and task1.cancelling() > 0)
            warmer.cancel_current_warming()

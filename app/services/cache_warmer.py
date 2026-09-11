from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None


from app.core.config import settings
from app.services.mp4_inspector import MP4HeaderInfo, inspect_mp4
from app.services.streaming import validate_file_safety

logger = logging.getLogger(__name__)

POSIX_FADV_WILLNEED = getattr(os, "POSIX_FADV_WILLNEED", 3)
POSIX_FADVISE_AVAILABLE = hasattr(os, "posix_fadvise")

# Safety & Limit Constants
MAX_TOTAL_WARM_BYTES = 3 * 1024 * 1024  # 3 MB max per job
HEAD_WARM_BYTES = 256 * 1024  # 256 KB
PLAYBACK_WARM_BYTES = 512 * 1024  # 512 KB
DEFAULT_TRAILING_WARM_BYTES = 1536 * 1024  # 1.5 MB
TTL_SECONDS = 30.0  # 30 seconds deduplication TTL
MAX_MEMORY_USAGE_PERCENT = 90.0  # Memory pressure limit


@dataclass
class WarmingRange:
    offset: int
    length: int


class CacheWarmer:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._ttl_cache: Dict[str, float] = {}
        self._current_task: Optional[asyncio.Task] = None

    def reset_for_tests(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        self._current_task = None
        self._ttl_cache.clear()

    def is_warmed_recently(self, file_path: str | Path) -> bool:
        path_str = str(Path(file_path).resolve())
        now = time.monotonic()
        timestamp = self._ttl_cache.get(path_str)
        if timestamp is not None and (now - timestamp) < TTL_SECONDS:
            return True
        return False

    def mark_warmed(self, file_path: str | Path) -> None:
        path_str = str(Path(file_path).resolve())
        now = time.monotonic()
        self._ttl_cache[path_str] = now
        # Clean up stale TTL cache entries periodically
        if len(self._ttl_cache) > 100:
            stale = [k for k, v in self._ttl_cache.items() if (now - v) >= TTL_SECONDS]
            for k in stale:
                self._ttl_cache.pop(k, None)

    def cancel_current_warming(self) -> None:
        """Cancel any in-flight background warming job immediately."""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            self._current_task = None

    def _is_server_busy_or_memory_high(self) -> bool:
        # Check memory pressure
        if psutil is not None:
            try:
                mem = psutil.virtual_memory()
                if mem.percent > MAX_MEMORY_USAGE_PERCENT:
                    logger.info("Skipping cache warming: High server memory pressure (%.1f%%)", mem.percent)
                    return True
            except Exception:
                pass


        # Check FFmpeg optimization activity
        try:
            from app.services.optimization import optimization_manager
            if optimization_manager.get_active_job() is not None:
                logger.info("Skipping cache warming: FFmpeg optimization job active")
                return True
        except Exception:
            pass

        return False

    def calculate_warming_ranges(
        self,
        file_path: Path,
        file_size: int,
        estimated_offset_bytes: int = 0,
        header_info: Optional[MP4HeaderInfo] = None,
    ) -> List[WarmingRange]:
        """
        Calculate precise byte ranges to warm (head, moov index, playback target),
        enforcing the strict 2-3 MB total cap per warming job.
        """
        if file_size <= 0:
            return []

        ranges: List[WarmingRange] = []
        budget = MAX_TOTAL_WARM_BYTES

        # 1. File Head (~256 KB)
        head_len = min(HEAD_WARM_BYTES, file_size, budget)
        if head_len > 0:
            ranges.append(WarmingRange(offset=0, length=head_len))
            budget -= head_len

        if budget <= 0:
            return ranges

        # 2. Index / moov Range
        info = header_info or inspect_mp4(file_path)

        if info.has_moov and info.moov_offset is not None and info.moov_size is not None:
            moov_off = info.moov_offset
            moov_len = min(info.moov_size, budget)
            if moov_off + moov_len <= file_size and moov_len > 0:
                # Avoid duplicate range if moov is within file head
                if moov_off >= head_len:
                    ranges.append(WarmingRange(offset=moov_off, length=moov_len))
                    budget -= moov_len
        elif info.moov_at_end or info.is_mp4:
            # Trailing range for moov at end (~1.5 MB max)
            trail_len = min(DEFAULT_TRAILING_WARM_BYTES, budget, max(0, file_size - head_len))
            trail_off = max(head_len, file_size - trail_len)
            if trail_len > 0 and trail_off < file_size:
                ranges.append(WarmingRange(offset=trail_off, length=trail_len))
                budget -= trail_len

        if budget <= 0:
            return ranges

        # 3. Playback Target Range (~512 KB around estimated position)
        if estimated_offset_bytes > 0 and estimated_offset_bytes < file_size:
            play_off = max(0, estimated_offset_bytes)
            play_len = min(PLAYBACK_WARM_BYTES, budget, file_size - play_off)
            if play_len > 0:
                # Check if overlap with existing range
                overlapping = False
                for r in ranges:
                    if r.offset <= play_off < (r.offset + r.length):
                        overlapping = True
                        break
                if not overlapping:
                    ranges.append(WarmingRange(offset=play_off, length=play_len))

        return ranges

    async def warm_file_cache_async(
        self,
        file_path: str | Path,
        estimated_offset_bytes: int = 0,
    ) -> bool:
        """
        Request background warming for a file.
        Enforces single-worker lock, 30s TTL deduplication, and execution conditions.
        """
        path_obj = Path(file_path)

        # 1. Validate file safety and containment
        try:
            safe_path = validate_file_safety(path_obj)
        except Exception as exc:
            logger.warning("Cache warmer security check failed for %s: %s", file_path, exc)
            return False

        # 2. Check TTL deduplication
        if self.is_warmed_recently(safe_path):
            logger.debug("Skipping cache warming for %s: Warmed within last 30s", safe_path.name)
            return False

        # 3. Check execution conditions
        if self._is_server_busy_or_memory_high():
            return False

        # Cancel any previous warming job to ensure strict single-worker queue
        self.cancel_current_warming()

        # Launch background warming task
        self._current_task = asyncio.create_task(
            self._execute_warming_job(safe_path, estimated_offset_bytes)
        )
        return True

    async def _execute_warming_job(self, file_path: Path, estimated_offset_bytes: int) -> None:
        async with self._lock:
            try:
                st = file_path.stat()
                file_size = st.st_size
                header_info = inspect_mp4(file_path)

                ranges = self.calculate_warming_ranges(
                    file_path=file_path,
                    file_size=file_size,
                    estimated_offset_bytes=estimated_offset_bytes,
                    header_info=header_info,
                )

                if not ranges:
                    return

                # Execute posix_fadvise offloaded to thread pool to avoid blocking async loop
                await asyncio.to_thread(self._advise_kernel_ranges, file_path, ranges)
                self.mark_warmed(file_path)
                logger.info(
                    "Warmed page cache for %s (%d ranges, total ~%.2f MB)",
                    file_path.name,
                    len(ranges),
                    sum(r.length for r in ranges) / (1024 * 1024),
                )
            except asyncio.CancelledError:
                logger.debug("Warming task for %s was cancelled", file_path.name)
                raise
            except Exception as exc:
                logger.warning("Error warming page cache for %s: %s", file_path.name, exc)

    def _advise_kernel_ranges(self, file_path: Path, ranges: List[WarmingRange]) -> None:
        if not POSIX_FADVISE_AVAILABLE:
            return

        try:
            with open(file_path, "rb") as f:
                fd = f.fileno()
                for r in ranges:
                    os.posix_fadvise(fd, r.offset, r.length, POSIX_FADV_WILLNEED)
        except OSError as exc:
            logger.warning("posix_fadvise failed for %s: %s", file_path.name, exc)


cache_warmer = CacheWarmer()

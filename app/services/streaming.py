import re
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional, Tuple
import aiofiles
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.media import MediaItem

RANGE_HEADER_REGEX = re.compile(r"^bytes=(\d*)-(\d*)$")


def validate_file_safety(file_path: str | Path) -> Path:
    """
    Validate that the file exists and resides within the allowed media directory
    to prevent path traversal attacks.
    """
    path_obj = Path(file_path).resolve()
    media_root = settings.resolved_media_dir.resolve()

    if not path_obj.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media file not found on disk."
        )

    # Resolve first so ``..`` and symlink escapes cannot bypass containment.
    if not path_obj.is_relative_to(media_root):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: File outside authorized media directory."
        )

    return path_obj


def parse_range_header(range_header: Optional[str], file_size: int) -> Optional[Tuple[int, int]]:
    """
    Parse HTTP Range header value (e.g. 'bytes=0-1023' or 'bytes=1024-').
    Returns (start, end) tuple or None if no valid range requested.
    Raises HTTPException 416 if requested range is out of bounds.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    if len(range_header) > 100:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    match = RANGE_HEADER_REGEX.fullmatch(range_header.strip())
    if not match:
        return None

    start_str, end_str = match.groups()

    if not start_str and not end_str:
        return None

    if start_str and end_str:
        start = int(start_str)
        end = int(end_str)
    elif start_str:
        start = int(start_str)
        end = file_size - 1
    else:  # suffix range: bytes=-500
        suffix_length = int(end_str)
        if suffix_length == 0:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start = max(0, file_size - suffix_length)
        end = file_size - 1

    if start < 0 or start >= file_size or start > end:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    end = min(end, file_size - 1)
    return start, end


import asyncio
import time
from app.core.telemetry import telemetry_tracker

async def file_chunk_generator(
    file_path: Path,
    start: int,
    end: int,
    chunk_size: int = settings.STREAM_CHUNK_SIZE,
    access_check: Optional[Callable[[], bool]] = None,
    access_check_interval_bytes: int = 8 * 1024 * 1024,
    start_time: Optional[float] = None,
    range_header: Optional[str] = None,
) -> AsyncGenerator[bytes, None]:
    """Yield a byte range and periodically stop if authorization is revoked.

    Authorization is already checked before headers are sent. Rechecking during
    long transfers closes the remaining body if a user/session/channel loses
    access while the video request is still active.
    """
    bytes_remaining = end - start + 1
    bytes_since_access_check = access_check_interval_bytes
    block_index = 0
    first_byte = True

    async with aiofiles.open(file_path, mode="rb") as f:
        await f.seek(start)
        while bytes_remaining > 0:
            if access_check is not None and bytes_since_access_check >= access_check_interval_bytes:
                # Offload synchronous SQLite access check out of main event loop
                allowed = await asyncio.to_thread(access_check)
                if not allowed:
                    break
                bytes_since_access_check = 0

            # Smaller initial reads for every Range request, without changing
            # response headers, byte coverage or authorization frequency.
            initial_limit = (64 * 1024, 256 * 1024)
            read_size = min(chunk_size, bytes_remaining,
                            initial_limit[block_index] if block_index < 2 else chunk_size)
            chunk = await f.read(read_size)
            if not chunk:
                break

            if first_byte and start_time is not None:
                ttfb_ms = (time.monotonic() - start_time) * 1000.0
                telemetry_tracker.record_range_request(
                    path=str(file_path),
                    range_header=range_header,
                    ttfb_ms=ttfb_ms,
                    status_code=206 if range_header else 200,
                )
                first_byte = False

            bytes_remaining -= len(chunk)
            bytes_since_access_check += len(chunk)
            block_index += 1
            yield chunk


def create_media_stream_response(
    episode: MediaItem,
    range_header: Optional[str] = None,
    access_check: Optional[Callable[[], bool]] = None,
) -> StreamingResponse:
    """
    Build a StreamingResponse with HTTP 206 Partial Content or 200 OK.
    """
    start_time = time.monotonic()
    file_path = validate_file_safety(episode.file_path)
    file_size = file_path.stat().st_size
    mime_type = episode.mime_type or "video/mp4"

    range_bounds = parse_range_header(range_header, file_size)

    if range_bounds is not None:
        start, end = range_bounds
        content_length = end - start + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": mime_type,
            "Cache-Control": "private, no-store",
        }
        return StreamingResponse(
            file_chunk_generator(
                file_path, start, end, access_check=access_check, start_time=start_time, range_header=range_header
            ),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
            media_type=mime_type,
        )

    # Full file response
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Content-Type": mime_type,
        "Cache-Control": "private, no-store",
    }
    return StreamingResponse(
        file_chunk_generator(
            file_path, 0, file_size - 1, access_check=access_check, start_time=start_time, range_header=None
        ),
        status_code=status.HTTP_200_OK,
        headers=headers,
        media_type=mime_type,
    )


import os
import re
from pathlib import Path
from typing import AsyncGenerator, Optional, Tuple
import aiofiles
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.media import MediaItem

RANGE_HEADER_REGEX = re.compile(r"bytes=(\d*)-(\d*)")


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

    # Check path containment
    try:
        common = os.path.commonpath([str(path_obj), str(media_root)])
        if common != str(media_root):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: File outside authorized media directory."
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Invalid file path."
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

    match = RANGE_HEADER_REGEX.match(range_header.strip())
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


async def file_chunk_generator(
    file_path: Path,
    start: int,
    end: int,
    chunk_size: int = settings.STREAM_CHUNK_SIZE,
) -> AsyncGenerator[bytes, None]:
    """
    Async generator yielding file chunks between start and end byte positions.
    """
    bytes_remaining = end - start + 1
    async with aiofiles.open(file_path, mode="rb") as f:
        await f.seek(start)
        while bytes_remaining > 0:
            read_size = min(chunk_size, bytes_remaining)
            chunk = await f.read(read_size)
            if not chunk:
                break
            bytes_remaining -= len(chunk)
            yield chunk


def create_media_stream_response(
    episode: MediaItem,
    range_header: Optional[str] = None,
) -> StreamingResponse:
    """
    Build a StreamingResponse with HTTP 206 Partial Content or 200 OK.
    """
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
            "Cache-Control": "no-cache",
        }
        return StreamingResponse(
            file_chunk_generator(file_path, start, end),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
            media_type=mime_type,
        )

    # Full file response
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Content-Type": mime_type,
    }
    return StreamingResponse(
        file_chunk_generator(file_path, 0, file_size - 1),
        status_code=status.HTTP_200_OK,
        headers=headers,
        media_type=mime_type,
    )

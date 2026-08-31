import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)


class MediaMetadata(TypedDict):
    duration: float
    video_codec: Optional[str]
    audio_codec: Optional[str]
    mime_type: str


EXTENSION_MIME_MAP = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
}


def get_mime_type(file_path: Path | str) -> str:
    """Determine video MIME type based on extension with reliable defaults."""
    suffix = Path(file_path).suffix.lower()
    if suffix in EXTENSION_MIME_MAP:
        return EXTENSION_MIME_MAP[suffix]
    guessed, _ = mimetypes.guess_type(str(file_path))
    return guessed or "video/mp4"


async def extract_metadata(file_path: Path | str) -> MediaMetadata:
    """
    Extract media metadata (duration, codecs) using ffprobe.
    Falls back gracefully if ffprobe fails or is unavailable.
    """
    path_obj = Path(file_path)
    mime_type = get_mime_type(path_obj)

    metadata: MediaMetadata = {
        "duration": 0.0,
        "video_codec": None,
        "audio_codec": None,
        "mime_type": mime_type,
    }

    if not path_obj.is_file():
        return metadata

    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name",
            "-of", "json",
            str(path_obj.resolve()),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="replace"))

            # Duration from format
            format_info = data.get("format", {})
            if "duration" in format_info:
                try:
                    metadata["duration"] = max(0.0, float(format_info["duration"]))
                except (ValueError, TypeError):
                    pass

            # Codecs from streams
            for stream in data.get("streams", []):
                codec_type = stream.get("codec_type")
                codec_name = stream.get("codec_name")
                if codec_type == "video" and not metadata["video_codec"]:
                    metadata["video_codec"] = codec_name
                elif codec_type == "audio" and not metadata["audio_codec"]:
                    metadata["audio_codec"] = codec_name

    except Exception as exc:
        logger.warning(f"ffprobe extraction failed for {path_obj}: {exc}")

    # Fallback to mutagen if duration is still 0
    if metadata["duration"] == 0.0:
        try:
            import mutagen
            media = mutagen.File(str(path_obj))
            if media and media.info and hasattr(media.info, "length"):
                metadata["duration"] = max(0.0, float(media.info.length))
        except Exception:
            pass

    return metadata

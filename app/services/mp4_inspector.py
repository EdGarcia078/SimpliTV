from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

CACHE_MAX_SIZE = 256


@dataclass(frozen=True)
class MP4HeaderInfo:
    is_mp4: bool
    moov_offset: Optional[int] = None
    moov_size: Optional[int] = None
    moov_at_end: bool = False
    file_size: int = 0

    @property
    def has_moov(self) -> bool:
        return self.moov_offset is not None and self.moov_size is not None


_INSPECTOR_CACHE: Dict[Tuple[str, int, float], MP4HeaderInfo] = {}


def inspect_mp4(file_path: str | Path) -> MP4HeaderInfo:
    """
    Lightweight MP4 atom scanner.
    Parses top-level container atoms (ftyp, moov, mdat) by reading minimal header bytes.
    Returns exact moov byte range and whether moov is at start or end of file.
    Caches results by (filepath, size, mtime).
    """
    path_obj = Path(file_path).resolve()
    if not path_obj.is_file():
        return MP4HeaderInfo(is_mp4=False)

    try:
        st = path_obj.stat()
        cache_key = (str(path_obj), st.st_size, st.st_mtime)
    except OSError:
        return MP4HeaderInfo(is_mp4=False)

    if cache_key in _INSPECTOR_CACHE:
        return _INSPECTOR_CACHE[cache_key]

    info = _parse_mp4_atoms(path_obj, st.st_size)

    # Manage cache size
    if len(_INSPECTOR_CACHE) >= CACHE_MAX_SIZE:
        # Simple FIFO eviction of oldest entry
        first_key = next(iter(_INSPECTOR_CACHE))
        _INSPECTOR_CACHE.pop(first_key, None)

    _INSPECTOR_CACHE[cache_key] = info
    return info


def clear_mp4_inspector_cache() -> None:
    """Clear the in-memory MP4 inspector cache."""
    _INSPECTOR_CACHE.clear()


def _parse_mp4_atoms(path: Path, file_size: int) -> MP4HeaderInfo:
    if file_size < 8:
        return MP4HeaderInfo(is_mp4=False, file_size=file_size)

    is_mp4 = False
    moov_offset: Optional[int] = None
    moov_size: Optional[int] = None
    mdat_offset: Optional[int] = None

    try:
        with open(path, "rb") as f:
            offset = 0
            while offset < file_size:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    break

                atom_size, atom_type_bytes = struct.unpack(">I4s", header)
                try:
                    atom_type = atom_type_bytes.decode("ascii", errors="replace")
                except Exception:
                    atom_type = ""

                header_size = 8

                if atom_size == 1:
                    extra = f.read(8)
                    if len(extra) < 8:
                        break
                    large_size, = struct.unpack(">Q", extra)
                    atom_size = large_size
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - offset

                if atom_size < header_size:
                    break

                if atom_type == "ftyp":
                    is_mp4 = True
                elif atom_type == "moov":
                    is_mp4 = True
                    moov_offset = offset
                    moov_size = atom_size
                elif atom_type == "mdat":
                    mdat_offset = offset

                offset += atom_size

    except Exception:
        return MP4HeaderInfo(is_mp4=False, file_size=file_size)

    if not is_mp4 and not moov_offset:
        # Fallback check for file extension if atoms were unusual
        if path.suffix.lower() in (".mp4", ".m4v", ".mov"):
            is_mp4 = True

    moov_at_end = False
    if moov_offset is not None:
        if mdat_offset is not None and moov_offset > mdat_offset:
            moov_at_end = True
        elif moov_offset > (file_size / 2):
            moov_at_end = True

    return MP4HeaderInfo(
        is_mp4=is_mp4,
        moov_offset=moov_offset,
        moov_size=moov_size,
        moov_at_end=moov_at_end,
        file_size=file_size,
    )

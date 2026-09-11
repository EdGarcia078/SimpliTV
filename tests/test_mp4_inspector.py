import struct
import tempfile
from pathlib import Path

import pytest

from app.services.mp4_inspector import (
    MP4HeaderInfo,
    clear_mp4_inspector_cache,
    inspect_mp4,
)


def _create_mock_mp4(tmp_path: Path, moov_at_end: bool = False) -> Path:
    file_path = tmp_path / ("test_end.mp4" if moov_at_end else "test_start.mp4")

    # ftyp atom (20 bytes)
    ftyp_data = struct.pack(">I4s", 20, b"ftyp") + b"isom" + b"\x00\x00\x02\x00" + b"isom"
    # moov atom (30 bytes)
    moov_data = struct.pack(">I4s", 30, b"moov") + b"x" * 22
    # mdat atom (500 bytes)
    mdat_data = struct.pack(">I4s", 500, b"mdat") + b"\x00" * 492

    with open(file_path, "wb") as f:
        f.write(ftyp_data)
        if moov_at_end:
            f.write(mdat_data)
            f.write(moov_data)
        else:
            f.write(moov_data)
            f.write(mdat_data)

    return file_path


def test_inspect_mp4_moov_at_start(tmp_path):
    clear_mp4_inspector_cache()
    file_path = _create_mock_mp4(tmp_path, moov_at_end=False)
    info = inspect_mp4(file_path)

    assert info.is_mp4 is True
    assert info.moov_offset is not None
    assert info.moov_size == 30
    assert info.moov_offset == 20  # right after 20-byte ftyp
    assert info.moov_at_end is False


def test_inspect_mp4_moov_at_end(tmp_path):
    clear_mp4_inspector_cache()
    file_path = _create_mock_mp4(tmp_path, moov_at_end=True)
    info = inspect_mp4(file_path)

    assert info.is_mp4 is True
    assert info.moov_offset is not None
    assert info.moov_size == 30
    assert info.moov_offset == 20 + 500  # after ftyp and mdat
    assert info.moov_at_end is True


def test_mp4_inspector_lru_cache(tmp_path):
    clear_mp4_inspector_cache()
    file_path = _create_mock_mp4(tmp_path, moov_at_end=False)

    info1 = inspect_mp4(file_path)
    info2 = inspect_mp4(file_path)
    assert info1 is info2


def test_mp4_inspector_invalid_file(tmp_path):
    clear_mp4_inspector_cache()
    file_path = tmp_path / "invalid.txt"
    file_path.write_bytes(b"hello world")

    info = inspect_mp4(file_path)
    assert info.is_mp4 is False

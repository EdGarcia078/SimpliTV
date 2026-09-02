from pathlib import Path

from app.services.normalization import (
    MediaStreams,
    NormalizationManager,
    classify_media,
    is_normalization_temp_file,
)


def make_media(**overrides):
    values = dict(
        path="/library/Canal/Show/Season 1/episode.mkv",
        relative_path="Canal/Show/Season 1/episode.mkv",
        size=500_000_000,
        duration=1440.0,
        container=".mkv",
        video_codec="h264",
        audio_codecs=["aac"],
        subtitle_codecs=[],
        unsupported_subtitle_codecs=[],
    )
    values.update(overrides)
    return MediaStreams(**values)


def test_h264_aac_mkv_uses_lossless_remux():
    item = classify_media(make_media())
    assert item.status == "convert"
    assert item.strategy == "remux"


def test_incompatible_video_requires_transcode():
    item = classify_media(make_media(video_codec="hevc"))
    assert item.status == "convert"
    assert item.strategy == "transcode"
    assert "H.264" in item.reason


def test_text_subtitles_are_discarded_without_video_transcode():
    item = classify_media(make_media(subtitle_codecs=["ass"]))
    assert item.status == "convert"
    assert item.strategy == "remux"
    assert "subtítulos descartados" in item.reason


def test_bitmap_subtitles_do_not_block_mkv_conversion():
    item = classify_media(make_media(
        subtitle_codecs=["hdmv_pgs_subtitle"],
        unsupported_subtitle_codecs=["hdmv_pgs_subtitle"],
    ))
    assert item.status == "convert"
    assert item.strategy == "remux"
    assert "subtítulos descartados" in item.reason.lower()


def test_any_existing_mp4_is_ready_even_if_codecs_are_not_canonical():
    item = classify_media(make_media(
        container=".mp4",
        path="/library/Canal/Show/Season 1/episode.mp4",
        relative_path="Canal/Show/Season 1/episode.mp4",
        video_codec="hevc",
        audio_codecs=["ac3"],
        subtitle_codecs=["ass"],
    ))
    assert item.status == "ready"
    assert item.strategy == "none"


def test_playing_file_is_protected():
    item = classify_media(make_media(), protected=True)
    assert item.status == "protected"


def test_conversion_temp_names_are_recognized():
    assert is_normalization_temp_file(Path("episode.converting.mp4"))
    assert not is_normalization_temp_file(Path("episode.mp4"))


def test_media_discovery_ignores_conversion_and_optimization_temps(tmp_path):
    (tmp_path / "episode.mkv").write_bytes(b"video")
    (tmp_path / "episode.converting.mp4").write_bytes(b"temp")
    (tmp_path / "episode.optimizing.mp4").write_bytes(b"temp")
    manager = NormalizationManager()
    assert manager._media_files(tmp_path) == [tmp_path / "episode.mkv"]

from pathlib import Path

from app.services.optimization import (
    AnalysisItem,
    OptimizationManager,
    ProbeResult,
    classify_probe,
    is_optimization_temp_file,
)


def make_probe(**overrides):
    values = dict(
        path="/library/Channel/Show/Season 1/episode.mkv",
        relative_path="Channel/Show/Season 1/episode.mkv",
        size=500_000_000,
        duration=1440.0,
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        fps=23.976,
        bitrate=8_000_000,
        video_bitrate=7_800_000,
        audio_bitrate=160_000,
        streams=2,
        container=".mkv",
    )
    values.update(overrides)
    return ProbeResult(**values)


def test_high_bitrate_h264_is_candidate():
    # Keep size/duration consistent with the declared high bitrate so the
    # estimated savings calculation sees a genuinely oversized source.
    item = classify_probe(make_probe(size=1_500_000_000))
    assert item.status == "optimize"
    assert item.estimated_savings > 0


def test_reasonable_hevc_is_left_untouched():
    item = classify_probe(make_probe(video_codec="hevc", bitrate=3_500_000))
    assert item.status == "ok"


def test_720p_is_never_marked_for_upscale():
    item = classify_probe(make_probe(
        video_codec="hevc", width=1280, height=720,
        bitrate=1_700_000, size=300_000_000,
    ))
    assert item.status == "ok"


def test_playing_file_is_protected_even_if_inefficient():
    item = classify_probe(make_probe(), protected=True)
    assert item.status == "protected"


def test_webm_is_not_reencoded_to_hevc_in_place():
    item = classify_probe(make_probe(container=".webm"))
    assert item.status == "not_worth"


def test_optimizer_temp_names_are_recognized():
    assert is_optimization_temp_file(Path("episode.optimizing.mkv"))
    assert not is_optimization_temp_file(Path("episode.mkv"))


def test_media_file_discovery_ignores_temporary_files(tmp_path):
    (tmp_path / "episode.mkv").write_bytes(b"video")
    (tmp_path / "episode.optimizing.mkv").write_bytes(b"temp")
    (tmp_path / "notes.txt").write_text("x")
    manager = OptimizationManager()
    files = manager._media_files(tmp_path)
    assert files == [tmp_path / "episode.mkv"]

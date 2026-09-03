from pathlib import Path

import pytest

import app.services.media_processing as media_processing
import app.services.optimization as optimization_module
from app.services.media_processing import (
    PROCESSING_PROFILES,
    MediaProcessingPriorityManager,
)
from app.services.optimization import AnalysisItem, OptimizationJob, OptimizationManager


def test_processing_priority_defaults_to_low_and_persists(tmp_path):
    config = tmp_path / "processing_profile.json"
    manager = MediaProcessingPriorityManager(config)

    assert manager.priority == "low"
    assert manager.profile.nice == 15
    assert manager.profile.cpu_threads == 1

    saved = manager.update_priority("normal")
    assert saved.key == "normal"
    assert config.is_file()

    reloaded = MediaProcessingPriorityManager(config)
    assert reloaded.priority == "normal"
    assert reloaded.profile.nice == 5
    assert reloaded.profile.cpu_threads == 2


def test_invalid_processing_priority_config_falls_back_to_low(tmp_path):
    config = tmp_path / "processing_profile.json"
    config.write_text('{"priority":"unlimited"}\n', encoding="utf-8")
    manager = MediaProcessingPriorityManager(config)
    assert manager.priority == "low"


def test_unknown_priority_cannot_be_saved(tmp_path):
    manager = MediaProcessingPriorityManager(tmp_path / "processing_profile.json")
    with pytest.raises(ValueError):
        manager.update_priority("-20")


def test_low_profile_builds_linux_nice_ionice_and_single_cpu_command(tmp_path, monkeypatch):
    manager = MediaProcessingPriorityManager(tmp_path / "processing_profile.json")
    monkeypatch.setattr(media_processing.sys, "platform", "linux")
    monkeypatch.setattr(
        media_processing.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"nice", "ionice", "taskset"} else None,
    )
    monkeypatch.setattr(media_processing, "_allowed_cpu_ids", lambda: [0, 2, 4, 6])

    cmd = manager.build_ffmpeg_command(["-i", "in.mkv", "out.mp4"], PROCESSING_PROFILES["low"])

    assert cmd[:4] == ["/usr/bin/nice", "-n", "15", "/usr/bin/ionice"]
    assert ["-t", "-c", "3"] == cmd[4:7]
    assert "/usr/bin/taskset" in cmd
    taskset_index = cmd.index("/usr/bin/taskset")
    assert cmd[taskset_index:taskset_index + 3] == ["/usr/bin/taskset", "--cpu-list", "0"]
    assert cmd[-4:] == ["ffmpeg", "-i", "in.mkv", "out.mp4"]
    assert manager.encoder_thread_args(PROCESSING_PROFILES["low"]) == ["-threads", "1"]


def test_normal_and_high_profiles_have_expected_resource_policy(tmp_path, monkeypatch):
    manager = MediaProcessingPriorityManager(tmp_path / "processing_profile.json")
    monkeypatch.setattr(media_processing.sys, "platform", "linux")
    monkeypatch.setattr(
        media_processing.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"nice", "ionice", "taskset"} else None,
    )
    monkeypatch.setattr(media_processing, "_allowed_cpu_ids", lambda: [1, 3, 5, 7])

    normal = manager.build_ffmpeg_command(["-version"], PROCESSING_PROFILES["normal"])
    assert normal[:4] == ["/usr/bin/nice", "-n", "5", "/usr/bin/ionice"]
    assert normal[4:9] == ["-t", "-c", "2", "-n", "7"]
    taskset_index = normal.index("/usr/bin/taskset")
    assert normal[taskset_index:taskset_index + 3] == ["/usr/bin/taskset", "--cpu-list", "1,3"]
    assert manager.encoder_thread_args(PROCESSING_PROFILES["normal"]) == ["-threads", "2"]

    high = manager.build_ffmpeg_command(["-version"], PROCESSING_PROFILES["high"])
    assert high[:4] == ["/usr/bin/nice", "-n", "0", "/usr/bin/ionice"]
    assert high[4:9] == ["-t", "-c", "2", "-n", "4"]
    assert "/usr/bin/taskset" not in high
    assert manager.encoder_thread_args(PROCESSING_PROFILES["high"]) == []


def test_missing_linux_helpers_degrades_to_ffmpeg_instead_of_failing(tmp_path, monkeypatch):
    manager = MediaProcessingPriorityManager(tmp_path / "processing_profile.json")
    monkeypatch.setattr(media_processing.sys, "platform", "linux")
    monkeypatch.setattr(media_processing.shutil, "which", lambda _name: None)

    cmd = manager.build_ffmpeg_command(["-version"], PROCESSING_PROFILES["low"])
    assert cmd == ["ffmpeg", "-version"]


@pytest.mark.asyncio
async def test_priority_change_is_applied_only_to_next_optimization_file(monkeypatch):
    class FakePriorityManager:
        def __init__(self):
            self.profile = PROCESSING_PROFILES["low"]

    fake_priority = FakePriorityManager()
    monkeypatch.setattr(optimization_module, "media_processing_priority_manager", fake_priority)

    manager = OptimizationManager()
    seen: list[str] = []

    async def fake_optimize(path: Path, job_id: int, processing_profile=None):
        seen.append(processing_profile.key)
        if len(seen) == 1:
            fake_priority.profile = PROCESSING_PROFILES["high"]
        return "skipped", 0

    monkeypatch.setattr(manager, "_optimize_one", fake_optimize)
    items = [
        AnalysisItem(path="/tmp/one.mp4", relative_path="one.mp4", status="optimize", reason="test"),
        AnalysisItem(path="/tmp/two.mp4", relative_path="two.mp4", status="optimize", reason="test"),
    ]
    job = OptimizationJob(id=1, total=2, started_priority="low")

    await manager._run_job(job, items)

    assert seen == ["low", "high"]
    assert job.status == "completed"
    assert job.current_priority == "high"
    assert job.processed == 2

@pytest.mark.asyncio
async def test_normalization_transcode_uses_selected_processing_profile(tmp_path, monkeypatch):
    import app.services.normalization as normalization_module
    from app.core.config import settings
    from app.services.normalization import MediaStreams, NormalizationManager

    source = tmp_path / "episode.mkv"
    source.write_bytes(b"original-video")
    monkeypatch.setattr(settings, "MEDIA_DIR", tmp_path)

    async def fake_probe(path: Path, root=None):
        return MediaStreams(
            path=str(path.resolve()),
            relative_path=path.name,
            size=source.stat().st_size if source.exists() else 14,
            duration=60.0,
            container=path.suffix.lower(),
            video_codec="hevc",
            audio_codecs=["aac"],
            subtitle_codecs=[],
            unsupported_subtitle_codecs=[],
        )

    async def fake_validate(output, original):
        return True, "ok"

    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"converted-mp4")
        return FakeProcess()

    manager = NormalizationManager()
    monkeypatch.setattr(manager, "_protected_paths", lambda: set())
    monkeypatch.setattr(normalization_module, "probe_streams", fake_probe)
    monkeypatch.setattr(normalization_module, "validate_mp4", fake_validate)
    monkeypatch.setattr(normalization_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await manager._convert_one(source, 1, PROCESSING_PROFILES["low"])

    assert result == "transcode"
    assert not source.exists()
    assert (tmp_path / "episode.mp4").read_bytes() == b"converted-mp4"
    assert "ffmpeg" in captured["cmd"]
    assert "-threads" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-threads") + 1] == "1"


@pytest.mark.asyncio
async def test_hevc_optimization_uses_selected_processing_profile_and_atomic_replace(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.optimization import ProbeResult

    source = tmp_path / "episode.mp4"
    source.write_bytes(b"x" * 1000)
    monkeypatch.setattr(settings, "MEDIA_DIR", tmp_path)

    original_probe = ProbeResult(
        path=str(source.resolve()),
        relative_path=source.name,
        size=1000,
        duration=60.0,
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        fps=24.0,
        bitrate=8_000_000,
        video_bitrate=7_800_000,
        audio_bitrate=160_000,
        streams=2,
        container=".mp4",
    )

    async def fake_probe(path: Path, root=None):
        return original_probe

    async def fake_validate(output, original, profile):
        return True, "ok", original_probe

    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"y" * 400)
        return FakeProcess()

    manager = OptimizationManager()
    monkeypatch.setattr(manager, "_protected_paths", lambda: set())
    monkeypatch.setattr(optimization_module, "probe_media", fake_probe)
    monkeypatch.setattr(optimization_module, "validate_output", fake_validate)
    monkeypatch.setattr(optimization_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result, saved = await manager._optimize_one(source, 1, PROCESSING_PROFILES["normal"])

    assert result == "optimized"
    assert saved == 600
    assert source.read_bytes() == b"y" * 400
    assert "ffmpeg" in captured["cmd"]
    assert "libx265" in captured["cmd"]
    assert "-threads" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-threads") + 1] == "2"

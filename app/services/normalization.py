from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session

from app.core.config import settings
from app.db.session import engine
from app.services.channel import channel_engine
from app.services.media_processing import (
    ProcessingFileState,
    ProcessingPriorityProfile,
    media_processing_priority_manager,
)
from app.services.scanner import scan_library

logger = logging.getLogger(__name__)

TEMP_MARKER = ".converting"
TARGET_EXTENSION = ".mp4"
MP4_VIDEO_COPY_CODECS = {"h264"}
MP4_AUDIO_COPY_CODECS = {"aac", "mp3"}
TEXT_SUBTITLE_CODECS = {
    "ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text", "microdvd",
}


@dataclass
class MediaStreams:
    path: str
    relative_path: str
    size: int
    duration: float
    container: str
    video_codec: Optional[str]
    audio_codecs: list[str]
    subtitle_codecs: list[str]
    unsupported_subtitle_codecs: list[str]


@dataclass
class NormalizationItem:
    path: str
    relative_path: str
    status: str
    reason: str
    size: int = 0
    video_codec: Optional[str] = None
    audio_codecs: list[str] | None = None
    subtitle_codecs: list[str] | None = None
    strategy: Optional[str] = None


@dataclass
class NormalizationAnalysis:
    created_at: float
    scan_seconds: float
    total_files: int
    ready: int
    convert: int
    protected: int
    errors: int
    remux: int
    transcode: int
    total_size: int
    items: list[NormalizationItem]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizationJob:
    id: int
    status: str = "queued"
    total: int = 0
    processed: int = 0
    converted: int = 0
    remuxed: int = 0
    transcoded: int = 0
    skipped: int = 0
    errors: int = 0
    current_file: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    started_priority: str = "low"
    current_priority: Optional[str] = None
    files: list[ProcessingFileState] = field(default_factory=list, repr=False)

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 100.0 if self.status == "completed" else 0.0
        return round((self.processed / self.total) * 100, 2)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(max(0.0, end - self.started_at), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "converted": self.converted,
            "remuxed": self.remuxed,
            "transcoded": self.transcoded,
            "skipped": self.skipped,
            "errors": self.errors,
            "current_file": self.current_file,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "started_priority": self.started_priority,
            "current_priority": self.current_priority,
            "progress": self.progress,
            "elapsed_seconds": self.elapsed_seconds,
        }

    def details_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data["files"] = [item.to_dict() for item in self.files]
        return data


def is_normalization_temp_file(path: Path | str) -> bool:
    return TEMP_MARKER in Path(path).stem


async def probe_streams(path: Path, root: Optional[Path] = None) -> MediaStreams:
    root_dir = (root or settings.resolved_media_dir).resolve()
    resolved = path.resolve()
    rel = resolved.relative_to(root_dir).as_posix()

    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,format_name:stream=codec_type,codec_name",
        "-of", "json", str(resolved),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "ffprobe failed")

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc

    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        raise RuntimeError("invalid or missing duration")

    video_codec: Optional[str] = None
    audio_codecs: list[str] = []
    subtitle_codecs: list[str] = []
    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        codec = (stream.get("codec_name") or "unknown").lower()
        if codec_type == "video" and video_codec is None:
            video_codec = codec
        elif codec_type == "audio":
            audio_codecs.append(codec)
        elif codec_type == "subtitle":
            subtitle_codecs.append(codec)

    if video_codec is None:
        raise RuntimeError("video stream not found")

    unsupported = sorted({c for c in subtitle_codecs if c not in TEXT_SUBTITLE_CODECS})
    return MediaStreams(
        path=str(resolved),
        relative_path=rel,
        size=resolved.stat().st_size,
        duration=duration,
        container=resolved.suffix.lower(),
        video_codec=video_codec,
        audio_codecs=audio_codecs,
        subtitle_codecs=subtitle_codecs,
        unsupported_subtitle_codecs=unsupported,
    )


def classify_media(media: MediaStreams, *, protected: bool = False) -> NormalizationItem:
    base = dict(
        path=media.path,
        relative_path=media.relative_path,
        size=media.size,
        video_codec=media.video_codec,
        audio_codecs=media.audio_codecs,
        subtitle_codecs=media.subtitle_codecs,
    )

    # This subsystem has one responsibility only: turn non-MP4 files into MP4.
    # Codec/size optimization is intentionally handled by optimization.py.
    if media.container == TARGET_EXTENSION:
        return NormalizationItem(
            **base,
            status="ready",
            strategy="none",
            reason="Ya es un archivo MP4; no necesita conversión de contenedor.",
        )

    if protected:
        return NormalizationItem(
            **base, status="protected",
            reason="En reproducción o programado inmediatamente después; se omite por seguridad.",
        )

    video_copy = (media.video_codec or "").lower() in MP4_VIDEO_COPY_CODECS
    audio_copy = all(codec in MP4_AUDIO_COPY_CODECS for codec in media.audio_codecs)
    needs_video_transcode = not video_copy
    needs_audio_transcode = not audio_copy

    # Text subtitles can be converted to mov_text. Bitmap subtitle formats (PGS,
    # VobSub, etc.) cannot be represented safely as MP4 text subtitles. We still
    # create the MP4, but keep the original file instead of deleting it so no
    # subtitle content is lost. _convert_one() implements that publication rule.
    drops_bitmap_subtitles = bool(media.unsupported_subtitle_codecs)
    strategy = "transcode" if (needs_video_transcode or needs_audio_transcode) else "remux"

    reasons: list[str] = [f"contenedor {media.container or 'desconocido'} → MP4"]
    if needs_video_transcode:
        reasons.append(f"video {media.video_codec or 'desconocido'} → H.264")
    if needs_audio_transcode:
        reasons.append("audio → AAC")
    if media.subtitle_codecs:
        reasons.append("subtítulos descartados")

    return NormalizationItem(
        **base,
        status="convert",
        strategy=strategy,
        reason="Normalizar: " + ", ".join(reasons) + ".",
    )


async def validate_mp4(output: Path, original: MediaStreams) -> tuple[bool, str]:
    if not output.is_file() or output.stat().st_size <= 0:
        return False, "FFmpeg no produjo un archivo válido."
    try:
        result = await probe_streams(output, output.parent)
    except Exception as exc:
        return False, f"FFprobe no pudo validar el resultado: {exc}"

    if result.container != ".mp4":
        return False, "El resultado no tiene extensión MP4."
    if (result.video_codec or "").lower() != "h264":
        return False, "El video resultante no usa H.264."
    if any(codec not in MP4_AUDIO_COPY_CODECS for codec in result.audio_codecs):
        return False, "El resultado contiene audio no compatible con el perfil MP4."
    tolerance = max(1.0, original.duration * 0.01)
    if abs(result.duration - original.duration) > tolerance:
        return False, "La duración del resultado difiere demasiado del original."
    return True, "ok"


class NormalizationManager:
    def __init__(self) -> None:
        self._latest_analysis: Optional[NormalizationAnalysis] = None
        self._jobs: dict[int, NormalizationJob] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._next_job_id = 1
        self._lock = asyncio.Lock()

    def reset_for_tests(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._latest_analysis = None
        self._jobs.clear()
        self._tasks.clear()
        self._processes.clear()
        self._next_job_id = 1

    @property
    def latest_analysis(self) -> Optional[NormalizationAnalysis]:
        return self._latest_analysis

    def get_job(self, job_id: int) -> Optional[NormalizationJob]:
        return self._jobs.get(job_id)

    def get_active_job(self) -> Optional[NormalizationJob]:
        return next((j for j in self._jobs.values() if j.status in {"queued", "running"}), None)

    def _prune_finished_jobs(self, keep: int = 3) -> None:
        finished = sorted(
            (job for job in self._jobs.values() if job.status not in {"queued", "running"}),
            key=lambda job: job.id,
            reverse=True,
        )
        for old_job in finished[max(0, keep):]:
            self._jobs.pop(old_job.id, None)
            self._tasks.pop(old_job.id, None)
            self._processes.pop(old_job.id, None)

    def _media_files(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return sorted(
            p for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in settings.SUPPORTED_EXTENSIONS
            and not is_normalization_temp_file(p)
            and ".optimizing" not in p.stem
        )

    def _protected_paths(self) -> set[Path]:
        try:
            with Session(engine) as session:
                return {p.resolve() for p in channel_engine.get_protected_media_paths(session)}
        except Exception as exc:
            logger.warning("Could not determine protected media paths: %s", exc)
            return set()

    async def analyze(self, root_dir: Optional[Path] = None) -> NormalizationAnalysis:
        start = time.monotonic()
        root = (root_dir or settings.resolved_media_dir).resolve()
        protected = self._protected_paths()
        items: list[NormalizationItem] = []

        for path in self._media_files(root):
            try:
                media = await probe_streams(path, root)
                items.append(classify_media(media, protected=path.resolve() in protected))
            except Exception as exc:
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    rel = str(path)
                items.append(NormalizationItem(
                    path=str(path), relative_path=rel, status="error", reason=str(exc),
                    size=path.stat().st_size if path.exists() else 0,
                ))

        analysis = NormalizationAnalysis(
            created_at=time.time(),
            scan_seconds=round(time.monotonic() - start, 3),
            total_files=len(items),
            ready=sum(i.status == "ready" for i in items),
            convert=sum(i.status == "convert" for i in items),
            protected=sum(i.status == "protected" for i in items),
            errors=sum(i.status == "error" for i in items),
            remux=sum(i.status == "convert" and i.strategy == "remux" for i in items),
            transcode=sum(i.status == "convert" and i.strategy == "transcode" for i in items),
            total_size=sum(i.size for i in items),
            items=items,
        )
        self._latest_analysis = analysis
        return analysis

    async def create_job(self) -> NormalizationJob:
        async with self._lock:
            if self.get_active_job() is not None:
                raise RuntimeError("Ya hay una conversión a MP4 en ejecución.")
            self._prune_finished_jobs()
            analysis = self._latest_analysis or await self.analyze()
            candidates = [i for i in analysis.items if i.status == "convert"]
            if candidates and shutil.which("ffmpeg") is None:
                raise RuntimeError("FFmpeg no está instalado o no está disponible en PATH.")
            job = NormalizationJob(
                id=self._next_job_id,
                total=len(candidates),
                started_priority=media_processing_priority_manager.priority,
                files=[
                    ProcessingFileState(
                        relative_path=item.relative_path,
                        action=item.strategy or "convert",
                    )
                    for item in candidates
                ],
            )
            self._next_job_id += 1
            self._jobs[job.id] = job
            self._tasks[job.id] = asyncio.create_task(self._run_job(job, candidates))
            return job

    async def cancel_job(self, job_id: int) -> NormalizationJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status not in {"queued", "running"}:
            return job

        # Mark it first so UI/API observers immediately see the cancellation.
        # Cancel the asyncio task before waiting for FFmpeg to exit, preventing
        # the job loop from advancing to the next file in a completion race.
        job.status = "cancelled"
        task = self._tasks.get(job_id)
        proc = self._processes.get(job_id)
        if proc is not None and proc.returncode is None:
            proc.terminate()
        if task is not None and not task.done():
            task.cancel()

        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif task is not None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # _run_job records failures on the job itself.
                pass

        job.current_file = None
        if job.finished_at is None:
            job.finished_at = time.time()
        return job

    async def _run_job(self, job: NormalizationJob, candidates: list[NormalizationItem]) -> None:
        if len(job.files) != len(candidates):
            job.files = [
                ProcessingFileState(
                    relative_path=item.relative_path,
                    action=item.strategy or "convert",
                )
                for item in candidates
            ]
        job.status = "running"
        job.started_at = time.time()
        try:
            for item, file_state in zip(candidates, job.files):
                job.current_file = item.relative_path
                file_state.status = "processing"
                file_state.started_at = time.time()
                try:
                    processing_profile = media_processing_priority_manager.profile
                    job.current_priority = processing_profile.key
                    file_state.priority = processing_profile.key
                    result = await self._convert_one(
                        Path(item.path), job.id, processing_profile
                    )
                    if result == "remux":
                        job.converted += 1
                        job.remuxed += 1
                        file_state.status = "completed"
                        file_state.result = "Remux completado"
                    elif result == "transcode":
                        job.converted += 1
                        job.transcoded += 1
                        file_state.status = "completed"
                        file_state.result = "Transcodificación completada"
                    else:
                        job.skipped += 1
                        file_state.status = "skipped"
                        file_state.result = "Omitido al volver a comprobar el archivo"
                except asyncio.CancelledError:
                    file_state.status = "cancelled"
                    file_state.result = "Procesamiento cancelado"
                    raise
                except Exception as exc:
                    job.errors += 1
                    file_state.status = "error"
                    file_state.error = str(exc)
                    file_state.result = "Error"
                    logger.exception("MP4 normalization failed for %s: %s", item.path, exc)
                finally:
                    file_state.finished_at = time.time()
                    job.processed += 1

            # Rebuild the filesystem-backed index only after all atomic replacements
            # are complete. This removes old paths and indexes the new .mp4 paths.
            with Session(engine) as session:
                await scan_library(session)
                await channel_engine.notify_library_changed(session)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            # Some earlier files may already have been atomically published as
            # MP4. Rebuild the index before finishing the cancellation.
            try:
                with Session(engine) as session:
                    await scan_library(session)
                    await channel_engine.notify_library_changed(session)
            except Exception as exc:
                logger.warning("Could not rescan library after cancelling normalization: %s", exc)
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            logger.exception("MP4 normalization job %s failed", job.id)
        finally:
            job.current_file = None
            job.finished_at = time.time()
            self._latest_analysis = None
            self._processes.pop(job.id, None)

    async def _convert_one(
        self,
        path: Path,
        job_id: int,
        processing_profile: Optional[ProcessingPriorityProfile] = None,
    ) -> str:
        if not path.is_file() or is_normalization_temp_file(path):
            return "skipped"

        root = settings.resolved_media_dir.resolve()
        processing_profile = processing_profile or media_processing_priority_manager.profile
        media = await probe_streams(path, root)
        decision = classify_media(media, protected=path.resolve() in self._protected_paths())
        if decision.status != "convert" or decision.strategy not in {"remux", "transcode"}:
            return "skipped"

        target = path.with_suffix(TARGET_EXTENSION)
        if target != path and target.exists():
            # A previous run may already have published the MP4 but kept the
            # original source. Validate that MP4 and finish the migration by
            # deleting the redundant source instead of failing forever.
            valid, reason = await validate_mp4(target, media)
            if not valid:
                raise RuntimeError(
                    f"Ya existe {target.name}, pero no supera la validación: {reason}"
                )
            path.unlink()
            logger.info("Removed redundant source after validating existing MP4: %s", path)
            return decision.strategy
        temp = target.with_name(f"{target.stem}{TEMP_MARKER}{target.suffix}")

        try:
            if temp.exists():
                temp.unlink()

            video_codec = "copy" if (media.video_codec or "").lower() in MP4_VIDEO_COPY_CODECS else "libx264"
            audio_codec = "copy" if all(c in MP4_AUDIO_COPY_CODECS for c in media.audio_codecs) else "aac"

            ffmpeg_args = [
                "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(path),
                "-map", "0:v:0", "-map", "0:a?",
            ]
            # SimpliTV only needs video/audio for linear playback. Subtitle
            # streams are intentionally not mapped, regardless of their format.
            ffmpeg_args += ["-c:v", video_codec]
            if video_codec != "copy":
                ffmpeg_args += ["-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
                ffmpeg_args += media_processing_priority_manager.encoder_thread_args(processing_profile)
            ffmpeg_args += ["-c:a", audio_codec]
            if audio_codec != "copy":
                ffmpeg_args += ["-b:a", "192k"]
            ffmpeg_args += [
                "-map_metadata", "0",
                "-movflags", "+faststart",
                str(temp),
            ]
            cmd = media_processing_priority_manager.build_ffmpeg_command(
                ffmpeg_args, processing_profile
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes[job_id] = proc
            try:
                _, stderr = await proc.communicate()
            finally:
                self._processes.pop(job_id, None)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "ffmpeg failed")

            valid, reason = await validate_mp4(temp, media)
            if not valid:
                raise RuntimeError(reason)

            # Atomic publication of the verified MP4. The source is removed only
            # after the target is safely in place; on any earlier failure it survives.
            os.replace(temp, target)
            if target != path:
                path.unlink()
                logger.info("Deleted original source after successful MP4 validation: %s", path)
            return decision.strategy
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    logger.warning("Could not delete temporary conversion file %s", temp)


normalization_manager = NormalizationManager()

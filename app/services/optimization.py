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

logger = logging.getLogger(__name__)

TEMP_MARKER = ".optimizing"
HEVC_CODECS = {"hevc", "h265", "x265"}
SAFE_HEVC_CONTAINERS = {".mp4", ".m4v", ".mkv", ".mov"}


@dataclass(frozen=True)
class OptimizationProfile:
    video_codec: str = "hevc"
    max_width: int = 1920
    max_height: int = 1080
    crf: int = 24
    preset: str = "medium"
    min_savings_ratio: float = 0.08
    # Conservative bitrate guides used only for analysis/estimation. FFmpeg
    # itself uses CRF so it can preserve quality better than a hard bitrate cap.
    target_video_bitrates: dict[str, int] = field(default_factory=lambda: {
        "1080p": 2_500_000,
        "720p": 1_500_000,
        "sd": 900_000,
    })
    assumed_audio_bitrate: int = 160_000


DEFAULT_PROFILE = OptimizationProfile()
PROFILE_CONFIG_PATH = settings.BASE_DIR / "optimization_profile.json"


def _profile_to_dict(profile: OptimizationProfile) -> dict[str, Any]:
    return asdict(profile)


def _profile_from_dict(data: dict[str, Any]) -> OptimizationProfile:
    defaults = DEFAULT_PROFILE
    bitrates = data.get("target_video_bitrates") or defaults.target_video_bitrates
    return OptimizationProfile(
        video_codec="hevc",
        max_width=int(data.get("max_width", defaults.max_width)),
        max_height=int(data.get("max_height", defaults.max_height)),
        crf=int(data.get("crf", defaults.crf)),
        preset=str(data.get("preset", defaults.preset)),
        min_savings_ratio=float(data.get("min_savings_ratio", defaults.min_savings_ratio)),
        target_video_bitrates={
            "1080p": int(bitrates.get("1080p", defaults.target_video_bitrates["1080p"])),
            "720p": int(bitrates.get("720p", defaults.target_video_bitrates["720p"])),
            "sd": int(bitrates.get("sd", defaults.target_video_bitrates["sd"])),
        },
        assumed_audio_bitrate=int(data.get("assumed_audio_bitrate", defaults.assumed_audio_bitrate)),
    )


def _load_profile() -> OptimizationProfile:
    try:
        if PROFILE_CONFIG_PATH.is_file():
            data = json.loads(PROFILE_CONFIG_PATH.read_text(encoding="utf-8"))
            return _profile_from_dict(data)
    except Exception as exc:
        logger.warning("Could not load optimization profile; defaults will be used: %s", exc)
    return DEFAULT_PROFILE


def _save_profile(profile: OptimizationProfile) -> None:
    tmp = PROFILE_CONFIG_PATH.with_suffix(PROFILE_CONFIG_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(_profile_to_dict(profile), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, PROFILE_CONFIG_PATH)


PROFILE = DEFAULT_PROFILE


@dataclass
class ProbeResult:
    path: str
    relative_path: str
    size: int
    duration: float
    video_codec: Optional[str]
    audio_codec: Optional[str]
    width: int
    height: int
    fps: float
    bitrate: int
    video_bitrate: int
    audio_bitrate: int
    streams: int
    container: str


@dataclass
class AnalysisItem:
    path: str
    relative_path: str
    status: str
    reason: str
    size: int = 0
    duration: float = 0.0
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    width: int = 0
    height: int = 0
    bitrate: int = 0
    estimated_size: int = 0
    estimated_savings: int = 0


@dataclass
class OptimizationAnalysis:
    created_at: float
    scan_seconds: float
    total_files: int
    ok: int
    optimize: int
    not_worth: int
    errors: int
    protected: int
    total_size: int
    estimated_size: int
    estimated_savings: int
    items: list[AnalysisItem]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OptimizationJob:
    id: int
    status: str = "queued"
    total: int = 0
    processed: int = 0
    optimized: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_saved: int = 0
    current_file: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 100.0 if self.status == "completed" else 0.0
        return round((self.processed / self.total) * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["progress"] = self.progress
        return data


def is_optimization_temp_file(path: Path | str) -> bool:
    return TEMP_MARKER in Path(path).stem


def _rate(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _fps(value: Any) -> float:
    try:
        if not value or value == "0/0":
            return 0.0
        if isinstance(value, str) and "/" in value:
            n, d = value.split("/", 1)
            d_value = float(d)
            return float(n) / d_value if d_value else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


async def probe_media(file_path: Path, root_dir: Optional[Path] = None) -> ProbeResult:
    root = (root_dir or settings.resolved_media_dir).resolve()
    path = file_path.resolve()
    rel = path.relative_to(root).as_posix()

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,bit_rate,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate",
        "-of", "json", str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip() or "ffprobe failed"
        raise RuntimeError(message)

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc

    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or 0.0)
    if duration <= 0:
        raise RuntimeError("invalid or missing duration")

    video = None
    audio = None
    streams = data.get("streams") or []
    for stream in streams:
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream

    if video is None:
        raise RuntimeError("video stream not found")

    size = path.stat().st_size
    container_rate = _rate(fmt.get("bit_rate"))
    computed_rate = int((size * 8) / duration) if duration else 0

    return ProbeResult(
        path=str(path),
        relative_path=rel,
        size=size,
        duration=duration,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
        width=_rate(video.get("width")),
        height=_rate(video.get("height")),
        fps=_fps(video.get("avg_frame_rate")),
        bitrate=container_rate or computed_rate,
        video_bitrate=_rate(video.get("bit_rate")),
        audio_bitrate=_rate(audio.get("bit_rate")) if audio else 0,
        streams=len(streams),
        container=path.suffix.lower(),
    )


def _resolution_bucket(probe: ProbeResult, profile: OptimizationProfile) -> str:
    out_w = min(probe.width or profile.max_width, profile.max_width)
    out_h = min(probe.height or profile.max_height, profile.max_height)
    if out_h > 720 or out_w > 1280:
        return "1080p"
    if out_h > 576 or out_w > 1024:
        return "720p"
    return "sd"


def classify_probe(
    probe: ProbeResult,
    profile: OptimizationProfile = PROFILE,
    *,
    protected: bool = False,
) -> AnalysisItem:
    if protected:
        return AnalysisItem(
            path=probe.path,
            relative_path=probe.relative_path,
            status="protected",
            reason="En reproducción o programado inmediatamente después; se omite por seguridad.",
            size=probe.size,
            duration=probe.duration,
            video_codec=probe.video_codec,
            audio_codec=probe.audio_codec,
            width=probe.width,
            height=probe.height,
            bitrate=probe.bitrate,
            estimated_size=probe.size,
        )

    if probe.container not in SAFE_HEVC_CONTAINERS:
        return AnalysisItem(
            path=probe.path,
            relative_path=probe.relative_path,
            status="not_worth",
            reason=f"El contenedor {probe.container or 'desconocido'} no se optimiza a HEVC sin cambiar la ruta del archivo.",
            size=probe.size,
            duration=probe.duration,
            video_codec=probe.video_codec,
            audio_codec=probe.audio_codec,
            width=probe.width,
            height=probe.height,
            bitrate=probe.bitrate,
            estimated_size=probe.size,
        )

    bucket = _resolution_bucket(probe, profile)
    target_video = profile.target_video_bitrates[bucket]
    target_total = target_video + (probe.audio_bitrate or profile.assumed_audio_bitrate)
    estimated_size = int((target_total * probe.duration) / 8)
    estimated_size = max(1, estimated_size)
    estimated_savings = max(0, probe.size - estimated_size)
    savings_ratio = estimated_savings / probe.size if probe.size else 0.0

    oversized_resolution = probe.width > profile.max_width or probe.height > profile.max_height
    is_hevc = (probe.video_codec or "").lower() in HEVC_CODECS
    bitrate_excessive = probe.bitrate > int(target_total * 1.50)

    if is_hevc and not oversized_resolution and not bitrate_excessive:
        status = "ok"
        reason = "Ya cumple el perfil HEVC, resolución máxima y bitrate razonable."
    elif not oversized_resolution and savings_ratio < profile.min_savings_ratio:
        status = "not_worth"
        reason = "El ahorro estimado es demasiado pequeño para justificar otra codificación con pérdida."
    else:
        status = "optimize"
        reasons = []
        if oversized_resolution:
            reasons.append(f"resolución superior a {profile.max_height}p")
        if not is_hevc:
            reasons.append(f"codec {probe.video_codec or 'desconocido'}")
        if bitrate_excessive:
            reasons.append("bitrate elevado")
        reason = "Candidato: " + ", ".join(reasons or ["no cumple el perfil"]) + "."

    return AnalysisItem(
        path=probe.path,
        relative_path=probe.relative_path,
        status=status,
        reason=reason,
        size=probe.size,
        duration=probe.duration,
        video_codec=probe.video_codec,
        audio_codec=probe.audio_codec,
        width=probe.width,
        height=probe.height,
        bitrate=probe.bitrate,
        estimated_size=min(probe.size, estimated_size) if status != "optimize" else estimated_size,
        estimated_savings=estimated_savings if status == "optimize" else 0,
    )


async def validate_output(
    output_path: Path,
    original: ProbeResult,
    profile: OptimizationProfile = PROFILE,
) -> tuple[bool, str, Optional[ProbeResult]]:
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        return False, "FFmpeg no produjo un archivo válido.", None
    output_size = output_path.stat().st_size
    if output_size >= original.size:
        return False, "El resultado no es menor que el original.", None
    actual_savings_ratio = (original.size - output_size) / original.size if original.size else 0.0
    if actual_savings_ratio < profile.min_savings_ratio:
        return False, "El ahorro real es demasiado pequeño para justificar reemplazar el original.", None

    try:
        result = await probe_media(output_path, output_path.parent)
    except Exception as exc:
        return False, f"FFprobe no pudo validar el resultado: {exc}", None

    if (result.video_codec or "").lower() not in HEVC_CODECS:
        return False, "El resultado no usa HEVC.", result
    if result.width > profile.max_width or result.height > profile.max_height:
        return False, "El resultado excede la resolución máxima.", result

    duration_tolerance = max(1.0, original.duration * 0.01)
    if abs(result.duration - original.duration) > duration_tolerance:
        return False, "La duración del resultado difiere demasiado del original.", result
    if result.streams <= 0:
        return False, "El resultado no contiene streams válidos.", result

    return True, "ok", result


class OptimizationManager:
    def __init__(self) -> None:
        self._latest_analysis: Optional[OptimizationAnalysis] = None
        self._jobs: dict[int, OptimizationJob] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._next_job_id = 1
        self._lock = asyncio.Lock()
        self._profile = _load_profile()

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
    def profile(self) -> OptimizationProfile:
        return self._profile

    def profile_dict(self) -> dict[str, Any]:
        return _profile_to_dict(self._profile)

    def update_profile(self, profile: OptimizationProfile) -> OptimizationProfile:
        if self.get_active_job() is not None:
            raise RuntimeError("No se puede cambiar el perfil mientras hay una optimización en ejecución.")
        _save_profile(profile)
        self._profile = profile
        # A previous analysis was made with different rules and is no longer valid.
        self._latest_analysis = None
        return profile

    @property
    def latest_analysis(self) -> Optional[OptimizationAnalysis]:
        return self._latest_analysis

    def _media_files(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return sorted(
            p for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in settings.SUPPORTED_EXTENSIONS
            and not is_optimization_temp_file(p)
            and ".converting" not in p.stem
        )

    def _protected_paths(self) -> set[Path]:
        try:
            with Session(engine) as session:
                return channel_engine.get_protected_media_paths(session)
        except Exception as exc:
            logger.warning("Could not determine protected media paths: %s", exc)
            return set()

    async def analyze(self, root_dir: Optional[Path] = None) -> OptimizationAnalysis:
        start = time.monotonic()
        root = (root_dir or settings.resolved_media_dir).resolve()
        protected_paths = {p.resolve() for p in self._protected_paths()}
        items: list[AnalysisItem] = []

        for path in self._media_files(root):
            try:
                probe = await probe_media(path, root)
                items.append(classify_probe(probe, self._profile, protected=path.resolve() in protected_paths))
            except Exception as exc:
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    rel = str(path)
                items.append(AnalysisItem(
                    path=str(path), relative_path=rel, status="error",
                    reason=str(exc), size=path.stat().st_size if path.exists() else 0,
                ))

        total_size = sum(i.size for i in items)
        estimated_savings = sum(i.estimated_savings for i in items if i.status == "optimize")
        analysis = OptimizationAnalysis(
            created_at=time.time(),
            scan_seconds=round(time.monotonic() - start, 3),
            total_files=len(items),
            ok=sum(i.status == "ok" for i in items),
            optimize=sum(i.status == "optimize" for i in items),
            not_worth=sum(i.status == "not_worth" for i in items),
            errors=sum(i.status == "error" for i in items),
            protected=sum(i.status == "protected" for i in items),
            total_size=total_size,
            estimated_size=max(0, total_size - estimated_savings),
            estimated_savings=estimated_savings,
            items=items,
        )
        self._latest_analysis = analysis
        return analysis

    async def create_job(self) -> OptimizationJob:
        async with self._lock:
            # Never allow two FFmpeg library jobs to compete for disk/CPU.
            if any(j.status in {"queued", "running"} for j in self._jobs.values()):
                raise RuntimeError("Ya hay una optimización en ejecución.")

            analysis = self._latest_analysis or await self.analyze()
            candidates = [item for item in analysis.items if item.status == "optimize"]
            if candidates and shutil.which("ffmpeg") is None:
                raise RuntimeError("FFmpeg no está instalado o no está disponible en PATH.")
            job = OptimizationJob(id=self._next_job_id, total=len(candidates))
            self._next_job_id += 1
            self._jobs[job.id] = job
            task = asyncio.create_task(self._run_job(job, candidates))
            self._tasks[job.id] = task
            return job

    def get_job(self, job_id: int) -> Optional[OptimizationJob]:
        return self._jobs.get(job_id)

    def get_active_job(self) -> Optional[OptimizationJob]:
        """Return the currently queued/running job, if one exists."""
        return next(
            (job for job in self._jobs.values() if job.status in {"queued", "running"}),
            None,
        )

    async def cancel_job(self, job_id: int) -> OptimizationJob:
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

    async def _run_job(self, job: OptimizationJob, candidates: list[AnalysisItem]) -> None:
        job.status = "running"
        job.started_at = time.time()
        try:
            for item in candidates:
                job.current_file = item.relative_path
                try:
                    result, saved = await self._optimize_one(Path(item.path), job.id)
                    if result == "optimized":
                        job.optimized += 1
                        job.bytes_saved += saved
                    else:
                        job.skipped += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    job.errors += 1
                    logger.exception("Optimization failed for %s: %s", item.path, exc)
                finally:
                    job.processed += 1

            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            logger.exception("Optimization job %s failed", job.id)
        finally:
            job.current_file = None
            job.finished_at = time.time()
            # Force the next explicit analysis to reflect the filesystem now.
            self._latest_analysis = None
            self._processes.pop(job.id, None)

    async def _optimize_one(self, path: Path, job_id: int) -> tuple[str, int]:
        if not path.is_file() or is_optimization_temp_file(path):
            return "skipped", 0

        root = settings.resolved_media_dir.resolve()
        original = await probe_media(path, root)

        # Re-check immediately before encoding. The broadcast may have advanced
        # since the analysis was made.
        protected_paths = {p.resolve() for p in self._protected_paths()}
        profile = self._profile
        decision = classify_probe(original, profile, protected=path.resolve() in protected_paths)
        if decision.status != "optimize":
            return "skipped", 0

        temp_path = path.with_name(f"{path.stem}{TEMP_MARKER}{path.suffix}")
        try:
            if temp_path.exists():
                temp_path.unlink()

            vf = []
            if original.width > profile.max_width or original.height > profile.max_height:
                # Force both dimensions even for unusual aspect ratios, while
                # preserving DAR and ensuring encoder-friendly even dimensions.
                vf = [
                    "-vf",
                    f"scale='min({profile.max_width},iw)':'min({profile.max_height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                ]

            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(path),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "0:s?",
                "-c:v", "libx265",
                "-preset", profile.preset,
                "-crf", str(profile.crf),
                *vf,
                "-c:a", "copy",
                "-c:s", "copy",
                "-map_metadata", "0",
            ]
            if path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
                cmd += ["-tag:v", "hvc1", "-movflags", "+faststart"]
            cmd.append(str(temp_path))

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

            valid, reason, _ = await validate_output(temp_path, original, profile)
            if not valid:
                logger.info("Discarded optimization for %s: %s", path, reason)
                return "skipped", 0

            new_size = temp_path.stat().st_size
            old_size = original.size
            # os.replace is atomic when source and target are on the same filesystem.
            os.replace(temp_path, path)
            return "optimized", old_size - new_size
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    logger.warning("Could not delete temporary optimization file %s", temp_path)


optimization_manager = OptimizationManager()

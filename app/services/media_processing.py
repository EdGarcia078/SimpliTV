from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

PROCESSING_CONFIG_PATH = settings.BASE_DIR / "processing_profile.json"
DEFAULT_PRIORITY = "low"


@dataclass(frozen=True)
class ProcessingPriorityProfile:
    key: str
    label: str
    description: str
    nice: int
    ionice_class: int
    ionice_priority: Optional[int]
    cpu_threads: Optional[int]

    @property
    def io_label(self) -> str:
        if self.ionice_class == 3:
            return "idle"
        if self.ionice_class == 2:
            return f"best-effort {self.ionice_priority}"
        return f"clase {self.ionice_class}"


@dataclass
class ProcessingFileState:
    """Runtime-only progress for one file inside a media processing job.

    This state intentionally stays in memory together with the parent job. It
    is not media metadata and is never written to SQLite or portable channel
    configuration files.
    """

    relative_path: str
    action: str
    status: str = "pending"
    result: Optional[str] = None
    error: Optional[str] = None
    priority: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(max(0.0, end - self.started_at), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "action": self.action,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "priority": self.priority,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds,
        }


PROCESSING_PROFILES: dict[str, ProcessingPriorityProfile] = {
    "low": ProcessingPriorityProfile(
        key="low",
        label="Baja",
        description=(
            "Recomendada mientras se utiliza SimpliTV. FFmpeg cede CPU y disco "
            "y se restringe a 1 CPU lógica."
        ),
        nice=15,
        ionice_class=3,
        ionice_priority=None,
        cpu_threads=1,
    ),
    "normal": ProcessingPriorityProfile(
        key="normal",
        label="Normal",
        description=(
            "Equilibrio entre velocidad y capacidad de respuesta. FFmpeg usa hasta "
            "2 CPU lógicas con prioridad inferior a la aplicación."
        ),
        nice=5,
        ionice_class=2,
        ionice_priority=7,
        cpu_threads=2,
    ),
    "high": ProcessingPriorityProfile(
        key="high",
        label="Alta",
        description=(
            "Máximo rendimiento. FFmpeg puede utilizar todas las CPU lógicas; "
            "recomendada cuando nadie está viendo SimpliTV."
        ),
        nice=0,
        ionice_class=2,
        ionice_priority=4,
        cpu_threads=None,
    ),
}


def _allowed_cpu_ids() -> list[int]:
    """Return CPUs the current process may use, respecting cpuset/container limits."""
    try:
        affinity = os.sched_getaffinity(0)  # type: ignore[attr-defined]
        if affinity:
            return sorted(int(cpu) for cpu in affinity)
    except (AttributeError, OSError):
        pass
    count = os.cpu_count() or 1
    return list(range(max(1, count)))


class MediaProcessingPriorityManager:
    """Persist and apply closed resource profiles to FFmpeg child processes.

    The manager deliberately changes only FFmpeg processes. FFprobe, FastAPI,
    streaming and the channel engine keep the application's normal scheduling
    priority. Resource settings are snapshotted for each file, so changing the
    profile never interrupts an FFmpeg process already working on a file.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = Path(config_path or PROCESSING_CONFIG_PATH)
        self._lock = threading.RLock()
        self._priority = self._load_priority()

    def _load_priority(self) -> str:
        try:
            if not self._config_path.is_file():
                return DEFAULT_PRIORITY
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            priority = str(data.get("priority", DEFAULT_PRIORITY)).lower().strip()
            if priority in PROCESSING_PROFILES:
                return priority
            logger.warning(
                "Unknown media processing priority %r; using %s.",
                priority,
                DEFAULT_PRIORITY,
            )
        except Exception as exc:
            logger.warning(
                "Could not load media processing priority; using %s: %s",
                DEFAULT_PRIORITY,
                exc,
            )
        return DEFAULT_PRIORITY

    def _save_priority(self, priority: str) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._config_path.with_name(f"{self._config_path.name}.tmp")
        tmp.write_text(
            json.dumps({"priority": priority}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._config_path)

    @property
    def priority(self) -> str:
        with self._lock:
            return self._priority

    @property
    def profile(self) -> ProcessingPriorityProfile:
        with self._lock:
            return PROCESSING_PROFILES[self._priority]

    def update_priority(self, priority: str) -> ProcessingPriorityProfile:
        normalized = str(priority).lower().strip()
        if normalized not in PROCESSING_PROFILES:
            raise ValueError(f"Prioridad de procesamiento desconocida: {priority}")
        with self._lock:
            self._save_priority(normalized)
            self._priority = normalized
            return PROCESSING_PROFILES[normalized]

    def api_data(self, profile: Optional[ProcessingPriorityProfile] = None) -> dict[str, Any]:
        selected = profile or self.profile
        return {
            "priority": selected.key,
            "label": selected.label,
            "description": selected.description,
            "nice": selected.nice,
            "io_policy": selected.io_label,
            "cpu_threads": selected.cpu_threads,
        }

    @staticmethod
    def encoder_thread_args(profile: ProcessingPriorityProfile) -> list[str]:
        """FFmpeg encoder hint used in addition to Linux CPU affinity.

        Affinity is the hard guard on Linux. The encoder hint additionally keeps
        x264/x265 from creating unnecessary worker threads. High intentionally
        leaves FFmpeg's automatic threading untouched.
        """
        if profile.cpu_threads is None:
            return []
        return ["-threads", str(max(1, profile.cpu_threads))]

    def build_ffmpeg_command(
        self,
        ffmpeg_args: list[str],
        profile: Optional[ProcessingPriorityProfile] = None,
    ) -> list[str]:
        """Build a command that applies the profile without shell=True or sudo.

        Linux utilities are wrappers that exec() the next command, so the PID
        tracked by the job manager remains the FFmpeg process after startup.
        Missing optional utilities degrade gracefully: conversion is never made
        unavailable merely because ionice/taskset/nice is absent.
        """
        selected = profile or self.profile
        command: list[str] = []

        if sys.platform.startswith("linux"):
            nice_bin = shutil.which("nice")
            if nice_bin:
                command += [nice_bin, "-n", str(selected.nice)]
            else:
                logger.warning("'nice' is unavailable; FFmpeg CPU priority cannot be adjusted.")

            ionice_bin = shutil.which("ionice")
            if ionice_bin:
                command += [ionice_bin, "-t", "-c", str(selected.ionice_class)]
                if selected.ionice_priority is not None:
                    command += ["-n", str(selected.ionice_priority)]
            else:
                logger.warning("'ionice' is unavailable; FFmpeg disk priority cannot be adjusted.")

            if selected.cpu_threads is not None:
                taskset_bin = shutil.which("taskset")
                allowed = _allowed_cpu_ids()
                selected_cpus = allowed[: max(1, selected.cpu_threads)]
                # Restrict only when doing so actually leaves CPUs available to
                # the rest of SimpliTV. This also avoids invalid empty CPU lists.
                if taskset_bin and selected_cpus and len(selected_cpus) < len(allowed):
                    command += [taskset_bin, "--cpu-list", ",".join(map(str, selected_cpus))]
                elif not taskset_bin:
                    logger.warning(
                        "'taskset' is unavailable; relying on FFmpeg's thread limit only."
                    )

        command += ["ffmpeg", *ffmpeg_args]
        return command


media_processing_priority_manager = MediaProcessingPriorityManager()

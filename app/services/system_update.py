"""Safe Git-based application updates initiated from the admin panel."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from app.core.config import settings


class SystemUpdateError(RuntimeError):
    """An update cannot be completed safely."""

    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.http_status = http_status


class SystemUpdateManager:
    """Compare the checked-out ``main`` branch with ``origin/main`` and update it."""

    def __init__(self, project_dir: Path | None = None) -> None:
        self.project_dir = (project_dir or settings.BASE_DIR).resolve()
        self._lock = threading.RLock()
        self._restart_scheduled = False

    def _git(self, *args: str, timeout: int = 30) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"

        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.project_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SystemUpdateError(
                "Git no está instalado en el servidor.", http_status=503
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SystemUpdateError(
                "La operación con Git excedió el tiempo de espera.", http_status=504
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if detail:
                detail = detail.splitlines()[-1]
            else:
                detail = "Git terminó con un error desconocido."
            raise SystemUpdateError(detail, http_status=503)

        return result.stdout.strip()

    def _status_without_lock(self, *, fetch: bool) -> dict[str, Any]:
        inside_repo = self._git("rev-parse", "--is-inside-work-tree")
        if inside_repo != "true":
            raise SystemUpdateError(
                "La aplicación no se está ejecutando desde un repositorio Git.",
                http_status=503,
            )

        branch = self._git("branch", "--show-current")
        if branch != "main":
            raise SystemUpdateError(
                f"La rama activa es '{branch or 'HEAD separado'}'; se requiere 'main'.",
                http_status=409,
            )

        if fetch:
            self._git("fetch", "--quiet", "origin", "main", timeout=45)

        local_commit = self._git("rev-parse", "HEAD")
        remote_commit = self._git("rev-parse", "refs/remotes/origin/main")
        counts = self._git(
            "rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"
        ).split()
        if len(counts) != 2:
            raise SystemUpdateError(
                "Git devolvió un estado de sincronización inesperado.", http_status=503
            )

        ahead_by, behind_by = (int(value) for value in counts)
        tracked_changes = bool(
            self._git("status", "--porcelain", "--untracked-files=no")
        )
        update_available = behind_by > 0
        diverged = ahead_by > 0 and behind_by > 0
        can_update = update_available and not diverged and not tracked_changes

        if diverged:
            state = "diverged"
            message = (
                "La rama local y origin/main han divergido. Resuelve el historial "
                "manualmente antes de actualizar."
            )
        elif tracked_changes and update_available:
            state = "local_changes"
            message = (
                "Hay una actualización, pero existen cambios locales rastreados. "
                "Guárdalos o revísalos antes de actualizar."
            )
        elif update_available:
            state = "update_available"
            message = f"Hay {behind_by} commit(s) nuevo(s) en origin/main."
        elif ahead_by > 0:
            state = "local_ahead"
            message = f"La copia local está {ahead_by} commit(s) por delante de origin/main."
        elif tracked_changes:
            state = "local_changes"
            message = "El código está al día, pero contiene cambios locales rastreados."
        else:
            state = "up_to_date"
            message = "El sistema está actualizado."

        return {
            "supported": True,
            "state": state,
            "message": message,
            "branch": branch,
            "update_available": update_available,
            "can_update": can_update,
            "dirty": tracked_changes,
            "diverged": diverged,
            "ahead_by": ahead_by,
            "behind_by": behind_by,
            "local_commit": local_commit,
            "remote_commit": remote_commit,
            "restart_scheduled": self._restart_scheduled,
        }

    def get_status(self, *, fetch: bool = True) -> dict[str, Any]:
        """Return update state after optionally refreshing ``origin/main``."""
        with self._lock:
            return self._status_without_lock(fetch=fetch)

    def apply_update(self) -> dict[str, Any]:
        """Fast-forward the clean local checkout and report the resulting revision."""
        with self._lock:
            if self._restart_scheduled:
                raise SystemUpdateError("El reinicio del sistema ya está programado.")

            current = self._status_without_lock(fetch=True)
            if not current["update_available"]:
                raise SystemUpdateError("El sistema ya está actualizado.")
            if current["diverged"]:
                raise SystemUpdateError(
                    "La rama local ha divergido de origin/main; no se hará un merge automático."
                )
            if current["dirty"]:
                raise SystemUpdateError(
                    "Hay cambios locales rastreados. El pull fue cancelado para no sobrescribirlos."
                )

            previous_commit = current["local_commit"]
            self._git("pull", "--ff-only", "origin", "main", timeout=120)
            updated = self._status_without_lock(fetch=False)
            if updated["local_commit"] == previous_commit or updated["behind_by"] != 0:
                raise SystemUpdateError(
                    "Git no dejó la copia local sincronizada con origin/main.",
                    http_status=500,
                )

            return {
                "message": "Actualización aplicada. SimpliTV se reiniciará ahora.",
                "previous_commit": previous_commit,
                "current_commit": updated["local_commit"],
                "restart_pending": True,
            }

    def schedule_restart(self, delay_seconds: float = 1.5) -> None:
        """Replace this Python process after the HTTP response has been delivered."""
        with self._lock:
            if self._restart_scheduled:
                return
            self._restart_scheduled = True

        timer = threading.Timer(delay_seconds, self._restart_process)
        timer.daemon = True
        timer.start()

    def _restart_process(self) -> None:
        """Re-execute the exact Python entrypoint used to start the server."""
        executable = sys.executable
        argv = [executable, *sys.argv]
        os.chdir(self.project_dir)
        os.execv(executable, argv)


system_update_manager = SystemUpdateManager()

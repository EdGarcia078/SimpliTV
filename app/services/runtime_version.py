"""Runtime deployment and static-asset fingerprints.

The values in this module are computed once when SimpliTV starts.  The deployment
identity follows the actual application contents, so it also works for portable
ZIP installs and manually replaced files.  When Git is available, its current
commit is exposed separately for the built-in updater.  Static assets use their
own content fingerprint so unchanged CSS/JS can stay cached across backend-only
updates.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings


_SOURCE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class RuntimeVersion:
    """Immutable identifiers for the code running in the current process."""

    deployment_id: str
    asset_version: str
    git_commit: str | None
    source: str


def _git_commit(project_dir: Path) -> str | None:
    """Return the current Git commit without contacting any remote."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    commit = result.stdout.strip().lower()
    if result.returncode != 0 or len(commit) != 40:
        return None
    if any(character not in "0123456789abcdef" for character in commit):
        return None
    return commit


def _fingerprint_tree(root: Path, *, suffixes: set[str] | None = None) -> str:
    """Create a deterministic SHA-256 fingerprint from relevant file contents."""
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"missing-tree")
        return digest.hexdigest()

    files = [path for path in root.rglob("*") if path.is_file()]
    if suffixes is not None:
        files = [path for path in files if path.suffix.lower() in suffixes]

    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            # A concurrently replaced file should not prevent the app from starting.
            # Its path still contributes to the fingerprint and the next restart will
            # calculate the final contents normally.
            digest.update(b"unreadable")

    return digest.hexdigest()


def build_runtime_version(project_dir: Path | None = None) -> RuntimeVersion:
    """Build deployment and asset identifiers for one SimpliTV process."""
    project = (project_dir or settings.BASE_DIR).resolve()
    app_dir = project / "app"
    static_dir = app_dir / "static"

    # Source and static contents are combined so every browser-relevant change is
    # detected, including static file types that are not part of _SOURCE_SUFFIXES.
    source_fingerprint = _fingerprint_tree(app_dir, suffixes=_SOURCE_SUFFIXES)
    static_fingerprint = _fingerprint_tree(static_dir)
    commit = _git_commit(project)
    deployment_digest = hashlib.sha256(
        f"{commit or 'portable'}:{source_fingerprint}:{static_fingerprint}".encode("ascii")
    ).hexdigest()
    asset_version = static_fingerprint[:20]

    return RuntimeVersion(
        deployment_id=f"app-{deployment_digest[:32]}",
        asset_version=asset_version,
        git_commit=commit,
        source="git" if commit else "fingerprint",
    )


runtime_version = build_runtime_version()

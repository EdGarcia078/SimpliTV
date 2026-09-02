import subprocess
from pathlib import Path

import pytest

from app.services.system_update import (
    SystemUpdateError,
    SystemUpdateManager,
    system_update_manager,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, content: str) -> None:
    (repo / "version.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "version.txt")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def git_update_repositories(tmp_path: Path):
    origin = tmp_path / "origin.git"
    publisher = tmp_path / "publisher"
    installed = tmp_path / "installed"

    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(publisher)], check=True, capture_output=True)
    _git(publisher, "config", "user.name", "SimpliTV Tests")
    _git(publisher, "config", "user.email", "tests@simplitv.local")
    _git(publisher, "checkout", "-b", "main")
    _commit(publisher, "initial", "v1\n")
    _git(publisher, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    subprocess.run(["git", "clone", str(origin), str(installed)], check=True, capture_output=True)
    _git(installed, "config", "user.name", "SimpliTV Tests")
    _git(installed, "config", "user.email", "tests@simplitv.local")

    return origin, publisher, installed


def test_detects_and_applies_fast_forward_update(git_update_repositories):
    _, publisher, installed = git_update_repositories
    _commit(publisher, "remote update", "v2\n")
    _git(publisher, "push", "origin", "main")

    manager = SystemUpdateManager(installed)
    status = manager.get_status(fetch=True)

    assert status["state"] == "update_available"
    assert status["update_available"] is True
    assert status["can_update"] is True
    assert status["behind_by"] == 1

    result = manager.apply_update()

    assert result["previous_commit"] != result["current_commit"]
    assert (installed / "version.txt").read_text(encoding="utf-8") == "v2\n"
    assert manager.get_status(fetch=False)["state"] == "up_to_date"


def test_refuses_update_when_tracked_files_are_modified(git_update_repositories):
    _, publisher, installed = git_update_repositories
    _commit(publisher, "remote update", "v2\n")
    _git(publisher, "push", "origin", "main")
    (installed / "version.txt").write_text("local change\n", encoding="utf-8")

    manager = SystemUpdateManager(installed)
    status = manager.get_status(fetch=True)

    assert status["state"] == "local_changes"
    assert status["update_available"] is True
    assert status["can_update"] is False

    with pytest.raises(SystemUpdateError, match="cambios locales"):
        manager.apply_update()

    assert (installed / "version.txt").read_text(encoding="utf-8") == "local change\n"


def test_restart_reexecutes_the_current_python_entrypoint(tmp_path, monkeypatch):
    manager = SystemUpdateManager(tmp_path)
    calls = {}

    monkeypatch.setattr("app.services.system_update.sys.executable", "/venv/bin/python")
    monkeypatch.setattr(
        "app.services.system_update.sys.argv",
        ["/venv/bin/uvicorn", "app.main:app", "--port", "8000"],
    )
    monkeypatch.setattr(
        "app.services.system_update.os.chdir",
        lambda path: calls.update(cwd=path),
    )
    monkeypatch.setattr(
        "app.services.system_update.os.execv",
        lambda executable, argv: calls.update(executable=executable, argv=argv),
    )

    manager._restart_process()

    assert calls == {
        "cwd": tmp_path.resolve(),
        "executable": "/venv/bin/python",
        "argv": [
            "/venv/bin/python",
            "/venv/bin/uvicorn",
            "app.main:app",
            "--port",
            "8000",
        ],
    }


def test_update_endpoints_require_admin(unauth_client, auth_client):
    assert unauth_client.get("/api/admin/system/update").status_code == 401
    assert auth_client.get("/api/admin/system/update").status_code == 403


def test_admin_can_check_and_start_update(admin_client, monkeypatch):
    status_payload = {
        "state": "update_available",
        "message": "Hay una actualización.",
        "update_available": True,
        "can_update": True,
    }
    update_payload = {
        "message": "Actualización aplicada.",
        "previous_commit": "a" * 40,
        "current_commit": "b" * 40,
        "restart_pending": True,
    }
    restart_calls = []

    monkeypatch.setattr(system_update_manager, "get_status", lambda fetch=True: status_payload)
    monkeypatch.setattr(system_update_manager, "apply_update", lambda: update_payload)
    monkeypatch.setattr(system_update_manager, "schedule_restart", lambda: restart_calls.append(True))

    status_response = admin_client.get("/api/admin/system/update")
    update_response = admin_client.post("/api/admin/system/update")

    assert status_response.status_code == 200
    assert status_response.json() == status_payload
    assert update_response.status_code == 202
    assert update_response.json() == update_payload
    assert restart_calls == [True]

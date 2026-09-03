from pathlib import Path

from app.services.client_assets import render_client_page, static_cache_control
from app.services.runtime_version import build_runtime_version, runtime_version


def test_health_exposes_running_deployment(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["deployment_id"] == runtime_version.deployment_id
    assert data["asset_version"] == runtime_version.asset_version
    assert response.headers["cache-control"] == "no-store"


def test_client_pages_are_automatically_versioned(client, unauth_client):
    index = client.get("/")
    admin = client.get("/admin")
    login = unauth_client.get("/login")

    for response in (index, admin, login):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
        assert f'name="simplitv-deployment" content="{runtime_version.deployment_id}"' in response.text
        assert f'/static/js/version-watch.js?v={runtime_version.asset_version}' in response.text

    assert f'/static/css/style.css?v={runtime_version.asset_version}' in index.text
    assert f'/static/js/player.js?v={runtime_version.asset_version}' in index.text
    assert f'/static/css/style.css?v={runtime_version.asset_version}' in admin.text
    assert f'/static/js/admin.js?v={runtime_version.asset_version}' in admin.text
    assert f'/static/css/style.css?v={runtime_version.asset_version}' in login.text


def test_static_cache_policy_distinguishes_current_and_legacy_urls(unauth_client):
    current = unauth_client.get(
        f"/static/css/style.css?v={runtime_version.asset_version}"
    )
    legacy = unauth_client.get("/static/css/style.css")
    stale = unauth_client.get("/static/css/style.css?v=old-deployment")

    assert current.status_code == 200
    assert current.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert legacy.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert stale.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"


def test_portable_runtime_version_changes_when_static_content_changes(tmp_path: Path):
    static_dir = tmp_path / "app" / "static" / "js"
    static_dir.mkdir(parents=True)
    script = static_dir / "app.js"
    script.write_text("console.log('v1');\n", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text("VERSION = 1\n", encoding="utf-8")

    first = build_runtime_version(tmp_path)
    script.write_text("console.log('v2');\n", encoding="utf-8")
    second = build_runtime_version(tmp_path)

    assert first.source == "fingerprint"
    assert second.source == "fingerprint"
    assert first.git_commit is None
    assert second.git_commit is None
    assert first.asset_version != second.asset_version
    assert first.deployment_id != second.deployment_id


def test_renderer_versions_all_current_html_shells():
    for filename in ("index.html", "admin.html", "login.html"):
        rendered = render_client_page(filename)
        assert f'name="simplitv-deployment" content="{runtime_version.deployment_id}"' in rendered
        assert f'/static/js/version-watch.js?v={runtime_version.asset_version}' in rendered
        assert '/static/css/style.css"' not in rendered

    assert static_cache_control(runtime_version.asset_version) == (
        "public, max-age=31536000, immutable"
    )
    assert static_cache_control(None) == "no-cache, max-age=0, must-revalidate"
    assert static_cache_control("stale") == "no-cache, max-age=0, must-revalidate"


def test_version_watcher_reloads_when_deployment_changes():
    source = Path("app/static/js/version-watch.js").read_text(encoding="utf-8")

    assert "data.deployment_id !== loadedDeployment" in source
    assert "window.location.reload()" in source
    assert "visibilitychange" in source
    assert "cache: 'no-store'" in source

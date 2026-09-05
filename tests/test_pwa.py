from pathlib import Path

from app.services.pwa import PWA_CACHE_PREFIX, pwa_precache_urls
from app.services.runtime_version import runtime_version


def test_manifest_is_installable_and_versioned(unauth_client):
    response = unauth_client.get('/manifest.webmanifest')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/manifest+json')
    assert response.headers['cache-control'] == 'no-cache, max-age=0, must-revalidate'

    manifest = response.json()
    assert manifest['name'] == 'SimpliTV'
    assert manifest['short_name'] == 'SimpliTV'
    assert manifest['id'] == '/'
    assert manifest['start_url'] == '/'
    assert manifest['scope'] == '/'
    assert manifest['display'] == 'standalone'

    icons = manifest['icons']
    assert any(icon['sizes'] == '192x192' for icon in icons)
    assert any(icon['sizes'] == '512x512' and icon['purpose'] == 'any' for icon in icons)
    assert any(icon['sizes'] == '512x512' and icon['purpose'] == 'maskable' for icon in icons)
    assert all(f"v={runtime_version.asset_version}" in icon['src'] for icon in icons)


def test_service_worker_is_root_scoped_static_only_and_uncached(unauth_client):
    response = unauth_client.get('/service-worker.js')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/javascript')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['service-worker-allowed'] == '/'

    source = response.text
    assert runtime_version.asset_version in source
    assert runtime_version.deployment_id in source
    assert PWA_CACHE_PREFIX in source
    assert "request.method !== 'GET'" in source
    assert "request.headers.has('range')" in source
    assert "url.pathname.startsWith(CONFIG.staticPrefix)" in source
    assert "url.searchParams.get('v') !== CONFIG.assetVersion" in source
    assert "STATIC_CACHE" in source


def test_pwa_precache_contains_only_versioned_non_html_static_assets():
    urls = pwa_precache_urls()

    assert urls
    assert all(url.startswith('/static/') for url in urls)
    assert all(f"?v={runtime_version.asset_version}" in url for url in urls)
    assert not any(url.split('?', 1)[0].endswith('.html') for url in urls)
    assert any('/static/js/pwa.js?' in url for url in urls)
    assert any('/static/icons/pwa-192.png?' in url for url in urls)
    assert any('/static/icons/pwa-512.png?' in url for url in urls)


def test_pwa_icons_are_present():
    icons_dir = Path('app/static/icons')

    for filename in (
        'pwa-192.png',
        'pwa-512.png',
        'pwa-maskable-512.png',
        'apple-touch-icon.png',
    ):
        path = icons_dir / filename
        assert path.is_file()
        assert path.stat().st_size > 0


def test_account_ui_exposes_opt_in_pwa_installation():
    html = Path('app/static/index.html').read_text(encoding='utf-8')
    player_js = Path('app/static/js/player.js').read_text(encoding='utf-8')
    pwa_js = Path('app/static/js/pwa.js').read_text(encoding='utf-8')

    assert 'id="pwa-install-state"' in html
    assert 'id="btn-install-pwa"' in html
    assert 'id="btn-pwa-install-info"' in html
    assert 'id="pwa-install-help"' in html
    assert 'HTTPS requerido' in player_js
    assert 'simplitv:pwa-statechange' in player_js

    # Installation is strictly opt-in: suppress browser-driven install prompts
    # and only call prompt() from the explicit UI integration method.
    assert "window.addEventListener('beforeinstallprompt'" in pwa_js
    assert 'event.preventDefault();' in pwa_js
    assert 'async function promptInstall()' in pwa_js
    assert 'await promptEvent.prompt();' in pwa_js


def test_pwa_help_keeps_platform_instructions_out_of_default_markup():
    html = Path('app/static/index.html').read_text(encoding='utf-8')

    assert 'Añadir a pantalla de inicio' not in html
    assert 'iPhone o iPad' not in html
    assert 'pwa-install-help hidden' in html

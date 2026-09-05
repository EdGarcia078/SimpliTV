"""Progressive Web App metadata and service-worker generation for SimpliTV.

The PWA layer deliberately caches only public, content-versioned static assets.
Authenticated HTML, API responses and media streams always remain network-only,
so installing SimpliTV cannot persist private application data in Cache Storage.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import settings
from app.services.client_assets import STATIC_DIR, with_asset_version
from app.services.runtime_version import runtime_version


PWA_THEME_COLOR = "#08090d"
PWA_CACHE_PREFIX = "simplitv-static-"
_CACHEABLE_STATIC_SUFFIXES = {
    ".css",
    ".ico",
    ".js",
    ".png",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
}


def _versioned_static_url(path: str) -> str:
    return with_asset_version(f"/static/{path.lstrip('/')}")


def pwa_precache_urls(static_dir: Path = STATIC_DIR) -> tuple[str, ...]:
    """Return safe static assets that may be cached by the service worker.

    HTML is intentionally excluded. New CSS/JS/icons/fonts are picked up
    automatically, which keeps the PWA scalable without risking accidental
    caching of authenticated pages or API data.
    """
    if not static_dir.exists():
        return ()

    urls: list[str] = []
    for path in static_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _CACHEABLE_STATIC_SUFFIXES:
            continue
        relative = path.relative_to(static_dir).as_posix()
        urls.append(_versioned_static_url(relative))
    return tuple(sorted(urls))


def web_app_manifest() -> dict[str, object]:
    """Build the install manifest using the current static-asset fingerprint."""
    return {
        "id": "/",
        "name": settings.APP_NAME,
        "short_name": "SimpliTV",
        "description": "Televisión privada para tu biblioteca multimedia local.",
        "lang": "es",
        "dir": "ltr",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": PWA_THEME_COLOR,
        "theme_color": PWA_THEME_COLOR,
        "icons": [
            {
                "src": _versioned_static_url("icons/pwa-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": _versioned_static_url("icons/pwa-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": _versioned_static_url("icons/pwa-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }


def service_worker_script() -> str:
    """Build a deployment-aware service worker with a strict static-only cache."""
    config = json.dumps(
        {
            "assetVersion": runtime_version.asset_version,
            "cachePrefix": PWA_CACHE_PREFIX,
            "deploymentId": runtime_version.deployment_id,
            "staticPrefix": "/static/",
        },
        separators=(",", ":"),
    )
    precache = json.dumps(pwa_precache_urls(), separators=(",", ":"))

    return f"""'use strict';

const CONFIG = Object.freeze({config});
const PRECACHE_URLS = Object.freeze({precache});
const STATIC_CACHE = `${{CONFIG.cachePrefix}}${{CONFIG.assetVersion}}`;

self.addEventListener('install', (event) => {{
  event.waitUntil((async () => {{
    const cache = await caches.open(STATIC_CACHE);
    await cache.addAll(PRECACHE_URLS);
  }})());
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil((async () => {{
    const keys = await caches.keys();
    await Promise.all(keys.map((key) => {{
      if (key.startsWith(CONFIG.cachePrefix) && key !== STATIC_CACHE) {{
        return caches.delete(key);
      }}
      return Promise.resolve(false);
    }}));
    await self.clients.claim();
  }})());
}});

self.addEventListener('fetch', (event) => {{
  const request = event.request;
  if (request.method !== 'GET' || request.headers.has('range')) return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Security boundary: never intercept documents, /api/, media streams or any
  // future authenticated endpoint. Only current, fingerprinted /static/ assets
  // are eligible for Cache Storage.
  if (!url.pathname.startsWith(CONFIG.staticPrefix)) return;
  if (url.searchParams.get('v') !== CONFIG.assetVersion) return;

  event.respondWith((async () => {{
    const cache = await caches.open(STATIC_CACHE);
    const cached = await cache.match(request);
    if (cached) return cached;

    const response = await fetch(request);
    if (response.ok && response.type === 'basic') {{
      await cache.put(request, response.clone());
    }}
    return response;
  }})());
}});
"""

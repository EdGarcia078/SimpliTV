"""Versioned browser assets and HTML shells for SimpliTV clients."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi.responses import HTMLResponse

from app.services.runtime_version import runtime_version


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_STATIC_REFERENCE_RE = re.compile(
    r'(?P<prefix>\b(?:src|href)=["\'])(?P<url>/static/[^"\']+)(?P<suffix>["\'])'
)


def with_asset_version(url: str) -> str:
    """Add or replace the cache-busting version on a local static URL."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "v"
    ]
    query.append(("v", runtime_version.asset_version))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


@lru_cache(maxsize=8)
def render_client_page(filename: str) -> str:
    """Render one HTML shell with automatic deployment metadata and asset URLs."""
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    deployment_bootstrap = (
        f'<meta name="simplitv-deployment" content="{runtime_version.deployment_id}" />\n'
        '<script src="/static/js/version-watch.js" defer></script>\n'
    )

    if "</head>" in html:
        html = html.replace("</head>", f"  {deployment_bootstrap}</head>", 1)
    else:
        html = deployment_bootstrap + html

    return _STATIC_REFERENCE_RE.sub(
        lambda match: (
            f'{match.group("prefix")}'
            f'{with_asset_version(match.group("url"))}'
            f'{match.group("suffix")}'
        ),
        html,
    )


def client_page(filename: str) -> HTMLResponse:
    """Serve an HTML shell that browsers must revalidate on navigation."""
    return HTMLResponse(
        render_client_page(filename),
        headers={
            "Cache-Control": "no-cache, max-age=0, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def static_cache_control(requested_version: str | None) -> str:
    """Return the safe cache policy for a static asset request."""
    if requested_version == runtime_version.asset_version:
        return "public, max-age=31536000, immutable"
    return "no-cache, max-age=0, must-revalidate"

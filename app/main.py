from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlmodel import Session

from app.api.deps import get_current_user_optional
from app.api.router import api_router
from app.core.config import settings
from app.core.request_security import is_cross_site_browser_request
from app.db.session import create_db_and_tables, engine
from app.models.user import User
from app.services.channel import channel_engine
from app.services.client_assets import STATIC_DIR, client_page, static_cache_control
from app.services.runtime_version import runtime_version
from app.services.scanner import scan_library
from app.services.watcher import media_watcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize persistent state, channel state and the live library watcher."""
    create_db_and_tables()
    with Session(engine) as session:
        await scan_library(session)
        await channel_engine.initialize(session)

    media_watcher.start()
    yield
    media_watcher.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Canal de televisión privado para archivos multimedia locales.",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)

allowed_hosts = [host.strip() for host in settings.ALLOWED_HOSTS.split(",") if host.strip()]
if allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

# SimpliTV's browser client and API are intentionally same-origin. No permissive
# CORS middleware is installed: another website must not be able to make
# credentialed API requests to a private SimpliTV instance.

# API routes
app.include_router(api_router)

# Mount only the explicit static directory; project files, .git, the database and
# media remain outside this mount and cannot be fetched as arbitrary static files.
static_dir = STATIC_DIR
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "; ".join(
            [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data:",
                "media-src 'self' blob:",
                "connect-src 'self'",
                "font-src 'self'",
                "object-src 'none'",
                "base-uri 'none'",
                "form-action 'self'",
                "frame-ancestors 'none'",
            ]
        ),
    )
    if settings.ENABLE_HSTS:
        # HSTS is opt-in because plain HTTP on a private LAN is an officially
        # supported SimpliTV deployment mode.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000"
        )
    return response


@app.middleware("http")
async def security_policy(request: Request, call_next):
    """Apply browser defenses, basic request limits and same-origin CSRF checks."""
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                return _apply_security_headers(
                    JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content={"detail": "Content-Length inválido."},
                    )
                )
            if declared_size < 0 or declared_size > settings.MAX_REQUEST_BODY_BYTES:
                return _apply_security_headers(
                    JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": "La solicitud es demasiado grande."},
                    )
                )

        if is_cross_site_browser_request(request):
            return _apply_security_headers(
                JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Solicitud cross-site rechazada."},
                )
            )

    response = await call_next(request)

    # Use the ASGI scope path rather than reconstructing request.url. Besides being
    # cheaper, this avoids letting a malformed/hostile Host header influence path
    # decisions in framework versions affected by URL reconstruction bugs.
    request_path = str(request.scope.get("path") or "")

    # Private API responses should not be retained by intermediary/browser caches
    # unless an endpoint already chose a stricter/specialized policy.
    if request_path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")

    if request_path.startswith("/static/") and response.status_code == 200:
        response.headers["Cache-Control"] = static_cache_control(
            request.query_params.get("v")
        )

    return _apply_security_headers(response)


@app.get("/api/health", tags=["Health"], summary="Health check")
def health_check():
    """Minimal health/deployment identity used by the client version watcher."""
    return JSONResponse(
        {
            "status": "online",
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "deployment_id": runtime_version.deployment_id,
            "asset_version": runtime_version.asset_version,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/login", include_in_schema=False)
def serve_login(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the login page or continue an already authenticated session."""
    if user:
        if user.must_change_password:
            return RedirectResponse(url="/change-password")
        if user.role == "admin":
            return RedirectResponse(url="/admin")
        return RedirectResponse(url="/")
    return client_page("login.html")


@app.get("/change-password", include_in_schema=False)
def serve_required_password_change(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the first-run password replacement page for the seeded admin."""
    if not user:
        return RedirectResponse(url="/login")
    if not user.must_change_password:
        return RedirectResponse(url="/admin" if user.role == "admin" else "/")
    return client_page("change-password.html")


@app.get("/admin", include_in_schema=False)
def serve_admin(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the Admin panel for authenticated administrators."""
    if not user:
        return RedirectResponse(url="/login?next=/admin")
    if user.must_change_password:
        return RedirectResponse(url="/change-password")
    if user.role != "admin":
        return RedirectResponse(url="/")
    return client_page("admin.html")


@app.get("/", include_in_schema=False)
def serve_index(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the SimpliTV web player for authenticated users."""
    if not user:
        return RedirectResponse(url="/login")
    if user.must_change_password:
        return RedirectResponse(url="/change-password")
    return client_page("index.html")

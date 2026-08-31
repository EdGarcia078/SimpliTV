from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from fastapi import Cookie, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.api.deps import get_current_user_optional
from app.api.router import api_router
from app.core.config import settings
from app.core.security import SESSION_COOKIE_NAME
from app.db.session import create_db_and_tables, engine, get_session
from app.models.user import User
from app.services.scanner import scan_library
from app.services.channel import channel_engine
from app.services.watcher import media_watcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager:
    1. Initialize database tables.
    2. Perform initial media scan.
    3. Initialize ChannelEngine broadcast state.
    4. Start background dynamic filesystem watcher.
    On shutdown: stop watcher cleanly.
    """
    create_db_and_tables()
    with Session(engine) as session:
        await scan_library(session)
        await channel_engine.initialize(session)

    # Start live media watcher
    media_watcher.start()

    yield

    # Clean shutdown
    media_watcher.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Canal de televisión privado para archivos multimedia locales.",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for LAN access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(api_router)

# Mount static assets
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/api/health", tags=["Health"], summary="Health check")
def health_check():
    """Health check endpoint."""
    return {
        "status": "online",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@app.get("/login", include_in_schema=False)
def serve_login(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the login page. Redirects to TV or Admin if already authenticated."""
    if user:
        if user.role == "admin":
            return RedirectResponse(url="/admin")
        return RedirectResponse(url="/")
    return FileResponse(static_dir / "login.html")


@app.get("/admin", include_in_schema=False)
def serve_admin(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the Admin panel for authenticated administrators."""
    if not user:
        return RedirectResponse(url="/login?next=/admin")
    if user.role != "admin":
        return RedirectResponse(url="/")
    return FileResponse(static_dir / "admin.html")


@app.get("/", include_in_schema=False)
def serve_index(
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Serve the SimpliTV web player for authenticated users."""
    if not user:
        return RedirectResponse(url="/login")
    return FileResponse(static_dir / "index.html")

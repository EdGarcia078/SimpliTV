from fastapi import APIRouter, Depends
from sqlmodel import Session, select, func
from app.api.deps import get_current_admin
from app.db.session import get_session
from app.models.media import MediaItem, ScanResult
from app.models.user import User
from app.services.scanner import scan_library

router = APIRouter(prefix="/library", tags=["Library"], dependencies=[Depends(get_current_admin)])


@router.post("/scan", response_model=ScanResult, summary="Scan Media Directory")
async def trigger_scan(session: Session = Depends(get_session)) -> ScanResult:
    """
    Trigger a filesystem scan of the configured media directory.
    Updates the database with all discovered media items. (Admin only)
    """
    return await scan_library(session)


@router.get("/stats", summary="Library Statistics")
def get_library_stats(session: Session = Depends(get_session)):
    """Return summary statistics of the media library. (Admin only)"""
    total_items = session.exec(select(func.count(MediaItem.id))).one()
    total_episodes = session.exec(
        select(func.count(MediaItem.id)).where(MediaItem.media_type == "episode")
    ).one()
    total_movies = session.exec(
        select(func.count(MediaItem.id)).where(MediaItem.media_type == "movie")
    ).one()
    unique_series = len(
        session.exec(
            select(MediaItem.media_title)
            .where(MediaItem.media_type == "episode")
            .distinct()
        ).all()
    )
    total_duration = session.exec(select(func.sum(MediaItem.duration))).one() or 0.0

    return {
        # Historical key kept as total library items for API compatibility.
        "total_episodes": total_items,
        "series_episodes": total_episodes,
        "total_movies": total_movies,
        "unique_series": unique_series,
        "total_duration_seconds": round(total_duration, 2),
        "total_duration_hours": round(total_duration / 3600, 2),
    }

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from app.api.deps import get_current_admin
from app.api.media import to_media_item_read
from app.core.security import hash_password
from app.db.session import get_session
from app.models.channel import Channel, ChannelRead
from app.models.access import (AccessGroup, AccessGroupCreate, AccessGroupRead, AccessGroupUpdate, GroupChannelAccess, UserAccessGroup)
from app.models.media import MediaItem, MediaItemRead
from app.models.preferences import UserBlockedChannel, UserPreference
from app.models.user import (
    PasswordResetRequest,
    User,
    UserCreate,
    UserRead,
    UserSession,
    UserUpdate,
)
from app.services.channel import channel_engine
from app.services.access import (
    ensure_group_membership_removal_is_safe,
    group_to_read,
    replace_group_channels,
    replace_group_memberships,
)
from app.services.optimization import OptimizationProfile, optimization_manager
from app.services.normalization import normalization_manager
from app.services.media_config import (
    CHANNEL_CONFIG_FILENAME,
    CONFIG_VERSION,
    ChannelConfig,
    FranchiseConfig,
    SeriesConfig,
    get_channel_dir,
    get_franchise_dir_from_relative_path,
    get_loose_movie_relative_path,
    get_series_dir_from_relative_path,
    load_channel_config,
    load_franchise_config,
    load_series_config,
    save_channel_config,
    save_franchise_config,
    save_series_config,
)


router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(get_current_admin)],
)


# ==========================================================
# SCHEMAS ADMINISTRATIVOS DE CANALES
# ==========================================================

class ChannelSettingsUpdate(BaseModel):
    batch_size: int = Field(ge=1, le=100)
    start_mode: Literal["any", "even", "odd"]
    loop: bool


class SeriesPortableConfigUpdate(BaseModel):
    relative_dir: str = Field(min_length=1, max_length=500)
    config: SeriesConfig


class FranchisePortableConfigUpdate(BaseModel):
    relative_dir: str = Field(min_length=1, max_length=500)
    config: FranchiseConfig


def _get_admin_channel_or_404(session: Session, channel_id: int) -> Channel:
    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Canal no encontrado.",
        )
    return channel


def _channel_config_dir_or_404(channel: Channel):
    channel_dir = get_channel_dir(channel.folder_name, channel.name)
    if not channel_dir.exists() or not channel_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La carpeta física del canal no existe.",
        )
    return channel_dir


def _relative_config_target(channel_dir, relative_dir: str, kind: str):
    """Resolve only UI-discovered series/franchise directories inside a channel."""
    clean = relative_dir.strip().replace("\\", "/")
    relative = Path(clean)
    if not clean or relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="Ruta de configuración inválida.")

    parts = relative.parts
    if kind == "series":
        valid = (
            len(parts) == 2 and parts[0].casefold() == "series"
        ) or (
            len(parts) == 1 and parts[0].casefold() not in {"series", "movies"}
        )
    else:
        valid = len(parts) == 2 and parts[0].casefold() == "movies"

    if not valid:
        raise HTTPException(status_code=400, detail="Ruta de configuración inválida.")

    root = channel_dir.resolve()
    target = (channel_dir / relative).resolve()
    if target == root or root not in target.parents or not target.is_dir():
        raise HTTPException(status_code=404, detail="El contenido solicitado ya no existe.")
    return target


def _portable_channel_configuration(session: Session, channel: Channel) -> dict:
    channel_dir = _channel_config_dir_or_404(channel)
    channel_config = load_channel_config(channel_dir)

    indexed_items = list(
        session.exec(select(MediaItem).where(MediaItem.channel_id == channel.id)).all()
    )
    series_counts: dict[str, int] = {}
    franchise_counts: dict[str, int] = {}
    loose_movie_items: list[dict] = []

    for item in indexed_items:
        if item.media_type == "movie":
            franchise_dir = get_franchise_dir_from_relative_path(
                item.relative_path, item.media_type
            )
            if franchise_dir is not None and franchise_dir.exists():
                try:
                    rel = franchise_dir.relative_to(channel_dir).as_posix()
                except ValueError:
                    continue
                franchise_counts[rel] = franchise_counts.get(rel, 0) + 1
            else:
                loose_rel = get_loose_movie_relative_path(item.relative_path, item.media_type)
                if loose_rel is not None:
                    loose_movie_items.append({
                        "relative_path": loose_rel,
                        "name": item.media_title,
                        "weight": int(channel_config.loose_movie_weights.get(loose_rel, 1)),
                    })
            continue

        series_dir = get_series_dir_from_relative_path(
            item.relative_path, item.media_type
        )
        if series_dir is not None and series_dir.exists():
            try:
                rel = series_dir.relative_to(channel_dir).as_posix()
            except ValueError:
                continue
            series_counts[rel] = series_counts.get(rel, 0) + 1

    children = [p for p in channel_dir.iterdir() if p.is_dir()]
    canonical_series_root = next(
        (p for p in children if p.name.casefold() == "series"), None
    )
    canonical_movies_root = next(
        (p for p in children if p.name.casefold() == "movies"), None
    )

    series_dirs = []
    if canonical_series_root is not None:
        series_dirs.extend(p for p in canonical_series_root.iterdir() if p.is_dir())
    series_dirs.extend(
        p for p in children if p.name.casefold() not in {"series", "movies"}
    )

    franchise_dirs = []
    if canonical_movies_root is not None:
        franchise_dirs.extend(p for p in canonical_movies_root.iterdir() if p.is_dir())

    series_payload = []
    seen = set()
    for series_dir in sorted(series_dirs, key=lambda path: path.name.casefold()):
        resolved = series_dir.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        rel = series_dir.relative_to(channel_dir).as_posix()
        cfg = load_series_config(series_dir)
        series_payload.append({
            "relative_dir": rel,
            "name": series_dir.name,
            "episode_count": series_counts.get(rel, 0),
            "config": cfg.model_dump(mode="json"),
        })

    franchise_payload = []
    for franchise_dir in sorted(franchise_dirs, key=lambda path: path.name.casefold()):
        rel = franchise_dir.relative_to(channel_dir).as_posix()
        cfg = load_franchise_config(franchise_dir)
        franchise_payload.append({
            "relative_dir": rel,
            "folder_name": franchise_dir.name,
            "name": cfg.name.strip() or franchise_dir.name,
            "movie_count": franchise_counts.get(rel, 0),
            "config": cfg.model_dump(mode="json"),
        })

    loose_movie_items.sort(key=lambda item: item["name"].casefold())
    return {
        "channel_id": channel.id,
        "folder_name": channel.folder_name or channel.name,
        "channel": channel_config.model_dump(mode="json"),
        "series": series_payload,
        "franchises": franchise_payload,
        # Kept for backwards-compatible clients while the new payload exposes
        # each standalone movie so the admin can configure weights and filters.
        "loose_movies": len(loose_movie_items),
        "loose_movie_items": loose_movie_items,
    }


# ==========================================================
# 1. USER MANAGEMENT
# ==========================================================

@router.get("/users", response_model=List[UserRead], summary="List Users")
def list_users(session: Session = Depends(get_session)) -> List[UserRead]:
    """List all registered users."""
    users = session.exec(select(User).order_by(User.created_at.desc())).all()
    return [
        UserRead(
            id=u.id,  # type: ignore[arg-type]
            username=u.username,
            role=u.role,
            is_active=u.is_active,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
        )
        for u in users
    ]


@router.post(
    "/users",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create User",
)
def create_user(
    user_in: UserCreate,
    session: Session = Depends(get_session),
) -> UserRead:
    """Create a new user account."""
    username = user_in.username.strip()

    if len(username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El nombre de usuario debe tener al menos 3 caracteres.",
        )

    if len(user_in.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La contraseña debe tener al menos 6 caracteres.",
        )

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El nombre de usuario '{username}' ya está en uso.",
        )

    role = "admin" if user_in.role == "admin" else "user"

    access_group = None
    if role == "user":
        if user_in.group_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Todo espectador debe pertenecer a un grupo de acceso.",
            )
        access_group = session.get(AccessGroup, user_in.group_id)
        if access_group is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El grupo de acceso seleccionado no existe.",
            )

    new_user = User(
        username=username,
        password_hash=hash_password(user_in.password),
        role=role,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(new_user)
    session.flush()
    if access_group is not None:
        session.add(UserAccessGroup(user_id=new_user.id, group_id=access_group.id))
    session.commit()
    session.refresh(new_user)

    return UserRead(
        id=new_user.id,  # type: ignore[arg-type]
        username=new_user.username,
        role=new_user.role,
        is_active=new_user.is_active,
        created_at=new_user.created_at,
        last_login_at=new_user.last_login_at,
    )


@router.patch(
    "/users/{user_id}",
    response_model=UserRead,
    summary="Update User Status or Role",
)
def update_user(
    user_id: int,
    user_update: UserUpdate,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> UserRead:
    """Update role or active status of a user."""
    target_user = session.get(User, user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    if target_user.id == admin.id:
        if user_update.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No puedes desactivar tu propia cuenta de administrador.",
            )
        if user_update.role and user_update.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No puedes quitarte el rol de administrador a ti mismo.",
            )

    if user_update.role in ["user", "admin"]:
        if user_update.role == "user" and target_user.role != "user":
            membership = session.exec(
                select(UserAccessGroup).where(UserAccessGroup.user_id == target_user.id)
            ).first()
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "No puedes convertir esta cuenta en espectador hasta asignarla "
                        "a por lo menos un grupo de acceso."
                    ),
                )
        target_user.role = user_update.role

    if user_update.is_active is not None:
        target_user.is_active = user_update.is_active

        if not user_update.is_active:
            active_sessions = session.exec(
                select(UserSession).where(UserSession.user_id == target_user.id)
            ).all()
            for active_session in active_sessions:
                session.delete(active_session)

    session.add(target_user)
    session.commit()
    session.refresh(target_user)

    return UserRead(
        id=target_user.id,  # type: ignore[arg-type]
        username=target_user.username,
        role=target_user.role,
        is_active=target_user.is_active,
        created_at=target_user.created_at,
        last_login_at=target_user.last_login_at,
    )


@router.post("/users/{user_id}/reset-password", summary="Reset User Password")
def reset_password(
    user_id: int,
    req: PasswordResetRequest,
    session: Session = Depends(get_session),
):
    """Reset password for a user."""
    if len(req.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña debe tener al menos 6 caracteres.",
        )

    target_user = session.get(User, user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    target_user.password_hash = hash_password(req.new_password)
    session.add(target_user)

    active_sessions = session.exec(
        select(UserSession).where(UserSession.user_id == target_user.id)
    ).all()
    for active_session in active_sessions:
        session.delete(active_session)

    session.commit()
    return {
        "message": f"Contraseña restablecida para el usuario '{target_user.username}'."
    }


@router.delete("/users/{user_id}", summary="Delete User")
def delete_user(
    user_id: int,
    admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
):
    """Delete a user account."""
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No puedes eliminar tu propia cuenta de administrador.",
        )

    target_user = session.get(User, user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )

    active_sessions = session.exec(
        select(UserSession).where(UserSession.user_id == target_user.id)
    ).all()
    for active_session in active_sessions:
        session.delete(active_session)

    memberships = session.exec(
        select(UserAccessGroup).where(UserAccessGroup.user_id == target_user.id)
    ).all()
    for membership in memberships:
        session.delete(membership)

    blocked_channels = session.exec(
        select(UserBlockedChannel).where(UserBlockedChannel.user_id == target_user.id)
    ).all()
    for blocked in blocked_channels:
        session.delete(blocked)
    preference = session.get(UserPreference, target_user.id)
    if preference:
        session.delete(preference)

    session.delete(target_user)
    session.commit()

    return {
        "message": f"Usuario '{target_user.username}' eliminado exitosamente."
    }


# ==========================================================
# 1.5 ACCESS GROUP MANAGEMENT
# ==========================================================

@router.get("/groups", response_model=List[AccessGroupRead], summary="List Access Groups")
def list_access_groups(session: Session = Depends(get_session)) -> List[AccessGroupRead]:
    groups = session.exec(select(AccessGroup).order_by(AccessGroup.name)).all()
    return [group_to_read(session, group) for group in groups]


@router.post("/groups", response_model=AccessGroupRead, status_code=status.HTTP_201_CREATED, summary="Create Access Group")
def create_access_group(
    group_in: AccessGroupCreate,
    session: Session = Depends(get_session),
) -> AccessGroupRead:
    name = group_in.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre del grupo no puede estar vacío.")
    existing = session.exec(select(AccessGroup).where(AccessGroup.name == name)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Ya existe un grupo llamado '{name}'.")

    group = AccessGroup(name=name)
    session.add(group)
    session.flush()
    replace_group_memberships(session, group.id, group_in.user_ids)
    replace_group_channels(session, group.id, group_in.channel_ids)
    session.commit()
    session.refresh(group)
    return group_to_read(session, group)


@router.patch("/groups/{group_id}", response_model=AccessGroupRead, summary="Update Access Group")
def update_access_group(
    group_id: int,
    group_in: AccessGroupUpdate,
    session: Session = Depends(get_session),
) -> AccessGroupRead:
    group = session.get(AccessGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado.")

    if group_in.name is not None:
        name = group_in.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="El nombre del grupo no puede estar vacío.")
        duplicate = session.exec(
            select(AccessGroup).where(AccessGroup.name == name, AccessGroup.id != group_id)
        ).first()
        if duplicate:
            raise HTTPException(status_code=400, detail=f"Ya existe un grupo llamado '{name}'.")
        group.name = name
        session.add(group)

    if group_in.user_ids is not None:
        replace_group_memberships(session, group_id, group_in.user_ids)
    if group_in.channel_ids is not None:
        replace_group_channels(session, group_id, group_in.channel_ids)

    session.commit()
    session.refresh(group)
    return group_to_read(session, group)


@router.delete("/groups/{group_id}", summary="Delete Access Group")
def delete_access_group(group_id: int, session: Session = Depends(get_session)):
    group = session.get(AccessGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado.")

    memberships = session.exec(
        select(UserAccessGroup).where(UserAccessGroup.group_id == group_id)
    ).all()
    ensure_group_membership_removal_is_safe(
        session, group_id, {membership.user_id for membership in memberships}
    )
    for membership in memberships:
        session.delete(membership)
    for grant in session.exec(select(GroupChannelAccess).where(GroupChannelAccess.group_id == group_id)).all():
        session.delete(grant)
    session.delete(group)
    session.commit()
    return {"message": f"Grupo '{group.name}' eliminado exitosamente."}


# ==========================================================
# 2. STATS & LIBRARY DASHBOARD
# ==========================================================

@router.get("/stats", summary="Admin Dashboard Statistics")
def get_admin_stats(session: Session = Depends(get_session)):
    """
    Estadísticas generales del sistema.

    El estado de emisión de cada canal se obtiene por separado mediante:
    GET /api/channels/{channel_id}/now-playing
    """
    total_items = session.exec(select(func.count(MediaItem.id))).one() or 0
    series_episodes = session.exec(
        select(func.count(MediaItem.id)).where(MediaItem.media_type == "episode")
    ).one() or 0
    total_movies = session.exec(
        select(func.count(MediaItem.id)).where(MediaItem.media_type == "movie")
    ).one() or 0
    unique_series = len(
        session.exec(
            select(MediaItem.media_title)
            .where(MediaItem.media_type == "episode")
            .distinct()
        ).all()
    )
    total_duration = session.exec(select(func.sum(MediaItem.duration))).one() or 0.0

    total_users = session.exec(select(func.count(User.id))).one() or 0
    active_users = session.exec(
        select(func.count(User.id)).where(User.is_active == True)  # noqa: E712
    ).one() or 0

    return {
        "media": {
            "total_episodes": total_items,
            "series_episodes": series_episodes,
            "total_movies": total_movies,
            "unique_series": unique_series,
            "total_duration_seconds": round(total_duration, 2),
            "total_duration_hours": round(total_duration / 3600, 2),
        },
        "users": {
            "total_users": total_users,
            "active_users": active_users,
        },
    }


@router.get(
    "/library",
    response_model=List[MediaItemRead],
    summary="Browse Media Library",
)
def browse_library(
    q: Optional[str] = Query(None, description="Search term for series, movie, franchise or episode title"),
    session: Session = Depends(get_session),
) -> List[MediaItemRead]:
    """Inspect all indexed media in the library with search capability."""
    stmt = select(MediaItem).order_by(
        MediaItem.media_title,
        MediaItem.season_number,
        MediaItem.episode_number,
    )
    episodes = session.exec(stmt).all()

    if q:
        query_lower = q.lower().strip()
        episodes = [
            ep
            for ep in episodes
            if query_lower in ep.media_title.lower()
            or (ep.franchise and query_lower in ep.franchise.lower())
            or (
                ep.episode_title
                and query_lower in ep.episode_title.lower()
            )
        ]

    return [to_media_item_read(ep) for ep in episodes]


# ==========================================================
# 2.5 LIBRARY OPTIMIZATION
# ==========================================================


class OptimizationProfileUpdate(BaseModel):
    resolution: Literal["1080p", "720p", "480p"]
    crf: int = Field(ge=18, le=30)
    bitrate_1080_mbps: float = Field(gt=0.0, le=20.0)
    bitrate_720_mbps: float = Field(gt=0.0, le=12.0)
    bitrate_sd_mbps: float = Field(gt=0.0, le=8.0)
    confirm_resolution_loss: bool = False


def _profile_api_data(profile: OptimizationProfile) -> dict:
    resolution = "1080p"
    if profile.max_height <= 480:
        resolution = "480p"
    elif profile.max_height <= 720:
        resolution = "720p"
    return {
        "resolution": resolution,
        "max_width": profile.max_width,
        "max_height": profile.max_height,
        "crf": profile.crf,
        "preset": profile.preset,
        "bitrate_1080_mbps": round(profile.target_video_bitrates["1080p"] / 1_000_000, 2),
        "bitrate_720_mbps": round(profile.target_video_bitrates["720p"] / 1_000_000, 2),
        "bitrate_sd_mbps": round(profile.target_video_bitrates["sd"] / 1_000_000, 2),
        "min_savings_percent": round(profile.min_savings_ratio * 100, 1),
    }


@router.get("/library/optimization/profile", summary="Get Optimization Profile")
def get_library_optimization_profile():
    return _profile_api_data(optimization_manager.profile)


@router.put("/library/optimization/profile", summary="Update Optimization Profile")
def update_library_optimization_profile(req: OptimizationProfileUpdate):
    dimensions = {
        "1080p": (1920, 1080),
        "720p": (1280, 720),
        "480p": (854, 480),
    }
    max_width, max_height = dimensions[req.resolution]
    current = optimization_manager.profile

    if max_height < current.max_height and not req.confirm_resolution_loss:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Reducir la resolución máxima puede reemplazar archivos de mayor resolución "
                "por versiones reducidas. Sin una copia externa del original, esta pérdida es irreversible."
            ),
        )

    profile = OptimizationProfile(
        video_codec="hevc",
        max_width=max_width,
        max_height=max_height,
        crf=req.crf,
        preset=current.preset,
        min_savings_ratio=current.min_savings_ratio,
        target_video_bitrates={
            "1080p": int(req.bitrate_1080_mbps * 1_000_000),
            "720p": int(req.bitrate_720_mbps * 1_000_000),
            "sd": int(req.bitrate_sd_mbps * 1_000_000),
        },
        assumed_audio_bitrate=current.assumed_audio_bitrate,
    )
    try:
        updated = optimization_manager.update_profile(profile)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _profile_api_data(updated)

@router.post("/library/normalization/analyze", summary="Analyze MP4 Normalization")
async def analyze_library_normalization():
    """Analyze which media files need conversion to the canonical MP4 format."""
    analysis = await normalization_manager.analyze()
    return analysis.to_dict()


@router.get("/library/normalization/analysis", summary="Get Latest MP4 Normalization Analysis")
def get_latest_library_normalization_analysis():
    analysis = normalization_manager.latest_analysis
    if analysis is None:
        raise HTTPException(status_code=404, detail="Aún no se ha analizado la conversión a MP4.")
    return analysis.to_dict()


@router.post("/library/normalization", status_code=status.HTTP_202_ACCEPTED, summary="Normalize Library to MP4")
async def start_library_normalization():
    if optimization_manager.get_active_job() is not None:
        raise HTTPException(status_code=409, detail="Hay una optimización en ejecución. Espera a que termine antes de convertir a MP4.")
    try:
        job = await normalization_manager.create_job()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/library/normalization/active", summary="Get Active MP4 Normalization Job")
def get_active_library_normalization_job():
    job = normalization_manager.get_active_job()
    return job.to_dict() if job is not None else None


@router.get("/library/normalization/{job_id}", summary="MP4 Normalization Job Progress")
def get_library_normalization_job(job_id: int):
    job = normalization_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Trabajo de conversión no encontrado.")
    return job.to_dict()


@router.post("/library/normalization/{job_id}/stop", summary="Stop MP4 Normalization Job")
async def stop_library_normalization_job(job_id: int):
    try:
        job = await normalization_manager.cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Trabajo de conversión no encontrado.") from exc
    return job.to_dict()


@router.post("/library/optimization/analyze", summary="Analyze Library Optimization")
async def analyze_library_optimization():
    """Read-only FFprobe scan. No optimization state is written to SQLite."""
    analysis = await optimization_manager.analyze()
    return analysis.to_dict()


@router.get("/library/optimization/analysis", summary="Get Latest Optimization Analysis")
def get_latest_library_optimization_analysis():
    analysis = optimization_manager.latest_analysis
    if analysis is None:
        raise HTTPException(status_code=404, detail="Aún no se ha analizado la biblioteca.")
    return analysis.to_dict()


@router.post("/library/optimization", status_code=status.HTTP_202_ACCEPTED, summary="Optimize Library")
async def start_library_optimization():
    """Start a single in-memory background optimization job."""
    if normalization_manager.get_active_job() is not None:
        raise HTTPException(status_code=409, detail="Hay una conversión a MP4 en ejecución. Espera a que termine antes de optimizar.")
    try:
        job = await optimization_manager.create_job()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/library/optimization/active", summary="Get Active Optimization Job")
def get_active_library_optimization_job():
    """Return the queued/running optimization job so the admin UI can reconnect to it."""
    job = optimization_manager.get_active_job()
    return job.to_dict() if job is not None else None


@router.get("/library/optimization/{job_id}", summary="Optimization Job Progress")
def get_library_optimization_job(job_id: int):
    job = optimization_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Trabajo de optimización no encontrado.")
    return job.to_dict()


@router.post("/library/optimization/{job_id}/stop", summary="Stop Optimization Job")
async def stop_library_optimization_job(job_id: int):
    try:
        job = await optimization_manager.cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Trabajo de optimización no encontrado.") from exc
    return job.to_dict()


# ==========================================================
# 3. CHANNEL ADMINISTRATION
# ==========================================================

@router.get("/channels", response_model=List[ChannelRead], summary="List All Channels for Admin")
def list_admin_channels(session: Session = Depends(get_session)) -> List[ChannelRead]:
    """Return every channel for administration, independent of viewer filters."""
    channels = session.exec(select(Channel).order_by(Channel.id)).all()
    result: list[ChannelRead] = []
    for channel in channels:
        channel_dir = get_channel_dir(channel.folder_name, channel.name)
        if channel_dir.exists():
            config = load_channel_config(channel_dir)
            result.append(ChannelRead(
                id=channel.id,  # type: ignore[arg-type]
                name=channel.name,
                folder_name=channel.folder_name,
                batch_size=channel.batch_size,
                start_mode=channel.start_mode,
                loop=channel.loop,
                config_source="channel.yaml",
                schedule_default=list(config.schedule.default),
                schedule_slots=len(config.schedule.slots),
                sensitive_content=config.sensitive_content,
            ))
        else:
            result.append(ChannelRead.model_validate(channel))
    return result

@router.get(
    "/channels/{channel_id}/now-playing",
    summary="Read Any Channel State for Admin",
)
async def admin_channel_now_playing(
    channel_id: int,
    session: Session = Depends(get_session),
):
    channel = _get_admin_channel_or_404(session, channel_id)
    state = await channel_engine.get_current_state(session, channel_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El canal '{channel.name}' no tiene una emisión activa.",
        )
    return state


@router.post(
    "/channels/{channel_id}/skip",
    summary="Administrative Skip MediaItem",
)
async def admin_skip_episode(
    channel_id: int,
    session: Session = Depends(get_session),
):
    """
    Avanza inmediatamente al siguiente episodio del canal indicado.

    Ruta final:
    POST /api/admin/channels/{channel_id}/skip
    """
    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Canal no encontrado.",
        )

    current_state = await channel_engine.get_current_state(session, channel_id)
    if not current_state:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El canal '{channel.name}' no tiene una emisión activa para saltar.",
        )

    await channel_engine.skip_episode(session, channel_id)
    new_state = await channel_engine.get_current_state(session, channel_id)

    if not new_state:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No fue posible iniciar el siguiente episodio del canal '{channel.name}'.",
        )

    return {
        "message": "Episodio avanzado exitosamente.",
        "state": new_state,
    }


@router.get(
    "/channels/{channel_id}/configuration",
    summary="Read Portable Channel Configuration",
)
def get_portable_channel_configuration(
    channel_id: int,
    session: Session = Depends(get_session),
):
    """Return the YAML-backed channel, series and franchise configuration."""
    channel = _get_admin_channel_or_404(session, channel_id)
    return _portable_channel_configuration(session, channel)


@router.put(
    "/channels/{channel_id}/configuration",
    summary="Save Portable Channel Configuration",
)
async def save_portable_channel_configuration(
    channel_id: int,
    config: ChannelConfig,
    session: Session = Depends(get_session),
):
    channel = _get_admin_channel_or_404(session, channel_id)
    channel_dir = _channel_config_dir_or_404(channel)

    if config.version != CONFIG_VERSION:
        raise HTTPException(status_code=400, detail="Versión de configuración no compatible.")

    new_name = config.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="El nombre del canal no puede estar vacío.")
    config.name = new_name

    name_conflict = session.exec(
        select(Channel).where(Channel.name == new_name, Channel.id != channel.id)
    ).first()
    if name_conflict:
        raise HTTPException(status_code=409, detail="Ya existe otro canal con ese nombre.")

    save_channel_config(channel_dir, config)
    if channel.name != new_name:
        channel.name = new_name
        session.add(channel)
        session.commit()
        session.refresh(channel)

    await channel_engine.refresh_channel_schedule(session, channel_id)
    return _portable_channel_configuration(session, channel)


@router.put(
    "/channels/{channel_id}/series/configuration",
    summary="Save Portable Series Configuration",
)
async def save_portable_series_configuration(
    channel_id: int,
    payload: SeriesPortableConfigUpdate,
    session: Session = Depends(get_session),
):
    channel = _get_admin_channel_or_404(session, channel_id)
    channel_dir = _channel_config_dir_or_404(channel)
    if payload.config.version != CONFIG_VERSION:
        raise HTTPException(status_code=400, detail="Versión de configuración no compatible.")

    series_dir = _relative_config_target(channel_dir, payload.relative_dir, "series")
    save_series_config(series_dir, payload.config)
    await channel_engine.refresh_channel_schedule(session, channel_id)
    return _portable_channel_configuration(session, channel)


@router.put(
    "/channels/{channel_id}/franchises/configuration",
    summary="Save Portable Movie Franchise Configuration",
)
async def save_portable_franchise_configuration(
    channel_id: int,
    payload: FranchisePortableConfigUpdate,
    session: Session = Depends(get_session),
):
    channel = _get_admin_channel_or_404(session, channel_id)
    channel_dir = _channel_config_dir_or_404(channel)
    if payload.config.version != CONFIG_VERSION:
        raise HTTPException(status_code=400, detail="Versión de configuración no compatible.")

    franchise_name = payload.config.name.strip()
    if not franchise_name:
        raise HTTPException(status_code=400, detail="El nombre de la franquicia no puede estar vacío.")
    payload.config.name = franchise_name

    franchise_dir = _relative_config_target(channel_dir, payload.relative_dir, "franchise")
    save_franchise_config(franchise_dir, payload.config)

    # Keep the searchable index/presentation in sync immediately; the YAML remains
    # the source of truth and a future scan will derive the same value again.
    prefix = f"{channel.folder_name or channel.name}/{payload.relative_dir.strip().replace('\\', '/').rstrip('/')}/"
    movies = session.exec(
        select(MediaItem).where(
            MediaItem.channel_id == channel.id,
            MediaItem.media_type == "movie",
        )
    ).all()
    changed = False
    for movie in movies:
        if movie.relative_path.startswith(prefix) and movie.franchise != franchise_name:
            movie.franchise = franchise_name
            session.add(movie)
            changed = True
    if changed:
        session.commit()

    await channel_engine.refresh_channel_schedule(session, channel_id)
    return _portable_channel_configuration(session, channel)


@router.patch(
    "/channels/{channel_id}/settings",
    response_model=ChannelRead,
    summary="Update Channel Settings",
)
async def update_channel_settings(
    channel_id: int,
    settings: ChannelSettingsUpdate,
    session: Session = Depends(get_session),
) -> ChannelRead:
    """
    Actualiza la configuración de programación de un canal.

    Ruta final:
    PATCH /api/admin/channels/{channel_id}/settings
    """
    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Canal no encontrado.",
        )

    channel_dir = get_channel_dir(channel.folder_name, channel.name)
    if (channel_dir / CHANNEL_CONFIG_FILENAME).exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Este canal usa configuración portable. Adminístralo desde la "
                "configuración del canal en el panel; los ajustes heredados de "
                "SQLite no se aplican a canales portables."
            ),
        )

    channel.batch_size = settings.batch_size
    channel.start_mode = settings.start_mode
    channel.loop = settings.loop

    session.add(channel)
    session.commit()
    session.refresh(channel)

    # The engine keeps the next episode pre-scheduled in memory. Recalculate
    # it immediately so a batch_size/loop/start_mode change takes effect
    # on the very next transition instead of one episode later.
    await channel_engine.refresh_channel_schedule(session, channel_id)

    return ChannelRead.model_validate(channel)

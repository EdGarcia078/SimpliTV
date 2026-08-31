from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.access import AccessGroup, GroupChannelAccess, UserAccessGroup
from app.models.channel import Channel
from app.models.media import MediaItem
from app.models.preferences import UserBlockedChannel, UserPreference
from app.models.user import User
from app.services.media_config import get_channel_dir, load_channel_config


def get_accessible_channel_ids(session: Session, user: User) -> set[int] | None:
    """Return channel IDs granted by access groups. ``None`` means unrestricted.

    This is the base authorization layer only. Viewer preferences (blocked channels
    and sensitive-content mode) are applied separately by
    :func:`get_player_channel_ids` and :func:`require_channel_access`.
    """
    if user.role == "admin":
        return None

    memberships = session.exec(
        select(UserAccessGroup).where(UserAccessGroup.user_id == user.id)
    ).all()
    if not memberships:
        # Viewers must belong to at least one access group. A legacy or otherwise
        # inconsistent viewer without memberships gets no channel access rather
        # than falling back to unrestricted access.
        return set()

    group_ids = [membership.group_id for membership in memberships]
    grants = session.exec(
        select(GroupChannelAccess).where(GroupChannelAccess.group_id.in_(group_ids))
    ).all()
    return {grant.channel_id for grant in grants}


def get_or_create_user_preference(session: Session, user: User) -> UserPreference:
    preference = session.get(UserPreference, user.id)
    if preference is None:
        preference = UserPreference(user_id=user.id, sensitive_content_enabled=False)
        session.add(preference)
        session.commit()
        session.refresh(preference)
    return preference


def get_user_sensitive_mode(session: Session, user: User) -> bool:
    preference = session.get(UserPreference, user.id)
    return bool(preference and preference.sensitive_content_enabled)


def get_user_blocked_channel_ids(session: Session, user: User) -> set[int]:
    rows = session.exec(
        select(UserBlockedChannel).where(UserBlockedChannel.user_id == user.id)
    ).all()
    return {row.channel_id for row in rows}


def channel_is_sensitive(channel: Channel) -> bool:
    """Read the portable sensitive-content flag for a real channel folder."""
    channel_dir = get_channel_dir(channel.folder_name, channel.name)
    if not channel_dir.exists() or not channel_dir.is_dir():
        # Synthetic/tests/legacy rows without a backing folder are treated as
        # normal content rather than disappearing unexpectedly.
        return False
    return bool(load_channel_config(channel_dir).sensitive_content)


def get_group_channel_ids_expanded(session: Session, user: User) -> set[int]:
    """Return concrete IDs granted by groups, expanding unrestricted access."""
    allowed = get_accessible_channel_ids(session, user)
    if allowed is not None:
        return set(allowed)
    return set(session.exec(select(Channel.id)).all())


def get_player_channel_ids(session: Session, user: User) -> set[int]:
    """Return channels that may actually appear/be selected in the player."""
    allowed = get_group_channel_ids_expanded(session, user)
    if not allowed:
        return set()

    blocked = get_user_blocked_channel_ids(session, user)
    visible = allowed - blocked
    if not visible:
        return set()

    if get_user_sensitive_mode(session, user):
        return visible

    channels = session.exec(select(Channel).where(Channel.id.in_(visible))).all()
    sensitive_ids = {channel.id for channel in channels if channel_is_sensitive(channel)}
    return visible - sensitive_ids


def user_can_access_channel(session: Session, user: User, channel_id: int) -> bool:
    """Apply group grants, user blocks, and sensitive-content preference."""
    base = get_accessible_channel_ids(session, user)
    if base is not None and channel_id not in base:
        return False

    blocked = session.get(UserBlockedChannel, (user.id, channel_id))
    if blocked is not None:
        return False

    channel = session.get(Channel, channel_id)
    if channel is None:
        # Preserve the API's normal 404 behaviour for IDs that do not exist.
        # Authorization has already rejected explicit group/block restrictions.
        return True

    if channel_is_sensitive(channel) and not get_user_sensitive_mode(session, user):
        return False

    return True


def require_channel_access(session: Session, user: User, channel_id: int) -> None:
    if not user_can_access_channel(session, user, channel_id):
        # Intentionally generic: do not disclose whether the channel is outside a
        # group, blocked by the user, or hidden by sensitive-content settings.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes acceso a este canal.",
        )


def require_episode_access(session: Session, user: User, episode: MediaItem) -> None:
    require_channel_access(session, user, episode.channel_id)


def replace_user_blocked_channels(
    session: Session,
    user: User,
    channel_ids: list[int],
) -> None:
    """Replace the user's personal block list, constrained to group-granted IDs."""
    requested = set(channel_ids)
    granted = get_group_channel_ids_expanded(session, user)
    invalid = requested - granted
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uno o más canales no están disponibles para tu usuario.",
        )

    old = session.exec(
        select(UserBlockedChannel).where(UserBlockedChannel.user_id == user.id)
    ).all()
    for row in old:
        session.delete(row)
    for channel_id in requested:
        session.add(UserBlockedChannel(user_id=user.id, channel_id=channel_id))


def _viewer_has_other_group(session: Session, user_id: int, excluded_group_id: int) -> bool:
    return session.exec(
        select(UserAccessGroup).where(
            UserAccessGroup.user_id == user_id,
            UserAccessGroup.group_id != excluded_group_id,
        )
    ).first() is not None


def ensure_group_membership_removal_is_safe(
    session: Session,
    group_id: int,
    user_ids: set[int],
) -> None:
    """Prevent any viewer from being left without an access group."""
    if not user_ids:
        return

    users = session.exec(select(User).where(User.id.in_(user_ids))).all()
    for user in users:
        if user.role == "admin":
            continue
        if not _viewer_has_other_group(session, user.id, group_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"No se puede quitar a '{user.username}' de este grupo porque "
                    "un espectador debe pertenecer al menos a un grupo de acceso."
                ),
            )


def replace_group_memberships(session: Session, group_id: int, user_ids: list[int]) -> None:
    unique_ids = set(user_ids)
    if unique_ids:
        users = session.exec(select(User).where(User.id.in_(unique_ids))).all()
        found_ids = {u.id for u in users}
        missing = unique_ids - found_ids
        if missing:
            raise HTTPException(status_code=400, detail=f"Usuarios inexistentes: {sorted(missing)}")

    old = session.exec(select(UserAccessGroup).where(UserAccessGroup.group_id == group_id)).all()
    old_ids = {row.user_id for row in old}
    ensure_group_membership_removal_is_safe(session, group_id, old_ids - unique_ids)

    for row in old:
        session.delete(row)
    for user_id in unique_ids:
        session.add(UserAccessGroup(user_id=user_id, group_id=group_id))


def replace_group_channels(session: Session, group_id: int, channel_ids: list[int]) -> None:
    unique_ids = set(channel_ids)
    if unique_ids:
        channels = session.exec(select(Channel).where(Channel.id.in_(unique_ids))).all()
        found_ids = {c.id for c in channels}
        missing = unique_ids - found_ids
        if missing:
            raise HTTPException(status_code=400, detail=f"Canales inexistentes: {sorted(missing)}")

    old = session.exec(select(GroupChannelAccess).where(GroupChannelAccess.group_id == group_id)).all()
    for row in old:
        session.delete(row)
    for channel_id in unique_ids:
        session.add(GroupChannelAccess(group_id=group_id, channel_id=channel_id))


def group_to_read(session: Session, group: AccessGroup):
    from app.models.access import AccessGroupRead

    members = session.exec(
        select(UserAccessGroup).where(UserAccessGroup.group_id == group.id)
    ).all()
    channels = session.exec(
        select(GroupChannelAccess).where(GroupChannelAccess.group_id == group.id)
    ).all()
    return AccessGroupRead(
        id=group.id,
        name=group.name,
        user_ids=sorted(row.user_id for row in members),
        channel_ids=sorted(row.channel_id for row in channels),
        created_at=group.created_at,
    )

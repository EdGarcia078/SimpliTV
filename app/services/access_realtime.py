"""Realtime fingerprints for viewer authorization state.

The player uses this as a lightweight, server-authoritative change detector.  The
fingerprint intentionally includes both effective access and the underlying
viewer preferences so a change is observable even when the final visible
channel set happens to stay the same.
"""

from __future__ import annotations

import hashlib
import json

from sqlmodel import Session, select

from app.models.access import UserAccessGroup
from app.models.channel import Channel
from app.models.preferences import UserBlockedChannel, UserPreference
from app.models.user import User
from app.services.access import (
    channel_is_sensitive,
    get_accessible_channel_ids,
    get_player_channel_ids,
)


def build_user_access_snapshot(session: Session, user: User) -> dict:
    """Return a deterministic snapshot of everything that can affect the player."""
    memberships = session.exec(
        select(UserAccessGroup).where(UserAccessGroup.user_id == user.id)
    ).all()
    membership_ids = sorted(row.group_id for row in memberships)

    base_access = get_accessible_channel_ids(session, user)
    if base_access is None:
        granted_channel_ids = sorted(session.exec(select(Channel.id)).all())
    else:
        granted_channel_ids = sorted(base_access)

    blocked_channel_ids = sorted(
        row.channel_id
        for row in session.exec(
            select(UserBlockedChannel).where(UserBlockedChannel.user_id == user.id)
        ).all()
    )

    preference = session.get(UserPreference, user.id)
    sensitive_content_enabled = bool(
        preference and preference.sensitive_content_enabled
    )

    # Include the portable YAML sensitive flags themselves.  This makes an admin
    # changing channel.yaml immediately observable even when no database row was
    # modified by that operation.
    relevant_ids = set(granted_channel_ids)
    if relevant_ids:
        relevant_channels = session.exec(
            select(Channel).where(Channel.id.in_(relevant_ids)).order_by(Channel.display_order, Channel.id)
        ).all()
    else:
        relevant_channels = []
    sensitive_channel_ids = sorted(
        channel.id for channel in relevant_channels if channel_is_sensitive(channel)
    )
    channel_catalog = [
        {"id": channel.id, "name": channel.name}
        for channel in relevant_channels
    ]

    return {
        "user_id": user.id,
        "role": user.role,
        "is_active": bool(user.is_active),
        "group_ids": membership_ids,
        "granted_channel_ids": granted_channel_ids,
        "blocked_channel_ids": blocked_channel_ids,
        "sensitive_content_enabled": sensitive_content_enabled,
        "sensitive_channel_ids": sensitive_channel_ids,
        "channel_catalog": channel_catalog,
        "visible_channel_ids": sorted(get_player_channel_ids(session, user)),
    }


def access_snapshot_fingerprint(snapshot: dict) -> str:
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_user_access_fingerprint(session: Session, user: User) -> str:
    return access_snapshot_fingerprint(build_user_access_snapshot(session, user))

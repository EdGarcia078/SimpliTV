from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi import Request
from sqlmodel import Session

from app.api.auth import access_events
from app.core.security import generate_session_token, get_session_expiry, hash_password
from app.models.access import AccessGroup, GroupChannelAccess, UserAccessGroup
from app.models.channel import Channel
from app.models.preferences import UserBlockedChannel, UserPreference
from app.models.user import User, UserSession
from app.services.access_realtime import (
    build_user_access_fingerprint,
    build_user_access_snapshot,
)
from app.services.media_config import ensure_channel_config


def _channel(
    session: Session,
    root: Path,
    name: str,
    *,
    sensitive: bool = False,
) -> Channel:
    channel_dir = root / name
    channel_dir.mkdir(parents=True, exist_ok=True)
    config_path = ensure_channel_config(channel_dir)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data["name"] = name
    data["sensitive_content"] = sensitive
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    channel = Channel(name=name, folder_name=name)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _viewer(session: Session) -> User:
    user = User(
        username="realtime_viewer",
        password_hash=hash_password("password123"),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_access_fingerprint_tracks_preferences_groups_and_sensitive_yaml(
    test_db: Session,
    sample_media_dir: Path,
):
    viewer = _viewer(test_db)
    channel_a = _channel(test_db, sample_media_dir, "Realtime A")
    channel_b = _channel(test_db, sample_media_dir, "Realtime B")

    group = AccessGroup(name="Realtime group")
    test_db.add(group)
    test_db.commit()
    test_db.refresh(group)
    test_db.add(UserAccessGroup(user_id=viewer.id, group_id=group.id))
    test_db.add(GroupChannelAccess(group_id=group.id, channel_id=channel_a.id))
    test_db.commit()

    initial = build_user_access_snapshot(test_db, viewer)
    initial_fp = build_user_access_fingerprint(test_db, viewer)
    assert initial["visible_channel_ids"] == [channel_a.id]

    # Personal channel block: another device must observe the preference change
    # even though the viewer/group objects themselves did not change.
    test_db.add(UserBlockedChannel(user_id=viewer.id, channel_id=channel_a.id))
    test_db.commit()
    blocked_fp = build_user_access_fingerprint(test_db, viewer)
    assert blocked_fp != initial_fp
    assert build_user_access_snapshot(test_db, viewer)["visible_channel_ids"] == []

    test_db.delete(test_db.get(UserBlockedChannel, (viewer.id, channel_a.id)))
    test_db.commit()

    # Group permission update: revoke A and grant B.
    grant_a = test_db.get(GroupChannelAccess, (group.id, channel_a.id))
    assert grant_a is not None
    test_db.delete(grant_a)
    test_db.add(GroupChannelAccess(group_id=group.id, channel_id=channel_b.id))
    test_db.commit()
    group_fp = build_user_access_fingerprint(test_db, viewer)
    assert group_fp != initial_fp
    assert build_user_access_snapshot(test_db, viewer)["visible_channel_ids"] == [channel_b.id]

    # Editing the portable channel.yaml sensitivity flag must also be visible to
    # the realtime detector, even though that operation does not touch SQLite.
    config_path = sample_media_dir / "Realtime B" / "channel.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["sensitive_content"] = True
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    sensitive_fp = build_user_access_fingerprint(test_db, viewer)
    assert sensitive_fp != group_fp
    assert build_user_access_snapshot(test_db, viewer)["visible_channel_ids"] == []

    # Enabling sensitive mode on another device restores B and changes the same
    # account-wide fingerprint.
    test_db.add(UserPreference(user_id=viewer.id, sensitive_content_enabled=True))
    test_db.commit()
    sensitive_mode_fp = build_user_access_fingerprint(test_db, viewer)
    assert sensitive_mode_fp != sensitive_fp
    assert build_user_access_snapshot(test_db, viewer)["visible_channel_ids"] == [channel_b.id]


@pytest.mark.asyncio
async def test_access_sse_sends_authoritative_revision_on_every_connection(
    test_db: Session,
    sample_media_dir: Path,
):
    viewer = _viewer(test_db)
    channel = _channel(test_db, sample_media_dir, "Realtime SSE")
    group = AccessGroup(name="Realtime SSE group")
    test_db.add(group)
    test_db.commit()
    test_db.refresh(group)
    test_db.add(UserAccessGroup(user_id=viewer.id, group_id=group.id))
    test_db.add(GroupChannelAccess(group_id=group.id, channel_id=channel.id))

    token = generate_session_token()
    test_db.add(
        UserSession(
            session_token=token,
            user_id=viewer.id,
            expires_at=get_session_expiry(),
        )
    )
    test_db.commit()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {"type": "http", "method": "GET", "path": "/api/auth/access-events", "headers": []},
        receive=receive,
    )
    response = await access_events(
        request=request,
        session_cookie=token,
        current_user=viewer,
        session=test_db,
    )

    iterator = response.body_iterator
    first = await iterator.__anext__()
    second = await iterator.__anext__()
    assert "retry: 1000" in first
    assert "event: access-update" in second
    assert '"initial": true' in second

    # Simulate device B blocking the current channel. Device A's already-open
    # SSE iterator must emit a new access revision on the next polling cycle.
    test_db.add(UserBlockedChannel(user_id=viewer.id, channel_id=channel.id))
    test_db.commit()
    changed = await iterator.__anext__()
    assert "event: access-update" in changed
    assert '"initial": true' not in changed
    await iterator.aclose()

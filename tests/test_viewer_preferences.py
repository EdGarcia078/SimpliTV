from pathlib import Path

import yaml
from sqlmodel import Session, select

from app.core.security import verify_password
from app.models.channel import Channel
from app.models.access import GroupChannelAccess, UserAccessGroup
from app.models.media import MediaItem
from app.services.media_config import ensure_channel_config, load_channel_config


def _make_channel(test_db: Session, sample_media_dir: Path, *, sensitive: bool = False) -> Channel:
    channel_dir = sample_media_dir / "Canal 1"
    channel_dir.mkdir(parents=True, exist_ok=True)
    config_path = ensure_channel_config(channel_dir)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data["name"] = "Canal 1"
    data["sensitive_content"] = sensitive
    config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    channel = Channel(name="Canal 1", folder_name="Canal 1")
    test_db.add(channel)
    test_db.commit()
    test_db.refresh(channel)
    return channel


def test_sensitive_mode_and_personal_channel_blocking(auth_client, test_db, sample_media_dir, normal_user):
    channel = _make_channel(test_db, sample_media_dir, sensitive=True)
    membership = test_db.exec(
        select(UserAccessGroup).where(UserAccessGroup.user_id == normal_user.id)
    ).first()
    assert membership is not None
    test_db.add(GroupChannelAccess(group_id=membership.group_id, channel_id=channel.id))
    test_db.commit()

    # Sensitive channels are invisible and cannot be reached directly by default.
    listed = auth_client.get("/api/channels")
    assert listed.status_code == 200
    assert listed.json() == []
    assert auth_client.get(f"/api/channels/{channel.id}/now-playing").status_code == 403

    # The protected preference section requires the user's current password.
    wrong = auth_client.post(
        "/api/auth/preferences",
        json={"current_password": "incorrect"},
    )
    assert wrong.status_code == 401

    locked = auth_client.post(
        "/api/auth/preferences",
        json={"current_password": "viewer123"},
    )
    assert locked.status_code == 200
    assert locked.json()["sensitive_content_enabled"] is False
    assert locked.json()["channels"] == []

    # Enabling the discreet sensitive mode reveals the channel to this user.
    enabled = auth_client.put(
        "/api/auth/preferences",
        json={"current_password": "viewer123", "sensitive_content_enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["sensitive_content_enabled"] is True
    assert [item["id"] for item in enabled.json()["channels"]] == [channel.id]

    listed = auth_client.get("/api/channels")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [channel.id]

    # A personal block removes the channel from every player-selection path.
    blocked = auth_client.put(
        "/api/auth/preferences",
        json={"current_password": "viewer123", "blocked_channel_ids": [channel.id]},
    )
    assert blocked.status_code == 200
    assert blocked.json()["blocked_channel_ids"] == [channel.id]
    assert auth_client.get("/api/channels").json() == []
    assert auth_client.get(f"/api/channels/{channel.id}/now-playing").status_code == 403

    # Direct episode metadata and stream URLs are protected too, not just the selector.
    media_path = sample_media_dir / "Canal 1" / "Bocchi the Rock" / "Season 1" / "S01E01.mp4"
    episode = MediaItem(
        channel_id=channel.id,
        media_title="Bocchi the Rock",
        season_number=1,
        episode_number=1,
        relative_path="Canal 1/Bocchi the Rock/Season 1/S01E01.mp4",
        file_path=str(media_path),
        file_size=media_path.stat().st_size,
        duration=1.0,
        mime_type="video/mp4",
    )
    test_db.add(episode)
    test_db.commit()
    test_db.refresh(episode)
    assert auth_client.get(f"/api/episodes/{episode.id}").status_code == 403
    assert auth_client.get(f"/api/stream/{episode.id}").status_code == 403

    # The protected panel still exposes blocked channels so they can be manually restored.
    unlocked_again = auth_client.post(
        "/api/auth/preferences",
        json={"current_password": "viewer123"},
    )
    assert unlocked_again.status_code == 200
    assert unlocked_again.json()["channels"][0]["blocked"] is True

    unblocked = auth_client.put(
        "/api/auth/preferences",
        json={"current_password": "viewer123", "blocked_channel_ids": []},
    )
    assert unblocked.status_code == 200
    assert unblocked.json()["blocked_channel_ids"] == []
    assert [item["id"] for item in auth_client.get("/api/channels").json()] == [channel.id]


def test_profile_change_requires_current_password(auth_client, test_db, normal_user):
    denied = auth_client.patch(
        "/api/auth/me",
        json={"current_password": "incorrect", "username": "viewer_changed"},
    )
    assert denied.status_code == 401

    renamed = auth_client.patch(
        "/api/auth/me",
        json={"current_password": "viewer123", "username": "viewer_changed"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["username"] == "viewer_changed"

    changed_password = auth_client.patch(
        "/api/auth/me",
        json={"current_password": "viewer123", "new_password": "viewer456"},
    )
    assert changed_password.status_code == 200

    test_db.refresh(normal_user)
    assert verify_password("viewer456", normal_user.password_hash)
    assert not verify_password("viewer123", normal_user.password_hash)


def test_channel_yaml_has_explicit_sensitive_default_and_migrates_legacy(sample_media_dir):
    channel_dir = sample_media_dir / "Canal Legacy Config"
    channel_dir.mkdir(parents=True, exist_ok=True)
    config_path = channel_dir / "channel.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "Canal Legacy Config",
                "schedule": {"default": ["series"], "slots": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_channel_config(channel_dir)
    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config.sensitive_content is False
    assert migrated["sensitive_content"] is False

    new_channel_dir = sample_media_dir / "Canal New Config"
    ensure_channel_config(new_channel_dir)
    created = yaml.safe_load((new_channel_dir / "channel.yaml").read_text(encoding="utf-8"))
    assert created["sensitive_content"] is False

from datetime import datetime, timezone

from sqlmodel import select

from app.core.security import hash_password
from app.models.access import AccessGroup, UserAccessGroup
from app.models.user import User


def test_demoting_admin_without_group_is_rejected(admin_client, test_db):
    target = User(
        username="demote_me",
        password_hash=hash_password("password123"),
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    test_db.add(target)
    test_db.commit()
    test_db.refresh(target)

    res = admin_client.patch(f"/api/admin/users/{target.id}", json={"role": "user"})
    assert res.status_code == 400
    assert "grupo" in res.json()["detail"].lower()


def test_demoting_admin_with_group_is_allowed(admin_client, test_db):
    target = User(
        username="grouped_admin",
        password_hash=hash_password("password123"),
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    group = AccessGroup(name="Future viewers")
    test_db.add(target)
    test_db.add(group)
    test_db.commit()
    test_db.refresh(target)
    test_db.refresh(group)
    test_db.add(UserAccessGroup(user_id=target.id, group_id=group.id))
    test_db.commit()

    res = admin_client.patch(f"/api/admin/users/{target.id}", json={"role": "user"})
    assert res.status_code == 200
    assert res.json()["role"] == "user"


def test_cannot_remove_viewer_from_last_group(admin_client, test_db):
    viewer = User(
        username="sole_group_viewer",
        password_hash=hash_password("password123"),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    group = AccessGroup(name="Only group")
    test_db.add(viewer)
    test_db.add(group)
    test_db.commit()
    test_db.refresh(viewer)
    test_db.refresh(group)
    test_db.add(UserAccessGroup(user_id=viewer.id, group_id=group.id))
    test_db.commit()

    res = admin_client.patch(f"/api/admin/groups/{group.id}", json={"user_ids": []})
    assert res.status_code == 400
    assert "grupo" in res.json()["detail"].lower()

    membership = test_db.exec(
        select(UserAccessGroup).where(
            UserAccessGroup.user_id == viewer.id,
            UserAccessGroup.group_id == group.id,
        )
    ).first()
    assert membership is not None


def test_cannot_delete_viewers_last_group(admin_client, test_db):
    viewer = User(
        username="delete_guard_viewer",
        password_hash=hash_password("password123"),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    group = AccessGroup(name="Protected group")
    test_db.add(viewer)
    test_db.add(group)
    test_db.commit()
    test_db.refresh(viewer)
    test_db.refresh(group)
    test_db.add(UserAccessGroup(user_id=viewer.id, group_id=group.id))
    test_db.commit()

    res = admin_client.delete(f"/api/admin/groups/{group.id}")
    assert res.status_code == 400
    assert test_db.get(AccessGroup, group.id) is not None


def test_viewer_without_group_has_no_channel_access(test_db):
    from app.services.access import get_accessible_channel_ids

    viewer = User(
        username="ungrouped_legacy_viewer",
        password_hash=hash_password("password123"),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    test_db.add(viewer)
    test_db.commit()
    test_db.refresh(viewer)

    assert get_accessible_channel_ids(test_db, viewer) == set()

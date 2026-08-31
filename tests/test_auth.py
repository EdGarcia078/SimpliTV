import argparse
from datetime import datetime, timezone, timedelta
import pytest
from sqlmodel import Session, select

from app.cli import create_admin_cmd
from app.core.security import SESSION_COOKIE_NAME, generate_session_token, hash_password
from app.models.user import User, UserSession
from app.models.access import AccessGroup, UserAccessGroup


# ==========================================
# 1. AUTHENTICATION TESTS
# ==========================================

def test_login_success(unauth_client, normal_user):
    res = unauth_client.post(
        "/api/auth/login",
        json={"username": "viewer_test", "password": "viewer123"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["username"] == "viewer_test"
    assert data["role"] == "user"
    assert "password_hash" not in data
    # Verify session cookie was set
    assert SESSION_COOKIE_NAME in res.cookies


def test_login_incorrect_password(unauth_client, normal_user):
    res = unauth_client.post(
        "/api/auth/login",
        json={"username": "viewer_test", "password": "wrongpassword"},
    )
    assert res.status_code == 401
    data = res.json()
    assert "password_hash" not in str(data)
    assert data["detail"] == "Usuario o contraseña incorrectos."


def test_login_nonexistent_user(unauth_client):
    res = unauth_client.post(
        "/api/auth/login",
        json={"username": "ghost_user", "password": "somepassword"},
    )
    assert res.status_code == 401
    data = res.json()
    assert data["detail"] == "Usuario o contraseña incorrectos."


def test_logout(auth_client):
    # Verify currently authenticated
    me_res = auth_client.get("/api/auth/me")
    assert me_res.status_code == 200

    # Logout
    logout_res = auth_client.post("/api/auth/logout")
    assert logout_res.status_code == 200

    # Subsequent request fails
    after_res = auth_client.get("/api/auth/me")
    assert after_res.status_code == 401


def test_expired_session(test_db: Session, sample_media_dir, normal_user):
    from app.main import app
    from fastapi.testclient import TestClient
    from app.db.session import get_session

    token = generate_session_token()
    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    session_rec = UserSession(
        session_token=token,
        user_id=normal_user.id,  # type: ignore
        expires_at=expired_time,
    )
    test_db.add(session_rec)
    test_db.commit()

    def override_get_session():
        yield test_db

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app, cookies={SESSION_COOKIE_NAME: token}) as expired_client:
        res = expired_client.get("/api/auth/me")
        assert res.status_code == 401
    app.dependency_overrides.clear()


def test_deactivated_user_cannot_authenticate(unauth_client, test_db: Session, normal_user):
    normal_user.is_active = False
    test_db.add(normal_user)
    test_db.commit()

    res = unauth_client.post(
        "/api/auth/login",
        json={"username": "viewer_test", "password": "viewer123"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Usuario o contraseña incorrectos."


# ==========================================
# 2. AUTHORIZATION TESTS
# ==========================================

def test_unauthenticated_cannot_access_channel_or_admin(unauth_client):
    # Channel endpoint -> 401 (unauthenticated)
    res1 = unauth_client.get("/api/channels/1/now-playing")
    assert res1.status_code == 401

    # Admin endpoint -> 401
    res2 = unauth_client.get("/api/admin/users")
    assert res2.status_code == 401

    # Admin UI route -> Redirect to /login
    res3 = unauth_client.get("/admin", follow_redirects=False)
    assert res3.status_code in [302, 307]
    assert "/login" in res3.headers.get("location", "")


def test_normal_user_cannot_access_admin(auth_client):
    # A viewer may receive 403 when its group does not grant the requested channel.
    res_channel = auth_client.get("/api/channels/1/now-playing")
    assert res_channel.status_code in [200, 403, 404]

    # Normal user CANNOT access admin API -> 403 Forbidden
    res_users = auth_client.get("/api/admin/users")
    assert res_users.status_code == 403

    res_stats = auth_client.get("/api/admin/stats")
    assert res_stats.status_code == 403

    res_skip = auth_client.post("/api/admin/channels/1/skip")
    assert res_skip.status_code == 403

    # Normal user visiting /admin is redirected to /
    res_admin_ui = auth_client.get("/admin", follow_redirects=False)
    assert res_admin_ui.status_code in [302, 307]
    assert res_admin_ui.headers.get("location") == "/"


def test_admin_can_access_all(admin_client):
    res_users = admin_client.get("/api/admin/users")
    assert res_users.status_code == 200

    res_stats = admin_client.get("/api/admin/stats")
    assert res_stats.status_code == 200

    res_lib = admin_client.get("/api/admin/library")
    assert res_lib.status_code == 200


# ==========================================
# 3. USER MANAGEMENT CRUD TESTS
# ==========================================

def test_admin_create_user(admin_client, test_db: Session):
    group = AccessGroup(name="Viewers")
    test_db.add(group)
    test_db.commit()
    test_db.refresh(group)

    res = admin_client.post(
        "/api/admin/users",
        json={
            "username": "new_viewer",
            "password": "password123",
            "role": "user",
            "group_id": group.id,
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["username"] == "new_viewer"
    assert data["role"] == "user"
    assert "password_hash" not in data

    # Verify user and mandatory membership in DB
    user_db = test_db.exec(select(User).where(User.username == "new_viewer")).first()
    assert user_db is not None
    assert user_db.password_hash != "password123"
    membership = test_db.exec(
        select(UserAccessGroup).where(
            UserAccessGroup.user_id == user_db.id,
            UserAccessGroup.group_id == group.id,
        )
    ).first()
    assert membership is not None


def test_admin_cannot_create_viewer_without_group(admin_client):
    res = admin_client.post(
        "/api/admin/users",
        json={"username": "orphan_viewer", "password": "password123", "role": "user"},
    )
    assert res.status_code == 400
    assert "grupo" in res.json()["detail"].lower()


def test_admin_can_create_admin_without_group(admin_client):
    res = admin_client.post(
        "/api/admin/users",
        json={"username": "second_admin", "password": "password123", "role": "admin"},
    )
    assert res.status_code == 201
    assert res.json()["role"] == "admin"


def test_admin_deactivate_and_reactivate_user(admin_client, normal_user):
    # Deactivate
    res1 = admin_client.patch(
        f"/api/admin/users/{normal_user.id}",
        json={"is_active": False},
    )
    assert res1.status_code == 200
    assert res1.json()["is_active"] is False

    # Reactivate
    res2 = admin_client.patch(
        f"/api/admin/users/{normal_user.id}",
        json={"is_active": True},
    )
    assert res2.status_code == 200
    assert res2.json()["is_active"] is True


def test_admin_change_user_role(admin_client, normal_user):
    res = admin_client.patch(
        f"/api/admin/users/{normal_user.id}",
        json={"role": "admin"},
    )
    assert res.status_code == 200
    assert res.json()["role"] == "admin"


def test_admin_reset_user_password(admin_client, unauth_client, normal_user):
    res = admin_client.post(
        f"/api/admin/users/{normal_user.id}/reset-password",
        json={"new_password": "newsecretpassword123"},
    )
    assert res.status_code == 200

    # Old password fails
    fail_res = unauth_client.post(
        "/api/auth/login",
        json={"username": "viewer_test", "password": "viewer123"},
    )
    assert fail_res.status_code == 401

    # New password succeeds
    ok_res = unauth_client.post(
        "/api/auth/login",
        json={"username": "viewer_test", "password": "newsecretpassword123"},
    )
    assert ok_res.status_code == 200


def test_admin_cannot_delete_or_deactivate_self(admin_client, admin_user):
    # Delete self -> 400
    del_res = admin_client.delete(f"/api/admin/users/{admin_user.id}")
    assert del_res.status_code == 400

    # Deactivate self -> 400
    deact_res = admin_client.patch(
        f"/api/admin/users/{admin_user.id}",
        json={"is_active": False},
    )
    assert deact_res.status_code == 400


# ==========================================
# 4. CLI CREATE-ADMIN TEST
# ==========================================

def test_cli_create_admin(test_db: Session):
    args = argparse.Namespace(username="cli_admin", password="clipassword123")
    create_admin_cmd(args, session=test_db)

    user = test_db.exec(select(User).where(User.username == "cli_admin")).first()
    assert user is not None
    assert user.role == "admin"
    assert user.is_active is True

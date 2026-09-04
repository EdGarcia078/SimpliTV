from datetime import datetime, timedelta, timezone
from fastapi import Request
from sqlmodel import Session, select

from app.core.config import settings
from app.core.request_security import get_client_ip
from app.core.security import (
    SESSION_COOKIE_NAME,
    as_utc,
    generate_session_token,
    get_session_expiry,
    hash_password,
    session_token_key,
)
from app.db.session import ensure_default_admin
from app.models.user import User, UserSession


def test_security_headers_and_private_same_origin_policy(unauth_client):
    response = unauth_client.get('/login')
    assert response.status_code == 200
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert 'access-control-allow-origin' not in response.headers


def test_api_docs_are_disabled_by_default(unauth_client):
    assert settings.ENABLE_API_DOCS is False
    assert unauth_client.get('/docs').status_code == 404
    assert unauth_client.get('/redoc').status_code == 404
    assert unauth_client.get('/openapi.json').status_code == 404


def test_sensitive_project_files_are_not_http_static_files(unauth_client):
    for path in ('/.env', '/simplitv.db', '/.git/HEAD', '/optimization_profile.json'):
        assert unauth_client.get(path).status_code == 404


def test_cross_site_browser_mutation_is_rejected(auth_client):
    blocked = auth_client.post(
        '/api/auth/logout',
        headers={
            'Origin': 'https://evil.example',
            'Sec-Fetch-Site': 'cross-site',
        },
    )
    assert blocked.status_code == 403

    # Same-host browser traffic remains valid for HTTP LAN installations.
    allowed = auth_client.post(
        '/api/auth/logout',
        headers={
            'Origin': 'http://testserver',
            'Sec-Fetch-Site': 'same-origin',
        },
    )
    assert allowed.status_code == 200


def test_login_cookie_supports_http_lan_by_default(unauth_client, normal_user):
    response = unauth_client.post(
        '/api/auth/login',
        json={'username': normal_user.username, 'password': 'viewer123'},
    )
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie
    assert 'SameSite=lax' in cookie
    assert 'Secure' not in cookie


def test_login_cookie_can_be_hardened_for_https(unauth_client, normal_user, monkeypatch):
    monkeypatch.setattr(settings, 'SECURE_COOKIES', True)
    response = unauth_client.post(
        '/api/auth/login',
        json={'username': normal_user.username, 'password': 'viewer123'},
    )
    assert response.status_code == 200
    assert 'Secure' in response.headers['set-cookie']


def test_new_sessions_are_hashed_in_database(unauth_client, test_db: Session, normal_user):
    response = unauth_client.post(
        '/api/auth/login',
        json={'username': normal_user.username, 'password': 'viewer123'},
    )
    assert response.status_code == 200
    raw_token = response.cookies.get(SESSION_COOKIE_NAME)
    assert raw_token

    stored = test_db.exec(
        select(UserSession).where(UserSession.user_id == normal_user.id)
    ).all()
    assert stored
    assert any(row.session_token == session_token_key(raw_token) for row in stored)
    assert all(row.session_token != raw_token for row in stored)


def test_legacy_plaintext_session_is_migrated_on_use(
    unauth_client,
    test_db: Session,
    normal_user,
):
    token = generate_session_token()
    legacy = UserSession(
        session_token=token,
        user_id=normal_user.id,
        expires_at=get_session_expiry(),
    )
    test_db.add(legacy)
    test_db.commit()

    unauth_client.cookies.set(SESSION_COOKIE_NAME, token)
    response = unauth_client.get('/api/auth/me')
    assert response.status_code == 200

    test_db.refresh(legacy)
    assert legacy.session_token == session_token_key(token)
    assert legacy.absolute_expires_at is not None


def test_session_renewal_never_crosses_absolute_expiry(
    auth_client,
    test_db: Session,
    normal_user,
):
    token = auth_client.cookies.get(SESSION_COOKIE_NAME)
    row = test_db.exec(
        select(UserSession).where(UserSession.session_token == session_token_key(token))
    ).one()
    hard_limit = datetime.now(timezone.utc) + timedelta(minutes=30)
    row.absolute_expires_at = hard_limit
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    test_db.add(row)
    test_db.commit()

    assert auth_client.get('/api/auth/me').status_code == 200
    test_db.refresh(row)
    assert as_utc(row.expires_at) <= hard_limit


def test_login_rate_limit_blocks_repeated_guessing(unauth_client, normal_user):
    for _ in range(settings.LOGIN_MAX_FAILURES):
        response = unauth_client.post(
            '/api/auth/login',
            json={'username': normal_user.username, 'password': 'incorrect-password'},
        )
        assert response.status_code == 401

    blocked = unauth_client.post(
        '/api/auth/login',
        json={'username': normal_user.username, 'password': 'incorrect-password'},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers['retry-after']) > 0


def test_seeded_default_admin_must_change_password_before_normal_use(
    unauth_client,
    test_db: Session,
):
    admin = ensure_default_admin(test_db)
    assert admin is not None
    assert admin.must_change_password is True

    login = unauth_client.post(
        '/api/auth/login', json={'username': 'admin', 'password': 'admin123'}
    )
    assert login.status_code == 200
    assert login.json()['must_change_password'] is True

    denied = unauth_client.get('/api/admin/users')
    assert denied.status_code == 403
    assert denied.headers['x-simplitv-password-change-required'] == '1'

    changed = unauth_client.post(
        '/api/auth/change-default-password',
        json={
            'current_password': 'admin123',
            'new_password': 'a-stronger-admin-password',
        },
    )
    assert changed.status_code == 200
    assert changed.json()['must_change_password'] is False
    assert unauth_client.get('/api/admin/users').status_code == 200


def test_new_password_policy_does_not_break_existing_short_passwords(
    unauth_client,
    admin_client,
    normal_user,
):
    # Existing installations keep authenticating; only newly-set passwords need 12+.
    assert unauth_client.post(
        '/api/auth/login',
        json={'username': normal_user.username, 'password': 'viewer123'},
    ).status_code == 200

    rejected = admin_client.post(
        f'/api/admin/users/{normal_user.id}/reset-password',
        json={'new_password': 'shortpass'},
    )
    assert rejected.status_code == 400
    assert '12' in rejected.json()['detail']


def test_forwarded_client_ip_is_ignored_unless_peer_is_trusted(monkeypatch):
    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/api/auth/login',
        'headers': [(b'x-forwarded-for', b'203.0.113.50')],
        'client': ('192.0.2.10', 50000),
        'server': ('simplitv', 8000),
        'scheme': 'http',
        'query_string': b'',
    }
    request = Request(scope)
    monkeypatch.setattr(settings, 'TRUSTED_PROXIES', '')
    assert get_client_ip(request) == '192.0.2.10'

    monkeypatch.setattr(settings, 'TRUSTED_PROXIES', '192.0.2.10')
    assert get_client_ip(request) == '203.0.113.50'


def test_oversized_session_token_is_rejected_without_database_lookup(unauth_client):
    unauth_client.cookies.set(SESSION_COOKIE_NAME, 'x' * 1000)
    response = unauth_client.get('/api/auth/me')
    assert response.status_code == 401

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import SESSION_COOKIE_NAME, generate_session_token, get_session_expiry, hash_password
from app.db.session import get_session
from app.models.user import User, UserSession
from app.models.access import AccessGroup, GroupChannelAccess, UserAccessGroup
from app.models.channel import Channel
from app.services.channel import channel_engine
from app.main import app


@pytest.fixture(scope="session")
def test_temp_dir():
    """Create a temporary directory for tests."""
    temp_dir = tempfile.mkdtemp(prefix="simplitv_test_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def test_db(test_temp_dir):
    """Create an isolated test SQLite database (unique per test invocation)."""
    import uuid
    channel_engine.reset()
    db_path = test_temp_dir / f"test_media_{uuid.uuid4().hex}.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        yield session

    SQLModel.metadata.drop_all(test_engine)
    channel_engine.reset()


@pytest.fixture
def sample_media_dir(test_temp_dir):
    """Create a sample directory structure with real dummy video files.

    Hierarchy: Channel / Show / Season / MediaItem
    This matches the official media/<CHANNEL>/<SHOW>/<SEASON>/<EPISODE> structure.
    """
    import uuid
    media_root = test_temp_dir / f"sample_media_{uuid.uuid4().hex}"
    media_root.mkdir(parents=True, exist_ok=True)

    # 1. Canal 1 / Bocchi the Rock / Season 1 / S01E01.mp4
    bocchi_dir = media_root / "Canal 1" / "Bocchi the Rock" / "Season 1"
    bocchi_dir.mkdir(parents=True, exist_ok=True)
    bocchi_ep1 = bocchi_dir / "S01E01.mp4"

    if not bocchi_ep1.exists():
        os.system(f"ffmpeg -y -f lavfi -i testsrc=duration=1:size=320x240:rate=1 -f lavfi -i sine=frequency=1000:duration=1 -c:v libx264 -c:a aac '{bocchi_ep1}' > /dev/null 2>&1")
        if not bocchi_ep1.exists() or bocchi_ep1.stat().st_size == 0:
            bocchi_ep1.write_bytes(b"\x00" * 4096)

    # 2. Canal 1 / JoJo / Season 1 / S01E02 - The Prophecy.mp4
    jojo_dir = media_root / "Canal 1" / "JoJo" / "Season 1"
    jojo_dir.mkdir(parents=True, exist_ok=True)
    jojo_ep2 = jojo_dir / "S01E02 - The Prophecy.mp4"
    if not jojo_ep2.exists():
        os.system(f"ffmpeg -y -f lavfi -i testsrc=duration=1:size=320x240:rate=1 -f lavfi -i sine=frequency=1000:duration=1 -c:v libx264 -c:a aac '{jojo_ep2}' > /dev/null 2>&1")
        if not jojo_ep2.exists() or jojo_ep2.stat().st_size == 0:
            jojo_ep2.write_bytes(b"\x00" * 4096)

    # Override settings.MEDIA_DIR during tests
    original_media_dir = settings.MEDIA_DIR
    settings.MEDIA_DIR = media_root

    yield media_root

    settings.MEDIA_DIR = original_media_dir


@pytest.fixture
def admin_user(test_db: Session) -> User:
    """Create an administrator user in test DB."""
    admin = User(
        username="admin_test",
        password_hash=hash_password("admin123"),
        role="admin",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    test_db.add(admin)
    test_db.commit()
    test_db.refresh(admin)
    return admin


@pytest.fixture
def normal_user(test_db: Session) -> User:
    """Create a standard viewer user in test DB."""
    user = User(
        username="viewer_test",
        password_hash=hash_password("viewer123"),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    test_db.add(user)
    test_db.commit()
    test_db.refresh(user)
    return user


@pytest.fixture
def unauth_client(test_db, sample_media_dir):
    """FastAPI TestClient with NO authenticated session."""
    def override_get_session():
        yield test_db

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def client(test_db, sample_media_dir, admin_user):
    """FastAPI TestClient logged in by default as admin for backwards compatibility with earlier tests."""
    def override_get_session():
        yield test_db

    app.dependency_overrides[get_session] = override_get_session

    token = generate_session_token()
    session_rec = UserSession(
        session_token=token,
        user_id=admin_user.id,  # type: ignore
        expires_at=get_session_expiry(),
    )
    test_db.add(session_rec)
    test_db.commit()

    with TestClient(app, cookies={SESSION_COOKIE_NAME: token}) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client(test_db, sample_media_dir, normal_user):
    """FastAPI TestClient logged in as standard user (non-admin)."""
    def override_get_session():
        yield test_db

    app.dependency_overrides[get_session] = override_get_session

    token = generate_session_token()
    session_rec = UserSession(
        session_token=token,
        user_id=normal_user.id,  # type: ignore
        expires_at=get_session_expiry(),
    )
    test_db.add(session_rec)
    test_db.commit()

    with TestClient(app, cookies={SESSION_COOKIE_NAME: token}) as test_client:
        # Every standard viewer must belong to a group. The generic authenticated
        # viewer fixture receives a group granting the channels discovered at startup.
        group = AccessGroup(name=f"Test viewers {normal_user.id}")
        test_db.add(group)
        test_db.flush()
        test_db.add(UserAccessGroup(user_id=normal_user.id, group_id=group.id))
        for channel in test_db.exec(select(Channel)).all():
            test_db.add(GroupChannelAccess(group_id=group.id, channel_id=channel.id))
        test_db.commit()
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(test_db, sample_media_dir, admin_user):
    """FastAPI TestClient logged in as admin."""
    def override_get_session():
        yield test_db

    app.dependency_overrides[get_session] = override_get_session

    token = generate_session_token()
    session_rec = UserSession(
        session_token=token,
        user_id=admin_user.id,  # type: ignore
        expires_at=get_session_expiry(),
    )
    test_db.add(session_rec)
    test_db.commit()

    with TestClient(app, cookies={SESSION_COOKIE_NAME: token}) as test_client:
        yield test_client
    app.dependency_overrides.clear()

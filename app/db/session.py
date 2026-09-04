import logging
from typing import Generator, Optional

from sqlalchemy import event, inspect
from sqlmodel import SQLModel, Session, create_engine, select

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models.user import User
# Import preference models before metadata.create_all() so their tables are registered.
from app.models.preferences import UserPreference, UserBlockedChannel  # noqa: F401


logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

# SQLite connection args for multi-threaded async workers
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Make relation cleanup guarantees effective for every SQLite connection."""
    if not settings.DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_and_tables() -> None:
    """Initialize database tables and apply lightweight compatible migrations."""
    _migrate_legacy_media_table()
    SQLModel.metadata.create_all(engine)
    _migrate_user_security_fields()
    _migrate_session_security_fields()
    _migrate_channel_start_mode()
    _migrate_channel_folder_name()
    _migrate_episode_media_fields()
    _migrate_episode_title_field()
    _migrate_media_file_mtime()
    _repair_orphan_channel_relations()
    with Session(engine) as session:
        ensure_default_admin(session)
        _mark_default_password_for_change(session)


def ensure_default_admin(session: Session) -> Optional[User]:
    """Create the first administrator for a brand-new installation.

    The seed only runs while the users table is completely empty. Once any user
    exists, startup never resets credentials, changes roles, reactivates an
    account, or recreates the well-known default administrator.
    """
    if session.exec(select(User.id).limit(1)).first() is not None:
        return None

    admin = User(
        username=DEFAULT_ADMIN_USERNAME,
        password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
        role="admin",
        is_active=True,
        must_change_password=True,
    )
    session.add(admin)
    session.commit()
    session.refresh(admin)
    logger.warning(
        "Created the default SimpliTV administrator '%s' with the documented first-run password. A password change is required before normal use.",
        DEFAULT_ADMIN_USERNAME,
    )
    return admin



def _migrate_user_security_fields() -> None:
    """Add security flags to existing SQLite user tables without resetting data."""
    inspector = inspect(engine)
    if "users" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "must_change_password" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"
            )


def _migrate_session_security_fields() -> None:
    """Add an absolute session lifetime while preserving active legacy sessions."""
    inspector = inspect(engine)
    if "user_sessions" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("user_sessions")}
    if "absolute_expires_at" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE user_sessions ADD COLUMN absolute_expires_at DATETIME"
            )


def _mark_default_password_for_change(session: Session) -> None:
    """Require a password change whenever the documented admin password is active.

    This also covers installations created by older SimpliTV releases before the
    ``must_change_password`` column existed. Other accounts are never modified.
    """
    admin = session.exec(select(User).where(User.username == DEFAULT_ADMIN_USERNAME)).first()
    if admin is None or admin.must_change_password:
        return
    if verify_password(DEFAULT_ADMIN_PASSWORD, admin.password_hash):
        admin.must_change_password = True
        session.add(admin)
        session.commit()
        logger.warning(
            "The default administrator password is still active. Change it before exposing SimpliTV outside a trusted LAN."
        )


def _migrate_legacy_media_table() -> None:
    """Upgrade the pre-SimpliTV media table name before SQLModel creates tables."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "media_items" in tables or "episodes" not in tables:
        return
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE episodes RENAME TO media_items")

def _migrate_channel_start_mode() -> None:
    """
    Upgrade existing databases from the old ``start_from_even`` boolean to
    ``start_mode`` (any/even/odd) without requiring a migration framework.
    """
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "channels" not in table_names:
        return

    columns = {column["name"] for column in inspector.get_columns("channels")}
    if "start_mode" in columns:
        return

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE channels "
            "ADD COLUMN start_mode VARCHAR NOT NULL DEFAULT 'any'"
        )
        if "start_from_even" in columns:
            connection.exec_driver_sql(
                "UPDATE channels SET start_mode = 'even' "
                "WHERE start_from_even = 1"
            )


def _migrate_channel_folder_name() -> None:
    inspector = inspect(engine)
    if "channels" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("channels")}
    if "folder_name" in columns:
        return

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE channels ADD COLUMN folder_name VARCHAR"
        )
        connection.exec_driver_sql(
            "UPDATE channels SET folder_name = name WHERE folder_name IS NULL"
        )


def _migrate_episode_media_fields() -> None:
    inspector = inspect(engine)
    if "media_items" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("media_items")}
    with engine.begin() as connection:
        if "media_type" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE media_items "
                "ADD COLUMN media_type VARCHAR NOT NULL DEFAULT 'episode'"
            )
        if "franchise" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE media_items ADD COLUMN franchise VARCHAR"
            )


def get_session() -> Generator[Session, None, None]:
    """Yield a database session for request dependency injection."""
    with Session(engine) as session:
        yield session


def _migrate_episode_title_field() -> None:
    """Rename the legacy anime-specific title column to the generic media title."""
    inspector = inspect(engine)
    if "media_items" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("media_items")}
    if "media_title" in columns or "anime_title" not in columns:
        return

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE media_items RENAME COLUMN anime_title TO media_title"
        )


def _migrate_media_file_mtime() -> None:
    """Add a nanosecond mtime fingerprint without rebuilding existing databases."""
    inspector = inspect(engine)
    if "media_items" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("media_items")}
    if "file_mtime_ns" in columns:
        return
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE media_items ADD COLUMN file_mtime_ns INTEGER NOT NULL DEFAULT 0"
        )


def _repair_orphan_channel_relations() -> None:
    """Clean historical dangling channel/media references before normal use."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        if {"channel_state", "channels", "media_items"} <= tables:
            connection.exec_driver_sql(
                "DELETE FROM channel_state "
                "WHERE channel_id NOT IN (SELECT id FROM channels) "
                "OR current_episode_id NOT IN (SELECT id FROM media_items)"
            )
            connection.exec_driver_sql(
                "UPDATE channel_state SET next_episode_id = NULL "
                "WHERE next_episode_id IS NOT NULL "
                "AND next_episode_id NOT IN (SELECT id FROM media_items)"
            )
        if {"group_channel_access", "channels"} <= tables:
            connection.exec_driver_sql(
                "DELETE FROM group_channel_access "
                "WHERE channel_id NOT IN (SELECT id FROM channels)"
            )
        if {"user_blocked_channels", "channels"} <= tables:
            connection.exec_driver_sql(
                "DELETE FROM user_blocked_channels "
                "WHERE channel_id NOT IN (SELECT id FROM channels)"
            )

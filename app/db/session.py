import logging
from typing import Generator, Optional

from sqlalchemy import inspect
from sqlmodel import SQLModel, Session, create_engine, select

from app.core.config import settings
from app.core.security import hash_password
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


def create_db_and_tables() -> None:
    """Initialize database tables and apply lightweight compatible migrations."""
    _migrate_legacy_media_table()
    SQLModel.metadata.create_all(engine)
    _migrate_channel_start_mode()
    _migrate_channel_folder_name()
    _migrate_episode_media_fields()
    _migrate_episode_title_field()
    with Session(engine) as session:
        ensure_default_admin(session)


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
    )
    session.add(admin)
    session.commit()
    session.refresh(admin)
    logger.warning(
        "Created the default SimpliTV administrator '%s'. Change its password after first login.",
        DEFAULT_ADMIN_USERNAME,
    )
    return admin



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

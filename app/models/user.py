from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class UserBase(SQLModel):
    """Base user properties."""
    username: str = Field(unique=True, index=True, min_length=3, max_length=50)
    role: str = Field(default="user", index=True)  # "user" | "admin"
    is_active: bool = Field(default=True)


class User(UserBase, table=True):
    """User database table."""
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    password_hash: str
    # Only the automatically seeded admin/admin123 account uses this flag. It
    # keeps first-run/reset access simple while preventing continued use of the
    # well-known default password.
    must_change_password: bool = Field(default=False, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_login_at: Optional[datetime] = Field(default=None)


class UserSession(SQLModel, table=True):
    """Database-backed user sessions for reliable, revocable authentication."""
    __tablename__ = "user_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    # New rows contain ``sha256:<digest>`` rather than the browser's raw token.
    # Legacy plaintext values are transparently migrated on successful use.
    session_token: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: datetime
    absolute_expires_at: Optional[datetime] = Field(default=None)


class UserRead(SQLModel):
    """Public user response schema (never exposes password_hash/session data)."""
    id: int
    username: str
    role: str
    is_active: bool
    must_change_password: bool = False
    created_at: datetime
    last_login_at: Optional[datetime] = None


class UserCreate(SQLModel):
    """Schema for creating a user. Viewers must be assigned to an access group."""
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=1, max_length=128)
    role: str = "user"
    group_id: Optional[int] = None


class UserUpdate(SQLModel):
    """Schema for updating user role or active status."""
    role: Optional[str] = None
    is_active: Optional[bool] = None


class PasswordResetRequest(SQLModel):
    """Schema for resetting a user's password."""
    new_password: str = Field(min_length=1, max_length=128)


class LoginRequest(SQLModel):
    """Schema for login credentials."""
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)

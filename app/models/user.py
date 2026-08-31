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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_login_at: Optional[datetime] = Field(default=None)


class UserSession(SQLModel, table=True):
    """Database-backed user sessions for reliable, revocable authentication."""
    __tablename__ = "user_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_token: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: datetime


class UserRead(SQLModel):
    """Public user response schema (never exposes password_hash)."""
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None


class UserCreate(SQLModel):
    """Schema for creating a user. Viewers must be assigned to an access group."""
    username: str
    password: str
    role: str = "user"
    group_id: Optional[int] = None


class UserUpdate(SQLModel):
    """Schema for updating user role or active status."""
    role: Optional[str] = None
    is_active: Optional[bool] = None


class PasswordResetRequest(SQLModel):
    """Schema for resetting a user's password."""
    new_password: str


class LoginRequest(SQLModel):
    """Schema for login credentials."""
    username: str
    password: str

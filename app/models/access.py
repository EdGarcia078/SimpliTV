from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class AccessGroup(SQLModel, table=True):
    """Named access policy used to grant users access to selected channels."""
    __tablename__ = "access_groups"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True, min_length=1, max_length=80)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserAccessGroup(SQLModel, table=True):
    """Many-to-many membership between users and access groups."""
    __tablename__ = "user_access_groups"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    group_id: int = Field(foreign_key="access_groups.id", primary_key=True)


class GroupChannelAccess(SQLModel, table=True):
    """Many-to-many allow-list between access groups and channels."""
    __tablename__ = "group_channel_access"

    group_id: int = Field(foreign_key="access_groups.id", primary_key=True)
    channel_id: int = Field(foreign_key="channels.id", primary_key=True)


class AccessGroupCreate(SQLModel):
    name: str = Field(min_length=1, max_length=80)
    user_ids: list[int] = Field(default_factory=list, max_length=10000)
    channel_ids: list[int] = Field(default_factory=list, max_length=10000)


class AccessGroupUpdate(SQLModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    user_ids: Optional[list[int]] = Field(default=None, max_length=10000)
    channel_ids: Optional[list[int]] = Field(default=None, max_length=10000)


class AccessGroupRead(SQLModel):
    id: int
    name: str
    user_ids: list[int]
    channel_ids: list[int]
    created_at: datetime

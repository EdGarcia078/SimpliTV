from sqlmodel import Field, SQLModel


class UserPreference(SQLModel, table=True):
    """Persistent viewer preferences that belong to one authenticated user."""
    __tablename__ = "user_preferences"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    sensitive_content_enabled: bool = Field(default=False)


class UserBlockedChannel(SQLModel, table=True):
    """Channels a user has explicitly hidden from their own player."""
    __tablename__ = "user_blocked_channels"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    channel_id: int = Field(foreign_key="channels.id", primary_key=True)

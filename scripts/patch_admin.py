with open("app/api/admin.py", "r") as f:
    content = f.read()

# Replace get_admin_stats
content = content.replace(
"""    channel_state = await channel_engine.get_current_state(session)

    return {
        "media": {
            "total_episodes": total_episodes,
            "unique_series": unique_series,
            "total_duration_seconds": round(total_duration, 2),
            "total_duration_hours": round(total_duration / 3600, 2),
        },
        "users": {
            "total_users": total_users,
            "active_users": active_users,
        },
        "channel": channel_state.model_dump() if channel_state else None,
    }""",
"""    return {
        "media": {
            "total_episodes": total_episodes,
            "unique_series": unique_series,
            "total_duration_seconds": round(total_duration, 2),
            "total_duration_hours": round(total_duration / 3600, 2),
        },
        "users": {
            "total_users": total_users,
            "active_users": active_users,
        },
    }"""
)

# Replace admin_skip_episode
content = content.replace(
"""@router.post("/channel/skip", summary="Administrative Skip MediaItem")
async def admin_skip_episode(session: Session = Depends(get_session)):
    \"\"\"
    Administratively advance the broadcast to the next episode immediately.
    Maintains synchronization across all viewers.
    \"\"\"
    now = datetime.now(timezone.utc)
    async with channel_engine._lock:
        await channel_engine._advance_episode_locked(session, now)

    new_state = await channel_engine.get_current_state(session)
    return {
        "message": "Episodio avanzado exitosamente.",
        "state": new_state,
    }""",
"""from pydantic import BaseModel
class ChannelSettingsUpdate(BaseModel):
    batch_size: int
    start_from_even: bool
    loop: bool

@router.post("/channels/{channel_id}/skip", summary="Administrative Skip MediaItem")
async def admin_skip_episode(channel_id: int, session: Session = Depends(get_session)):
    \"\"\"
    Administratively advance the broadcast to the next episode immediately.
    \"\"\"
    await channel_engine.skip_episode(session, channel_id)
    new_state = await channel_engine.get_current_state(session, channel_id)
    return {
        "message": "Episodio avanzado exitosamente.",
        "state": new_state,
    }

@router.patch("/channels/{channel_id}/settings", summary="Update Channel Settings")
def update_channel_settings(
    channel_id: int,
    settings: ChannelSettingsUpdate,
    session: Session = Depends(get_session)
):
    from app.models.channel import Channel, ChannelRead
    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
        
    channel.batch_size = settings.batch_size
    channel.start_from_even = settings.start_from_even
    channel.loop = settings.loop
    
    session.add(channel)
    session.commit()
    session.refresh(channel)
    
    return ChannelRead.model_validate(channel)
"""
)

with open("app/api/admin.py", "w") as f:
    f.write(content)

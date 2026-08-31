with open("scripts/migrate_v2.py", "r") as f:
    content = f.read()

content = content.replace(
    "current_episode_id INTEGER NOT NULL,",
    "current_episode_id INTEGER NOT NULL,\n                consecutive_plays INTEGER NOT NULL DEFAULT 1,"
)
content = content.replace(
    "SELECT channel_id, current_episode_id, next_episode_id, started_at, duration, updated_at",
    "SELECT channel_id, current_episode_id, 1, next_episode_id, started_at, duration, updated_at"
)
content = content.replace(
    "INSERT INTO channel_state (channel_id, current_episode_id, next_episode_id, started_at, duration, updated_at)",
    "INSERT INTO channel_state (channel_id, current_episode_id, consecutive_plays, next_episode_id, started_at, duration, updated_at)"
)

with open("scripts/migrate_v2.py", "w") as f:
    f.write(content)
print("Updated migrate script")

with open("app/models/channel.py", "r") as f:
    content = f.read()

content = content.replace(
    "current_episode_id: int = Field(foreign_key=\"episodes.id\")",
    "current_episode_id: int = Field(foreign_key=\"episodes.id\")\n    consecutive_plays: int = Field(default=1)"
)

with open("app/models/channel.py", "w") as f:
    f.write(content)

print("Updated channel.py")

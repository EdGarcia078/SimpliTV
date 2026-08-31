import re

with open("app/services/scanner.py", "r") as f:
    content = f.read()

# Replace upsert_episode_file
content = content.replace(
"""    media_title, season_num, ep_num, ep_title = parse_media_filename(resolved_path, target_dir)

    if existing_ep is None:""",
"""    channel_name, show_name, season_num, ep_num, ep_title = parse_media_filename(resolved_path, target_dir)

    channel = session.exec(select(Channel).where(Channel.name == channel_name)).first()
    if not channel:
        channel = Channel(name=channel_name)
        session.add(channel)
        session.commit()
        session.refresh(channel)

    if existing_ep is None:""")

content = content.replace(
"""        new_ep = MediaItem(
            media_title=media_title,""",
"""        new_ep = MediaItem(
            channel_id=channel.id,
            media_title=show_name,""")

content = content.replace(
"""        if (
            existing_ep.media_title != media_title""",
"""        if existing_ep.channel_id != channel.id:
            existing_ep.channel_id = channel.id
            changed = True
            
        if (
            existing_ep.media_title != show_name""")

content = content.replace(
"""            existing_ep.media_title = media_title""",
"""            existing_ep.media_title = show_name""")

content = content.replace(
"""logger.info(f"Dynamically indexed new episode: '{media_title}' S{season_num}E{ep_num} ({rel_path})")""",
"""logger.info(f"Dynamically indexed new episode: '[{channel_name}] {show_name}' S{season_num}E{ep_num} ({rel_path})")""")

content = content.replace(
"""logger.info(f"Dynamically updated episode: '{media_title}' S{season_num}E{ep_num} ({rel_path})")""",
"""logger.info(f"Dynamically updated episode: '[{channel_name}] {show_name}' S{season_num}E{ep_num} ({rel_path})")""")

# Replace scan_library equivalents
content = content.replace(
"""            media_title, season_num, ep_num, ep_title = parse_media_filename(file_path, target_dir)

            if existing_ep is None:""",
"""            channel_name, show_name, season_num, ep_num, ep_title = parse_media_filename(file_path, target_dir)

            channel = session.exec(select(Channel).where(Channel.name == channel_name)).first()
            if not channel:
                channel = Channel(name=channel_name)
                session.add(channel)
                session.commit()
                session.refresh(channel)

            if existing_ep is None:""")

with open("app/services/scanner.py", "w") as f:
    f.write(content)

print("Patched scanner.py")

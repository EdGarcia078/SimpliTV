import yaml
from sqlmodel import select

from app.models.channel import Channel
from app.models.media import MediaItem


def test_admin_panel_reads_and_writes_portable_configuration(client, test_db, sample_media_dir):
    franchise_dir = sample_media_dir / "Canal 1" / "Movies" / "Harry Potter"
    franchise_dir.mkdir(parents=True, exist_ok=True)
    (franchise_dir / "Movie One.mp4").write_bytes(b"dummy movie")

    scan = client.post("/api/library/scan")
    assert scan.status_code == 200

    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal 1")).one()
    response = client.get(f"/api/admin/channels/{channel.id}/configuration")
    assert response.status_code == 200
    data = response.json()

    assert data["channel"]["schedule"]["default"] == ["series", "movies"]
    assert {item["name"] for item in data["series"]} >= {"Bocchi the Rock", "JoJo"}
    assert any(item["folder_name"] == "Harry Potter" for item in data["franchises"])

    channel_save = client.put(
        f"/api/admin/channels/{channel.id}/configuration",
        json={
            "version": 1,
            "name": "Canal Retro",
            "schedule": {
                "default": ["series"],
                "slots": [
                    {"start": "20:00", "end": "06:00", "content": ["movies"]},
                ],
            },
        },
    )
    assert channel_save.status_code == 200, channel_save.text

    channel_yaml = yaml.safe_load((sample_media_dir / "Canal 1" / "channel.yaml").read_text())
    assert channel_yaml["name"] == "Canal Retro"
    assert channel_yaml["schedule"]["default"] == ["series"]
    assert channel_yaml["schedule"]["slots"][0]["programming"]["series"]["mode"] == "off"
    assert channel_yaml["schedule"]["slots"][0]["programming"]["movies"]["mode"] == "all"

    test_db.refresh(channel)
    assert channel.name == "Canal Retro"

    refreshed = channel_save.json()
    jojo = next(item for item in refreshed["series"] if item["name"] == "JoJo")
    series_save = client.put(
        f"/api/admin/channels/{channel.id}/series/configuration",
        json={
            "relative_dir": jojo["relative_dir"],
            "config": {
                "version": 1,
                "episodes_per_airing": 3,
                "start_episode": {"mode": "odd"},
                "playback": {"mode": "random"},
            },
        },
    )
    assert series_save.status_code == 200, series_save.text

    series_yaml = yaml.safe_load((sample_media_dir / "Canal 1" / "Series" / "JoJo" / "series.yaml").read_text())
    assert series_yaml["episodes_per_airing"] == 3
    assert series_yaml["start_episode"]["mode"] == "odd"
    assert series_yaml["playback"]["mode"] == "random"

    hp = next(item for item in series_save.json()["franchises"] if item["folder_name"] == "Harry Potter")
    franchise_save = client.put(
        f"/api/admin/channels/{channel.id}/franchises/configuration",
        json={
            "relative_dir": hp["relative_dir"],
            "config": {"version": 1, "name": "Mundo Mágico"},
        },
    )
    assert franchise_save.status_code == 200, franchise_save.text

    franchise_yaml = yaml.safe_load((franchise_dir / "franchise.yaml").read_text())
    assert franchise_yaml["name"] == "Mundo Mágico"

    movie = test_db.exec(
        select(MediaItem).where(MediaItem.media_type == "movie", MediaItem.channel_id == channel.id)
    ).one()
    assert movie.franchise == "Mundo Mágico"


def test_admin_portable_configuration_rejects_path_escape(client, test_db, sample_media_dir):
    scan = client.post("/api/library/scan")
    assert scan.status_code == 200
    channel = test_db.exec(select(Channel).where(Channel.folder_name == "Canal 1")).one()

    response = client.put(
        f"/api/admin/channels/{channel.id}/series/configuration",
        json={
            "relative_dir": "../outside",
            "config": {
                "version": 1,
                "episodes_per_airing": 1,
                "start_episode": {"mode": "any"},
                "playback": {"mode": "random"},
            },
        },
    )
    assert response.status_code == 400

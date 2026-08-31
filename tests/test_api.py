import pytest
from app.services.scanner import scan_library


def test_health_check(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "online"
    assert data["app"] == "SimpliTV"


@pytest.mark.asyncio
async def test_scan_and_episodes_api(client, test_db, sample_media_dir):
    # Scan API
    scan_res = client.post("/api/library/scan")
    assert scan_res.status_code == 200
    scan_data = scan_res.json()
    assert scan_data["total_episodes"] >= 2

    # Stats API
    stats_res = client.get("/api/library/stats")
    assert stats_res.status_code == 200
    stats_data = stats_res.json()
    assert stats_data["total_episodes"] >= 2
    assert stats_data["unique_series"] >= 2

    # List episodes API
    episodes_res = client.get("/api/episodes")
    assert episodes_res.status_code == 200
    episodes_data = episodes_res.json()
    assert len(episodes_data) >= 2

    # Random episode API
    random_res = client.get("/api/episodes/random")
    assert random_res.status_code == 200
    rand_data = random_res.json()
    assert "stream_url" in rand_data
    assert "play_count" in rand_data


def test_web_player_index(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "SimpliTV" in res.text
    assert "tv-player" in res.text

import pytest
from fastapi import HTTPException
from app.models.media import MediaItem
from app.services.streaming import validate_file_safety


def test_path_traversal_blocked(test_db, sample_media_dir):
    # Attempt to validate path outside sample_media_dir
    outside_file = "/etc/passwd"
    with pytest.raises(HTTPException) as exc_info:
        validate_file_safety(outside_file)
    assert exc_info.value.status_code in [403, 404]

    # Non-existent relative escape attempt
    traversal_path = sample_media_dir / "../../../etc/passwd"
    with pytest.raises(HTTPException) as exc_info:
        validate_file_safety(traversal_path)
    assert exc_info.value.status_code in [403, 404]


def test_streaming_nonexistent_id(client):
    res = client.get("/api/stream/999999")
    assert res.status_code == 404


def test_invalid_episode_query(client):
    res = client.get("/api/episodes/999999")
    assert res.status_code == 404

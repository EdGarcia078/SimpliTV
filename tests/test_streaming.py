import pytest
from app.services.scanner import scan_library


@pytest.mark.asyncio
async def test_streaming_range_requests(client, test_db, sample_media_dir):
    # Ensure library is scanned
    await scan_library(test_db, sample_media_dir)

    # Get random episode to stream
    res = client.get("/api/episodes/random")
    assert res.status_code == 200
    ep = res.json()
    ep_id = ep["id"]
    file_size = ep["file_size"]

    # 1. HEAD request
    head_res = client.head(f"/api/stream/{ep_id}")
    assert head_res.status_code == 200
    assert head_res.headers.get("accept-ranges") == "bytes"
    assert int(head_res.headers.get("content-length")) == file_size

    # 2. Full Stream (200 OK)
    full_res = client.get(f"/api/stream/{ep_id}")
    assert full_res.status_code == 200
    assert full_res.headers.get("accept-ranges") == "bytes"
    assert int(full_res.headers.get("content-length")) == file_size

    # 3. First 50 bytes (206 Partial Content)
    range_res = client.get(f"/api/stream/{ep_id}", headers={"Range": "bytes=0-49"})
    assert range_res.status_code == 206
    assert range_res.headers.get("content-range") == f"bytes 0-49/{file_size}"
    assert int(range_res.headers.get("content-length")) == 50
    assert len(range_res.content) == 50

    # 4. Open range (206 Partial Content from byte 20 to end)
    range_open = client.get(f"/api/stream/{ep_id}", headers={"Range": "bytes=20-"})
    assert range_open.status_code == 206
    assert range_open.headers.get("content-range") == f"bytes 20-{file_size - 1}/{file_size}"
    assert int(range_open.headers.get("content-length")) == (file_size - 20)

    # 5. Out-of-bounds Range (416 Range Not Satisfiable)
    invalid_range = client.get(f"/api/stream/{ep_id}", headers={"Range": "bytes=99999999-999999999"})
    assert invalid_range.status_code == 416


@pytest.mark.asyncio
async def test_open_stream_stops_when_periodic_access_check_is_revoked(tmp_path):
    """An already-open response must not continue indefinitely after revocation."""
    from app.services.streaming import file_chunk_generator

    media_file = tmp_path / "revocation-test.mp4"
    media_file.write_bytes(b"0123456789")
    checks = iter([True, False])

    chunks = []
    async for chunk in file_chunk_generator(
        media_file,
        0,
        9,
        chunk_size=4,
        access_check=lambda: next(checks, False),
        access_check_interval_bytes=1,
    ):
        chunks.append(chunk)

    assert b"".join(chunks) == b"0123"

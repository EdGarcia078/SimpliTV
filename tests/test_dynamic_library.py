"""
Dynamic library tests: scanner, watcher, ChannelEngine integration.
All use the canonical hierarchy: Channel/Series/Show[/Season]/MediaItem.
"""
import asyncio
import os
import time
from pathlib import Path
import pytest
from sqlmodel import Session, select

from app.models.media import MediaItem
from app.models.channel import Channel, ChannelState
from app.services.scanner import scan_library, upsert_episode_file, remove_episode_by_path
from app.services.channel import ChannelEngine
from app.services.selector import select_next_episode
from app.services.watcher import MediaWatcher, is_file_stable


def create_dummy_video(path: Path, duration: int = 1):
    """Helper to generate a valid lightweight dummy video file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = (
        f"ffmpeg -y -f lavfi -i testsrc=duration={duration}:size=320x240:rate=1 "
        f"-f lavfi -i sine=frequency=1000:duration={duration} "
        f"-c:v libx264 -c:a aac '{path}' > /dev/null 2>&1"
    )
    os.system(cmd)
    if not path.exists() or path.stat().st_size == 0:
        path.write_bytes(b"\x00" * 4096)


def _get_first_channel(session: Session) -> Channel:
    """Return the first channel in the DB (created by scan)."""
    ch = session.exec(select(Channel)).first()
    assert ch is not None, "No channel found in DB"
    return ch


# 1. Scan inicial detecta archivos existentes
@pytest.mark.asyncio
async def test_initial_scan_detects_existing(test_db: Session, sample_media_dir: Path):
    result = await scan_library(test_db, sample_media_dir)
    assert result.total_episodes >= 2
    episodes = test_db.exec(select(MediaItem)).all()
    titles = [e.media_title for e in episodes]
    assert "Bocchi the Rock" in titles
    assert "JoJo" in titles

    # Every episode must have a channel_id
    for ep in episodes:
        assert ep.channel_id is not None


# 2. Crear un nuevo archivo después de iniciar la aplicación: watcher lo detecta
@pytest.mark.asyncio
async def test_watcher_detects_new_file(test_db: Session, sample_media_dir: Path):
    watcher = MediaWatcher(
        media_dir=sample_media_dir,
        debounce_seconds=0.3,
        session_factory=lambda: Session(test_db.bind),
    )
    watcher.start()

    try:
        new_file = sample_media_dir / "Canal 1" / "Series" / "JoJo" / "Season 1" / "S01E03 - Silver Chariot.mp4"
        create_dummy_video(new_file, duration=2)

        # Trigger watcher event queue and wait for debounce worker
        watcher.queue_change(new_file, is_deletion=False)
        await asyncio.sleep(1.5)

        with Session(test_db.bind) as s:
            ep = s.exec(select(MediaItem).where(MediaItem.episode_number == 3)).first()
            assert ep is not None
            assert ep.media_title == "JoJo"
            assert ep.episode_title == "Silver Chariot"
            assert ep.channel_id is not None
    finally:
        watcher.stop()


# 3. Nuevo archivo termina de copiarse: no se procesa prematuramente (file stability check)
def test_file_stability_check(test_temp_dir: Path):
    stable_file = test_temp_dir / "stable_test.mp4"
    create_dummy_video(stable_file, duration=1)
    assert is_file_stable(stable_file, min_check_interval=0.2) is True

    # Non-existent file is unstable
    non_existent = test_temp_dir / "ghost.mp4"
    assert is_file_stable(non_existent, min_check_interval=0.1) is False


# 4. Múltiples eventos para el mismo archivo: no generan duplicados
@pytest.mark.asyncio
async def test_idempotent_multiple_events(test_db: Session, sample_media_dir: Path):
    target = sample_media_dir / "Canal 1" / "Series" / "Bocchi the Rock" / "Season 1" / "S01E01.mp4"
    assert target.exists()

    # Process same file multiple times
    ep1 = await upsert_episode_file(test_db, target, sample_media_dir)
    ep2 = await upsert_episode_file(test_db, target, sample_media_dir)
    ep3 = await upsert_episode_file(test_db, target, sample_media_dir)

    assert ep1 is not None and ep2 is not None and ep3 is not None
    assert ep1.id == ep2.id == ep3.id

    # Verify in DB only 1 record exists with that relative path
    rel = target.relative_to(sample_media_dir).as_posix()
    count = len(test_db.exec(select(MediaItem).where(MediaItem.relative_path == rel)).all())
    assert count == 1


# 5. Modificar un archivo: sus metadatos se actualizan
@pytest.mark.asyncio
async def test_modify_file_updates_metadata(test_db: Session, sample_media_dir: Path):
    target = sample_media_dir / "Canal 1" / "Series" / "Bocchi the Rock" / "Season 1" / "S01E01.mp4"
    ep_before = await upsert_episode_file(test_db, target, sample_media_dir)
    assert ep_before is not None

    # Overwrite file with different duration
    create_dummy_video(target, duration=3)
    ep_after = await upsert_episode_file(test_db, target, sample_media_dir)
    assert ep_after is not None
    assert ep_after.id == ep_before.id
    assert ep_after.duration >= 2.0


# 6. Eliminar un archivo: desaparece de los candidatos de programación
@pytest.mark.asyncio
async def test_delete_file_removes_from_candidates(test_db: Session, sample_media_dir: Path):
    await scan_library(test_db, sample_media_dir)
    channel = _get_first_channel(test_db)

    target = sample_media_dir / "Canal 1" / "Series" / "JoJo" / "Season 1" / "S01E02 - The Prophecy.mp4"
    ep = await upsert_episode_file(test_db, target, sample_media_dir)
    assert ep is not None

    # Remove file from disk and sync deletion
    if target.exists():
        target.unlink()
    deleted = remove_episode_by_path(test_db, target, sample_media_dir)
    assert deleted is True

    # Candidate selection should no longer return it
    candidate = select_next_episode(test_db, channel.id, current_episode_id=None)
    assert candidate is None or candidate.id != ep.id


# 7. Añadir un nuevo episodio: ChannelEngine puede seleccionarlo posteriormente
@pytest.mark.asyncio
async def test_added_episode_becomes_candidate(test_db: Session, sample_media_dir: Path):
    engine = ChannelEngine()
    await scan_library(test_db, sample_media_dir)
    channel = _get_first_channel(test_db)

    # Set all existing episodes to play_count = 5
    for ep in test_db.exec(select(MediaItem)).all():
        ep.play_count = 5
        test_db.add(ep)
    test_db.commit()

    await engine.initialize(test_db)
    state_before = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert state_before is not None

    # Add a brand new show with play_count = 0 under Canal 1
    frieren_file = sample_media_dir / "Canal 1" / "Series" / "Frieren" / "Season 1" / "S01E01 - The Journey Begins.mp4"
    create_dummy_video(frieren_file, duration=5)
    new_ep = await upsert_episode_file(test_db, frieren_file, sample_media_dir)
    assert new_ep is not None

    await engine.notify_library_changed(test_db)

    # Next candidate selection must pick Frieren (play_count 0 vs 5)
    candidate = select_next_episode(test_db, channel.id, current_episode_id=state_before.episode.id)
    assert candidate is not None
    assert candidate.media_title == "Frieren"


# 8. Añadir un episodio mientras otro está siendo emitido: emisión actual continúa sin interrupción
@pytest.mark.asyncio
async def test_broadcast_not_interrupted_by_new_files(test_db: Session, sample_media_dir: Path):
    engine = ChannelEngine()
    await scan_library(test_db, sample_media_dir)
    await engine.initialize(test_db)

    channel = _get_first_channel(test_db)

    state1 = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert state1 is not None
    current_id = state1.episode.id
    current_started_at = state1.started_at

    # Dynamically add another file under same channel
    extra_file = sample_media_dir / "Canal 1" / "Series" / "Chainsaw Man" / "Season 1" / "S01E01.mp4"
    create_dummy_video(extra_file, duration=5)
    await upsert_episode_file(test_db, extra_file, sample_media_dir)
    await engine.notify_library_changed(test_db)

    # Check state again -> currently playing episode & start time are strictly unchanged
    state2 = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert state2 is not None
    assert state2.episode.id == current_id
    assert state2.started_at == current_started_at


# 9. Reiniciar la aplicación después de haber añadido archivos: scan inicial detecta los cambios
@pytest.mark.asyncio
async def test_restart_scan_detects_offline_changes(test_db: Session, sample_media_dir: Path):
    # Add file while "offline" under Canal 1
    offline_file = sample_media_dir / "Canal 1" / "Series" / "Spy x Family" / "Season 1" / "S01E01.mp4"
    create_dummy_video(offline_file, duration=5)

    # Simulate startup scan
    scan_res = await scan_library(test_db, sample_media_dir)
    assert scan_res.total_episodes >= 3

    ep = test_db.exec(select(MediaItem).where(MediaItem.media_title == "Spy x Family")).first()
    assert ep is not None
    assert ep.channel_id is not None


# 10. Ejecutar repetidamente la lógica de sincronización: estado consistente y sin duplicados
@pytest.mark.asyncio
async def test_repeated_sync_consistency(test_db: Session, sample_media_dir: Path):
    await scan_library(test_db, sample_media_dir)
    count1 = len(test_db.exec(select(MediaItem)).all())

    # Scan 5 times repeatedly
    for _ in range(5):
        res = await scan_library(test_db, sample_media_dir)
        assert res.added_count == 0
        assert res.deleted_count == 0

    count2 = len(test_db.exec(select(MediaItem)).all())
    assert count1 == count2


# 11. Biblioteca vacía: comportamiento seguro
@pytest.mark.asyncio
async def test_empty_library_safety(test_db: Session, test_temp_dir: Path):
    import uuid
    empty_dir = test_temp_dir / f"empty_media_{uuid.uuid4().hex}"
    empty_dir.mkdir(parents=True, exist_ok=True)

    result = await scan_library(test_db, empty_dir)
    assert result.total_episodes == 0

    engine = ChannelEngine()
    await engine.initialize(test_db)

    # No channels were created, so no channel_id to query — engine should be empty
    channels = test_db.exec(select(Channel)).all()
    for ch in channels:
        state = await engine.get_current_state(test_db, ch.id)  # type: ignore
        assert state is None


# 12. Un único episodio: comportamiento seguro
@pytest.mark.asyncio
async def test_single_episode_library_safety(test_db: Session, test_temp_dir: Path):
    import uuid
    single_dir = test_temp_dir / f"single_media_{uuid.uuid4().hex}"
    single_file = single_dir / "Canal Test" / "Series" / "OnePiece" / "Season 1" / "S01E01.mp4"
    create_dummy_video(single_file, duration=10)

    result = await scan_library(test_db, single_dir)
    assert result.total_episodes == 1

    engine = ChannelEngine()
    await engine.initialize(test_db)

    channel = _get_first_channel(test_db)
    state = await engine.get_current_state(test_db, channel.id)  # type: ignore
    assert state is not None
    assert state.episode.media_title == "OnePiece"

    # Next candidate should loop back to the same episode safely
    candidate = select_next_episode(test_db, channel.id, current_episode_id=state.episode.id)
    assert candidate is not None
    assert candidate.id == state.episode.id

from datetime import datetime, timezone, timedelta
from sqlmodel import Session
from app.models.media import MediaItem
from app.models.channel import Channel
from app.services.selector import select_next_episode

def test_select_empty_library(test_db: Session):
    ch = Channel(name="Default")
    test_db.add(ch)
    test_db.commit()
    selected = select_next_episode(test_db, ch.id)
    assert selected is None

def test_select_episodes(test_db: Session, monkeypatch):
    ch = Channel(name="Default", batch_size=2, loop=True, start_mode="any")
    test_db.add(ch)
    test_db.commit()

    ep1 = MediaItem(
        channel_id=ch.id,
        media_title="A",
        season_number=1, episode_number=1,
        relative_path="A/1.mp4", file_path="A/1.mp4"
    )
    ep2 = MediaItem(
        channel_id=ch.id,
        media_title="A",
        season_number=1, episode_number=2,
        relative_path="A/2.mp4", file_path="A/2.mp4"
    )
    ep3 = MediaItem(
        channel_id=ch.id,
        media_title="A",
        season_number=1, episode_number=3,
        relative_path="A/3.mp4", file_path="A/3.mp4"
    )
    ep4 = MediaItem(
        channel_id=ch.id,
        media_title="B",
        season_number=1, episode_number=1,
        relative_path="B/1.mp4", file_path="B/1.mp4"
    )
    test_db.add_all([ep1, ep2, ep3, ep4])
    test_db.commit()

    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    # First episode should be A1
    res = select_next_episode(test_db, ch.id)
    assert res.id == ep1.id
    
    # If A1 just played and we have consecutive=1, should pick A2
    res = select_next_episode(test_db, ch.id, ep1.id, consecutive_plays=1)
    assert res.id == ep2.id
    
    # If A2 just played and consecutive=2 (batch size reached), should pick B1
    res = select_next_episode(test_db, ch.id, ep2.id, consecutive_plays=2)
    assert res.id == ep4.id



def test_start_mode_even_and_odd(test_db: Session, monkeypatch):
    """The first episode of a block honors even/odd start mode."""
    ch = Channel(name="Parity", batch_size=2, loop=True, start_mode="even")
    test_db.add(ch)
    test_db.commit()
    test_db.refresh(ch)

    episodes = []
    for number in range(1, 5):
        ep = MediaItem(
            channel_id=ch.id,
            media_title="Parity Show",
            season_number=1,
            episode_number=number,
            relative_path=f"Parity/{number}.mp4",
            file_path=f"Parity/{number}.mp4",
        )
        test_db.add(ep)
        episodes.append(ep)
    test_db.commit()

    # Make random.choice deterministic so the assertion tests filtering,
    # not randomness.
    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])

    even = select_next_episode(test_db, ch.id)
    assert even is not None
    assert even.episode_number % 2 == 0

    ch.start_mode = "odd"
    test_db.add(ch)
    test_db.commit()

    odd = select_next_episode(test_db, ch.id)
    assert odd is not None
    assert odd.episode_number % 2 == 1


def test_start_mode_falls_back_when_requested_parity_is_missing(test_db: Session, monkeypatch):
    """A parity cap must not leave a channel without programming."""
    ch = Channel(name="Only Odds", batch_size=1, loop=True, start_mode="even")
    test_db.add(ch)
    test_db.commit()
    test_db.refresh(ch)

    ep = MediaItem(
        channel_id=ch.id,
        media_title="Odd Show",
        season_number=1,
        episode_number=1,
        relative_path="Odd/1.mp4",
        file_path="Odd/1.mp4",
    )
    test_db.add(ep)
    test_db.commit()

    monkeypatch.setattr("app.services.selector.random.choice", lambda seq: seq[0])
    selected = select_next_episode(test_db, ch.id)
    assert selected is not None
    assert selected.id == ep.id

from sqlmodel import select

from app.models.channel import Channel
from app.models.media import LibraryRevision
from app.services.scanner import compact_channel_display_order


def _seed(session):
    channels = [
        Channel(id=1, name="Canal A", folder_name="Canal A", display_order=1),
        Channel(id=4, name="Canal B", folder_name="Canal B", display_order=2),
        Channel(id=9, name="Canal C", folder_name="Canal C", display_order=3),
    ]
    session.add_all(channels)
    session.commit()
    return channels


def test_admin_reorders_channels_atomically(client, test_db):
    _seed(test_db)
    res = client.put('/api/admin/channels/order', json={"channel_ids": [9, 1, 4]})
    assert res.status_code == 200
    assert [row['id'] for row in res.json()] == [9, 1, 4]

    ordered = test_db.exec(select(Channel).order_by(Channel.display_order, Channel.id)).all()
    assert [channel.id for channel in ordered] == [9, 1, 4]
    assert [channel.display_order for channel in ordered] == [1, 2, 3]

    revision = test_db.get(LibraryRevision, 1)
    assert revision is not None
    assert revision.revision == 1


def test_reorder_rejects_duplicates_and_missing_ids(client, test_db):
    _seed(test_db)
    before = [channel.id for channel in test_db.exec(select(Channel).order_by(Channel.display_order, Channel.id)).all()]

    duplicate = client.put('/api/admin/channels/order', json={"channel_ids": [1, 1, 9]})
    assert duplicate.status_code == 422
    missing = client.put('/api/admin/channels/order', json={"channel_ids": [1, 9]})
    assert missing.status_code == 422

    after = [channel.id for channel in test_db.exec(select(Channel).order_by(Channel.display_order, Channel.id)).all()]
    assert after == before


def test_compaction_preserves_identity_and_only_changes_positions(test_db):
    _seed(test_db)
    channel = test_db.get(Channel, 4)
    test_db.delete(channel)
    test_db.commit()

    compact_channel_display_order(test_db)
    test_db.commit()

    ordered = test_db.exec(select(Channel).order_by(Channel.display_order, Channel.id)).all()
    assert [channel.id for channel in ordered] == [1, 9]
    assert [channel.display_order for channel in ordered] == [1, 2]

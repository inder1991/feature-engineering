import pytest

from featuregen.privacy.crypto_shred import BlobNotFoundError, rotate_blob_key


class FakeKeyManager:
    def __init__(self):
        self.rotated: list[tuple[str, str]] = []

    def destroy(self, kms_key_id):  # pragma: no cover - unused here
        pass

    def rotate(self, old_kms_key_id, object_key):
        self.rotated.append((old_kms_key_id, object_key))
        return old_kms_key_id + "_v2"


def _seed_event(db):
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id, type, "
        "schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES ('evt_1','run','run_1',1,'run_1','RUN_OPENED',1,1,"
        "'{\"subject\":\"s\"}'::jsonb, '{}'::jsonb, '{}'::jsonb, now())"
    )


def test_rotate_updates_key_and_leaves_events_untouched(db):
    _seed_event(db)
    db.execute(
        "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, kms_key_id, status) "
        "VALUES ('blob_r', 'k/blob_r', 'sha256:x', 'pii-erasable', 'k1', 'live')"
    )
    before = db.execute("SELECT count(*), max(event_id) FROM events").fetchone()

    km = FakeKeyManager()
    new_key = rotate_blob_key(db, "blob_r", key_manager=km)

    assert new_key == "k1_v2"
    assert km.rotated == [("k1", "k/blob_r")]
    assert (
        db.execute("SELECT kms_key_id FROM blob_index WHERE blob_id='blob_r'").fetchone()[0]
        == "k1_v2"
    )
    after = db.execute("SELECT count(*), max(event_id) FROM events").fetchone()
    assert after == before  # events never rewritten (§9)

    with pytest.raises(BlobNotFoundError):
        rotate_blob_key(db, "blob_absent", key_manager=km)

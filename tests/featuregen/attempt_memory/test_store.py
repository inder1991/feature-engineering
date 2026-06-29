import pytest

from featuregen.attempt_memory.store import (
    ATTEMPT_DISPOSITIONS,
    AttemptMemoryEntry,
    count_candidates_explored,
    lookup_attempt,
    record_attempt,
)


def test_lookup_missing_returns_none(db):
    assert lookup_attempt(db, "h_absent") is None


def test_record_is_non_pii_exempt_and_upserts_by_hash(db):
    record_attempt(db, definition_hash="h1", disposition="explored", score=0.4, request_id="req_1")
    first = lookup_attempt(db, "h1")
    assert isinstance(first, AttemptMemoryEntry)
    assert first.disposition == "explored"
    assert first.crypto_shred_exempt is True  # §3.9: survives erasure of source bodies

    record_attempt(db, definition_hash="h1", disposition="rejected", reason="leaky", feature_id="feat_1")
    second = lookup_attempt(db, "h1")
    assert second.disposition == "rejected"
    assert second.reason == "leaky"
    assert second.request_id == "req_1"   # earlier value preserved
    assert second.feature_id == "feat_1"  # newly supplied value merged
    seen = db.execute(
        "SELECT first_seen <= last_seen FROM attempt_memory WHERE definition_hash = %s", ("h1",)
    ).fetchone()[0]
    assert seen is True


def test_invalid_disposition_rejected(db):
    assert "explored" in ATTEMPT_DISPOSITIONS
    with pytest.raises(ValueError):
        record_attempt(db, definition_hash="h2", disposition="bogus")


def test_count_candidates_explored_scopes_by_request_and_feature(db):
    record_attempt(db, definition_hash="a", disposition="explored", request_id="req_X")
    record_attempt(db, definition_hash="b", disposition="discarded", request_id="req_X")
    record_attempt(db, definition_hash="c", disposition="explored", request_id="req_Y", feature_id="feat_Z")
    assert count_candidates_explored(db, request_id="req_X") == 2
    assert count_candidates_explored(db, feature_id="feat_Z") == 1

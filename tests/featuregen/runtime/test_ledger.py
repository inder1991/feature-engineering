from __future__ import annotations

import psycopg
import pytest

from featuregen.runtime.ledger import (
    is_processed,
    processed_watermark,
    prune_processed_messages,
    record_processed,
)


def test_record_then_is_processed(db) -> None:
    assert is_processed(db, "m1") is False
    record_processed(
        db, message_id="m1", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=10,
    )
    assert is_processed(db, "m1") is True


def test_duplicate_record_violates_pk(db) -> None:
    record_processed(
        db, message_id="m2", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=10,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        record_processed(
            db, message_id="m2", aggregate="run", aggregate_id="r1",
            result_event_id=None, processed_seq=11,
        )


def test_watermark_zero_when_no_projections(db) -> None:
    assert processed_watermark(db) == 0


def test_prune_deletes_below_min_checkpoint(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO projection_checkpoints (projection_name, checkpoint_seq, head_seq) "
            "VALUES ('p_a', 100, 100), ('p_b', 60, 200)"
        )
    record_processed(
        db, message_id="old", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=50,
    )
    record_processed(
        db, message_id="keep", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=70,
    )
    # watermark = min(100, 60) = 60 -> only processed_seq < 60 is pruned
    assert processed_watermark(db) == 60
    assert prune_processed_messages(db) == 1
    assert is_processed(db, "old") is False
    assert is_processed(db, "keep") is True

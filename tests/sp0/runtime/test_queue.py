from __future__ import annotations

from sp0.runtime.queue import (
    claim_one,
    complete,
    enqueue,
    fail_permanent,
    fail_retryable,
    queue_depth,
    reclaim_stuck_queue,
)


def test_enqueue_idempotent_on_message_id(db) -> None:
    a = enqueue(db, message_id="e1", partition_key="run:r1", handler="h", payload={})
    b = enqueue(db, message_id="e1", partition_key="run:r1", handler="h", payload={})
    assert a == b
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id = 'e1'")
        assert cur.fetchone()[0] == 1


def test_claim_leases_ready_row_and_bumps_attempts(db) -> None:
    enqueue(db, message_id="c1", partition_key="run:r1", handler="h", payload={"k": 1})
    claim = claim_one(db, owner="w1")
    assert claim is not None
    assert claim.message_id == "c1"
    assert claim.handler == "h"
    assert claim.payload == {"k": 1}
    assert claim.attempts == 1
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM queue WHERE message_id = 'c1'")
        status, owner = cur.fetchone()
    assert status == "leased"
    assert owner == "w1"


def test_claim_skips_partition_with_inflight_lease(db) -> None:
    # partition run:r1 already has an in-flight lease; only run:r2 is claimable
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('busy', 'run:r1', 'h', '{}'::jsonb, "
            "'leased', 'w0', now() + interval '1 minute')"
        )
    enqueue(db, message_id="ready_same", partition_key="run:r1", handler="h", payload={})
    enqueue(db, message_id="ready_other", partition_key="run:r2", handler="h", payload={})
    claim = claim_one(db, owner="w1")
    assert claim is not None
    assert claim.message_id == "ready_other"  # run:r1 is blocked by the in-flight lease


def test_claim_returns_none_when_empty(db) -> None:
    assert claim_one(db, owner="w1") is None


def test_complete_sets_done(db) -> None:
    qid = enqueue(db, message_id="d1", partition_key="run:r1", handler="h", payload={})
    claim_one(db, owner="w1")
    complete(db, qid)
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM queue WHERE id = %s", (qid,))
        status, owner = cur.fetchone()
    assert status == "done"
    assert owner is None


def test_fail_retryable_reschedules(db) -> None:
    qid = enqueue(db, message_id="r1", partition_key="run:r1", handler="h", payload={})
    claim_one(db, owner="w1")
    fail_retryable(db, qid, error="boom")
    with db.cursor() as cur:
        cur.execute("SELECT status, last_error FROM queue WHERE id = %s", (qid,))
        status, err = cur.fetchone()
    assert status == "ready"
    assert err == "boom"


def test_fail_retryable_dlqs_at_max_attempts(db) -> None:
    qid = enqueue(db, message_id="r2", partition_key="run:r1", handler="h", payload={})
    with db.cursor() as cur:
        cur.execute("UPDATE queue SET attempts = max_attempts WHERE id = %s", (qid,))
    fail_retryable(db, qid, error="exhausted")
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE id = %s", (qid,))
        assert cur.fetchone()[0] == "dead"


def test_fail_permanent_dlqs(db) -> None:
    qid = enqueue(db, message_id="p1", partition_key="run:r1", handler="h", payload={})
    claim_one(db, owner="w1")
    fail_permanent(db, qid, error="deterministic")
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE id = %s", (qid,))
        assert cur.fetchone()[0] == "dead"


def test_reclaim_stuck_queue(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('stuck', 'run:r1', 'h', '{}'::jsonb, "
            "'leased', 'dead-w', now() - interval '1 minute')"
        )
    assert reclaim_stuck_queue(db) == 1
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM queue WHERE message_id = 'stuck'")
        status, owner = cur.fetchone()
    assert status == "ready"
    assert owner is None


def test_queue_depth_counts_ready_and_leased(db) -> None:
    enqueue(db, message_id="qd1", partition_key="run:r1", handler="h", payload={})
    enqueue(db, message_id="qd2", partition_key="run:r2", handler="h", payload={})
    claim_one(db, owner="w1")  # leases one row; leased rows still count toward depth
    assert queue_depth(db) == 2
    assert queue_depth(db, partition_key="run:r1") == 1
    assert queue_depth(db, partition_key="run:nope") == 0

from __future__ import annotations

import pytest

from featuregen.runtime.queue import (
    claim_one,
    complete,
    enqueue,
    fail_permanent,
    fail_retryable,
    queue_depth,
    reclaim_stuck_queue,
)


@pytest.fixture
def make_queue_row(db):
    """Local factory: enqueue a ready row then force its attempt count, returning the row id."""

    def _make(*, message_id: str, attempts: int) -> int:
        qid = enqueue(
            db, message_id=message_id, partition_key=f"run:{message_id}", handler="h", payload={}
        )
        with db.cursor() as cur:
            cur.execute("UPDATE queue SET attempts=%s WHERE id=%s", (attempts, qid))
        return qid

    return _make


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


@pytest.mark.parametrize("handler", ["runtime.auto_park", "runtime.repair_exhausted"])
def test_claim_one_never_claims_a_control_signal_row(db, handler) -> None:
    """A control-signal row (runtime.auto_park / runtime.repair_exhausted) is OWNED by the dedicated
    control poller. claim_one MUST exclude it so process_one — on ANY worker — can never steal it and
    convert a safety park into an unknown-handler DLQ. Even as the ONLY ready row it stays 'ready'."""
    enqueue(
        db,
        message_id=f"cs:{handler}",
        partition_key="run:rp1",
        handler=handler,
        payload={"run_id": "rp1"},
    )
    assert claim_one(db, owner="w1") is None
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id = %s", (f"cs:{handler}",))
        assert cur.fetchone()[0] == "ready"  # left for the control poller, never leased


def test_claim_one_still_claims_a_normal_row_alongside_a_control_signal(db) -> None:
    """The exclusion is surgical and the two consumers are COMPLEMENTARY: a normal step row is still
    claimable even when a higher-priority control-signal row sits ahead of it in the queue."""
    enqueue(
        db,
        message_id="cs_ap",
        partition_key="run:rp1",
        handler="runtime.auto_park",
        payload={"run_id": "rp1"},
        priority=1,  # would sort FIRST, but claim_one must skip it
    )
    enqueue(db, message_id="normal1", partition_key="run:rp2", handler="advance", payload={})
    claim = claim_one(db, owner="w1")
    assert claim is not None
    assert claim.message_id == "normal1"
    assert claim.handler == "advance"


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


def test_fail_permanent_increments_dlq_counter(db) -> None:
    """m5: a permanent (poison) DLQ transition emits a dedicated queue.dlq counter, distinct from
    the transient queue.fail backoff counter."""
    from featuregen.runtime.observability import counters

    counters.reset()
    qid = enqueue(db, message_id="dlq_perm", partition_key="run:r1", handler="h", payload={})
    fail_permanent(db, qid, error="deterministic")
    assert counters.snapshot()["counters"].get("queue.dlq", 0) == 1


def test_fail_retryable_at_budget_increments_dlq_counter(db) -> None:
    """m5: the retry-EXHAUSTED DLQ path (attempts hit max_attempts) also emits queue.dlq — but a
    transient backoff reschedule (attempts below budget) does NOT."""
    from featuregen.runtime.observability import counters

    counters.reset()
    # below budget: reschedule with backoff, NOT a DLQ
    reschedule = enqueue(db, message_id="dlq_rt_ok", partition_key="run:r1", handler="h", payload={})
    with db.cursor() as cur:
        cur.execute("UPDATE queue SET attempts = 1 WHERE id = %s", (reschedule,))
    fail_retryable(db, reschedule, error="transient")
    assert counters.snapshot()["counters"].get("queue.dlq", 0) == 0  # no DLQ on backoff

    # at budget: retry-exhausted DLQ
    exhausted = enqueue(db, message_id="dlq_rt", partition_key="run:r2", handler="h", payload={})
    with db.cursor() as cur:
        cur.execute("UPDATE queue SET attempts = max_attempts WHERE id = %s", (exhausted,))
    fail_retryable(db, exhausted, error="exhausted")
    assert counters.snapshot()["counters"].get("queue.dlq", 0) == 1


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


def test_fail_retryable_applies_jitter(db, make_queue_row) -> None:
    """Two rows failed at the same attempt count must not reschedule to the identical instant —
    jitter breaks the thundering herd after an outage (review MINOR #23)."""
    from featuregen.runtime.queue import fail_retryable

    a = make_queue_row(message_id="m_a", attempts=2)
    b = make_queue_row(message_id="m_b", attempts=2)
    fail_retryable(db, a, error="x")
    fail_retryable(db, b, error="x")
    with db.cursor() as cur:
        cur.execute("SELECT available_at FROM queue WHERE id IN (%s,%s) ORDER BY id", (a, b))
        ts = [r[0] for r in cur.fetchall()]
    assert ts[0] != ts[1]  # jitter makes collisions astronomically unlikely

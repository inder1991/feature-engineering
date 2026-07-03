from __future__ import annotations

import pytest

from featuregen.runtime.outbox import (
    insert_outbox_message,
    outbox_messages_for_events,
    partition_key_for,
)


def test_partition_key_per_aggregate(db, seed_run_event) -> None:
    ev = seed_run_event("run_p1")
    assert partition_key_for(ev) == "run:run_p1"


def test_derive_one_message_per_event(db, seed_run_event) -> None:
    ev = seed_run_event("run_d1", type="STEP_TRIGGER")
    msgs = outbox_messages_for_events([ev])
    assert len(msgs) == 1
    m = msgs[0]
    assert m.message_id == ev.event_id
    assert m.partition_key == "run:run_d1"
    assert m.topic == "STEP_TRIGGER"
    assert m.caused_by_event == ev.event_id
    assert m.payload["event_id"] == ev.event_id
    assert m.payload["run_id"] == "run_d1"


def test_insert_is_idempotent_on_message_id(db, seed_run_event) -> None:
    ev = seed_run_event("run_i1")
    (m,) = outbox_messages_for_events([ev])
    first = insert_outbox_message(db, m)
    second = insert_outbox_message(db, m)  # duplicate publish -> same row
    assert first == second
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox WHERE message_id = %s", (m.message_id,))
        assert cur.fetchone()[0] == 1


def test_partition_key_for_unknown_aggregate_raises() -> None:
    class _Fake:
        aggregate = "bogus"
        run_id = feature_id = request_id = aggregate_id = "x"

    with pytest.raises(ValueError):
        partition_key_for(_Fake())  # type: ignore[arg-type]


from featuregen.runtime.outbox import (
    make_queue_publisher,
    outbox_pending_depth,
    reclaim_stuck_outbox,
    relay_publish_batch,
)
from featuregen.runtime.queue import enqueue


def _seed_pending(db, message_id: str, topic: str = "STEP_TRIGGER") -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload) "
            "VALUES (%s, 'run:r1', %s, '{}'::jsonb)",
            (message_id, topic),
        )


def test_relay_publishes_then_marks_sent(db) -> None:
    _seed_pending(db, "rp1")
    published: list[str] = []

    def publish(conn, msg) -> None:
        published.append(msg.message_id)

    assert relay_publish_batch(db, publish, owner="relay1") == 1
    assert published == ["rp1"]
    with db.cursor() as cur:
        cur.execute("SELECT status, sent_at FROM outbox WHERE message_id = 'rp1'")
        status, sent_at = cur.fetchone()
    assert status == "sent"
    assert sent_at is not None


def test_relay_backoff_on_publish_failure(db) -> None:
    _seed_pending(db, "rp2")

    def publish(conn, msg) -> None:
        raise RuntimeError("downstream down")

    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute("SELECT status, attempts, last_error FROM outbox WHERE message_id = 'rp2'")
        status, attempts, last_error = cur.fetchone()
    assert status == "pending"
    assert attempts == 1
    assert "downstream down" in last_error


def test_relay_backoff_applies_jitter(db) -> None:
    """m7: two outbox rows failing at the SAME attempt count must not reschedule to the identical
    next_attempt_at — jitter must reach the outbox reschedule (the outbox analogue of the queue
    test_fail_retryable_applies_jitter). now() is fixed within the test transaction, so any
    difference between the two next_attempt_at values comes purely from jitter in the backoff."""
    _seed_pending(db, "jit_a")
    _seed_pending(db, "jit_b")

    def publish(conn, msg) -> None:
        raise RuntimeError("downstream down")

    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute(
            "SELECT next_attempt_at FROM outbox WHERE message_id IN ('jit_a', 'jit_b') "
            "ORDER BY message_id"
        )
        ts = [r[0] for r in cur.fetchall()]
    assert ts[0] != ts[1]  # jitter makes an identical reschedule astronomically unlikely


def test_relay_routes_to_dlq_at_max_attempts(db) -> None:
    _seed_pending(db, "rp3")
    with db.cursor() as cur:
        cur.execute("UPDATE outbox SET attempts = max_attempts - 1 WHERE message_id = 'rp3'")

    def publish(conn, msg) -> None:
        raise RuntimeError("still down")

    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute("SELECT status FROM outbox WHERE message_id = 'rp3'")
        assert cur.fetchone()[0] == "dead"


def test_reclaim_stuck_outbox(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('rs1', 'run:r1', 'T', '{}'::jsonb, "
            "'leased', 'dead-relay', now() - interval '1 minute')"
        )
    assert reclaim_stuck_outbox(db) == 1
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM outbox WHERE message_id = 'rs1'")
        status, owner = cur.fetchone()
    assert status == "pending"
    assert owner is None


def test_pending_depth_counts_pending_and_leased(db) -> None:
    _seed_pending(db, "pd1")
    _seed_pending(db, "pd2")
    assert outbox_pending_depth(db) == 2


def test_make_queue_publisher_enqueues_routed_topics_only(db) -> None:
    _seed_pending(db, "qp1", topic="STEP_TRIGGER")
    _seed_pending(db, "qp2", topic="UNROUTED")
    publish = make_queue_publisher({"STEP_TRIGGER": "my_handler"})
    assert relay_publish_batch(db, publish, owner="relay1") == 2  # both marked sent
    with db.cursor() as cur:
        cur.execute("SELECT message_id, handler FROM queue ORDER BY message_id")
        rows = cur.fetchall()
    assert rows == [("qp1", "my_handler")]  # qp2 unrouted -> no queue row


def test_route_required_unrouted_topic_dlqs_not_drains(db) -> None:
    # A topic declared ROUTE-REQUIRED but with no route is a LOUD failure: relay backs off -> DLQ
    # and counts it, instead of silently marking it 'sent' (SP-0.5 round-2 outbox route policy).
    from featuregen.runtime.observability import counters

    _seed_pending(db, "rr1", topic="MUST_ROUTE")
    with db.cursor() as cur:
        cur.execute("UPDATE outbox SET attempts = max_attempts - 1 WHERE message_id = 'rr1'")
    counters.reset()
    publish = make_queue_publisher({}, route_required=frozenset({"MUST_ROUTE"}))
    relay_publish_batch(db, publish, owner="relay1")
    with db.cursor() as cur:
        cur.execute("SELECT status FROM outbox WHERE message_id = 'rr1'")
        assert cur.fetchone()[0] == "dead"  # not silently drained to 'sent'
    assert counters.snapshot()["counters"].get("outbox.unrouted.MUST_ROUTE", 0) >= 1


def test_unrouted_topic_not_required_still_drains_to_sent(db) -> None:
    # The SAME unrouted topic, when NOT route-required, is a benign drain (marked 'sent') — the
    # empty-default policy keeps every current event topic draining, so nothing breaks today.
    _seed_pending(db, "dr1", topic="MUST_ROUTE")
    publish = make_queue_publisher({})  # empty route_required
    assert relay_publish_batch(db, publish, owner="relay1") == 1
    with db.cursor() as cur:
        cur.execute("SELECT status FROM outbox WHERE message_id = 'dr1'")
        assert cur.fetchone()[0] == "sent"


def test_backpressure_holds_outbox_pending_without_failing(db) -> None:
    _seed_pending(db, "bp1", topic="STEP_TRIGGER")  # partition run:r1
    # saturate the run:r1 worker-queue partition up to the admission limit
    enqueue(db, message_id="bp_pre", partition_key="run:r1", handler="h", payload={})
    publish = make_queue_publisher({"STEP_TRIGGER": "h"}, max_partition_depth=1)
    # nothing is published while the partition is at capacity -> durable waiting
    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute("SELECT status, attempts FROM outbox WHERE message_id='bp1'")
        status, attempts = cur.fetchone()
    assert status == "pending"  # held durably, not failed
    assert attempts == 0  # backpressure is NOT a failure: no attempt bump, no DLQ
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id='bp1'")
        assert cur.fetchone()[0] == 0  # not enqueued while saturated

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from featuregen.contracts import Disposition
from featuregen.runtime.retries import (
    OUTBOX_SPEC,
    QUEUE_SPEC,
    compute_backoff,
    record_delivery_outcome,
    within_budget,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def test_compute_backoff_bounded_and_deterministic():
    rng = random.Random(7)
    d = compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, rng=rng)
    assert 0.0 <= d <= 4.0  # window = min(30, 1*2**2) = 4
    assert compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, rng=random.Random(7)) == d


def test_compute_backoff_rejects_zero_attempts():
    with pytest.raises(ValueError):
        compute_backoff(0, base_seconds=1.0, cap_seconds=30.0, rng=random.Random(1))


def test_within_budget_caps():
    assert within_budget(attempts=3, max_attempts=12, started_at=NOW, now=NOW,
                         max_elapsed_seconds=3600) is True
    assert within_budget(attempts=12, max_attempts=12, started_at=NOW, now=NOW,
                         max_elapsed_seconds=3600) is False
    past = NOW - timedelta(hours=2)
    assert within_budget(attempts=1, max_attempts=12, started_at=past, now=NOW,
                         max_elapsed_seconds=3600) is False


def _insert_queue(conn, *, attempts=0, max_attempts=12):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, attempts, max_attempts) "
            "VALUES ('m1','run:1','h','{}'::jsonb,%s,%s) RETURNING id",
            (attempts, max_attempts),
        )
        return cur.fetchone()[0]


def _insert_outbox(conn, *, attempts=0, max_attempts=12):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, attempts, max_attempts) "
            "VALUES ('o1','run:1','t','{}'::jsonb,%s,%s) RETURNING id",
            (attempts, max_attempts),
        )
        return cur.fetchone()[0]


def _status(conn, table, row_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {table} WHERE id=%s", (row_id,))
        return cur.fetchone()[0]


def test_ok_disposition_is_rejected(conn):
    rid = _insert_queue(conn)
    with pytest.raises(ValueError):
        record_delivery_outcome(
            conn, QUEUE_SPEC, rid, disposition=Disposition.OK, error=None,
            started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
            rng=random.Random(1),
        )
    assert _status(conn, "queue", rid) == "ready"  # untouched; OK is not a failure


def test_permanent_goes_to_dlq(conn):
    rid = _insert_queue(conn)
    status = record_delivery_outcome(
        conn, QUEUE_SPEC, rid, disposition=Disposition.PERMANENT, error="bad",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "dead" and _status(conn, "queue", rid) == "dead"


def test_retryable_within_budget_reschedules(conn):
    rid = _insert_queue(conn, attempts=0)
    status = record_delivery_outcome(
        conn, QUEUE_SPEC, rid, disposition=Disposition.RETRYABLE, error="503",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "ready"
    with conn.cursor() as cur:
        cur.execute("SELECT attempts, available_at FROM queue WHERE id=%s", (rid,))
        attempts, available_at = cur.fetchone()
    assert attempts == 1 and available_at >= NOW


def test_retryable_exhausted_goes_to_dlq(conn):
    rid = _insert_queue(conn, attempts=11, max_attempts=12)
    status = record_delivery_outcome(
        conn, QUEUE_SPEC, rid, disposition=Disposition.RETRYABLE, error="503",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "dead"


def test_outbox_uses_next_attempt_at(conn):
    rid = _insert_outbox(conn)
    status = record_delivery_outcome(
        conn, OUTBOX_SPEC, rid, disposition=Disposition.RETRYABLE, error="x",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "pending"
    with conn.cursor() as cur:
        cur.execute("SELECT next_attempt_at FROM outbox WHERE id=%s", (rid,))
        assert cur.fetchone()[0] >= NOW

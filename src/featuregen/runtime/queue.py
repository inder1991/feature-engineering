from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from featuregen.runtime.backoff import compute_backoff


class BackpressureError(RuntimeError):
    """Admission control signal (§5.2): a partition is at capacity. Raised by the queue
    publisher; the relay treats it as durable waiting (leave the outbox row pending, no attempt
    bump, no DLQ), never as a delivery failure."""


@dataclass(frozen=True, slots=True)
class QueueClaim:
    """A leased worker-queue item (§5.2)."""

    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    conn: psycopg.Connection,
    *,
    message_id: str,
    partition_key: str,
    handler: str,
    payload: Mapping[str, Any],
    available_at: datetime | None = None,
    priority: int = 100,
) -> int:
    """Insert a 'ready' work item; idempotent on message_id. Returns the row id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, available_at, priority) "
            "VALUES (%s, %s, %s, %s, COALESCE(%s, now()), %s) "
            "ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (message_id, partition_key, handler, Json(payload), available_at, priority),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0])
        cur.execute("SELECT id FROM queue WHERE message_id = %s", (message_id,))
        return int(cur.fetchone()[0])


def claim_one(
    conn: psycopg.Connection, *, owner: str, lease_seconds: int = 30
) -> QueueClaim | None:
    """Claim one ready item via FOR UPDATE SKIP LOCKED, excluding partitions that already
    have an in-flight lease (per-aggregate serialization, §5.2). Bumps attempts atomically.

    Concurrent-claimer race: two workers can both pass the partition-exclusion subquery before
    either commits its lease; `queue_one_inflight_per_partition` then rejects the loser with a
    UniqueViolation. We run the claim in a SAVEPOINT and translate that violation into
    "nothing claimed" (return None) so it never aborts the caller's outer transaction."""
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    "  SELECT id FROM queue"
                    "   WHERE status='ready' AND available_at <= now()"
                    "     AND partition_key NOT IN (SELECT partition_key FROM queue WHERE status='leased')"
                    "   ORDER BY priority, available_at, id"
                    "   FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    "  lease_expires_at = now() + make_interval(secs => %s), attempts = q.attempts + 1 "
                    "FROM c WHERE q.id = c.id RETURNING q.*",
                    (owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None  # lost the per-partition in-flight race; nothing claimed this round
    if row is None:
        return None
    return QueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


def complete(conn: psycopg.Connection, queue_id: int) -> None:
    """Mark a claimed item done and release its lease."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL WHERE id=%s",
            (queue_id,),
        )


def fail_retryable(conn: psycopg.Connection, queue_id: int, *, error: str) -> None:
    """Transient failure: reschedule with backoff, or DLQ once attempts hit the budget (§5.6)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT attempts, max_attempts FROM queue WHERE id=%s", (queue_id,))
        row = cur.fetchone()
        if row["attempts"] >= row["max_attempts"]:
            cur.execute(
                "UPDATE queue SET status='dead', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL WHERE id=%s",
                (error, queue_id),
            )
        else:
            delay = compute_backoff(row["attempts"], jitter=0.0)
            cur.execute(
                "UPDATE queue SET status='ready', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL, available_at = now() + make_interval(secs => %s) "
                "WHERE id=%s",
                (error, delay, queue_id),
            )


def fail_permanent(conn: psycopg.Connection, queue_id: int, *, error: str) -> None:
    """Deterministic failure: skip delivery retry, route to DLQ (§5.6)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='dead', last_error=%s, lease_owner=NULL, "
            "lease_expires_at=NULL WHERE id=%s",
            (error, queue_id),
        )


def reclaim_stuck_queue(conn: psycopg.Connection) -> int:
    """Return expired-lease items to 'ready' so a crashed worker's items resume (§5.7)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='leased' AND lease_expires_at < now()"
        )
        return cur.rowcount


def queue_depth(conn: psycopg.Connection, *, partition_key: str | None = None) -> int:
    """In-flight backlog (ready+leased), globally or for one partition. The relay's admission
    control consults this for §5.2 backpressure (bound per-partition work-in-progress)."""
    with conn.cursor() as cur:
        if partition_key is None:
            cur.execute("SELECT count(*) FROM queue WHERE status IN ('ready', 'leased')")
        else:
            cur.execute(
                "SELECT count(*) FROM queue WHERE status IN ('ready', 'leased') "
                "AND partition_key = %s",
                (partition_key,),
            )
        return int(cur.fetchone()[0])

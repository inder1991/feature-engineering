from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

import psycopg
from psycopg.types.json import Json


class BackpressureError(RuntimeError):
    """Admission control signal (§5.2): a partition is at capacity. Raised by the queue
    publisher; the relay treats it as durable waiting (leave the outbox row pending, no attempt
    bump, no DLQ), never as a delivery failure."""


def enqueue(
    conn: psycopg.Connection,
    *,
    message_id: str,
    partition_key: str,
    handler: str,
    payload: Mapping[str, Any],
    available_at: Optional[datetime] = None,
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


def queue_depth(
    conn: psycopg.Connection, *, partition_key: Optional[str] = None
) -> int:
    """In-flight backlog (ready+leased), globally or for one partition. The relay's admission
    control consults this for §5.2 backpressure (bound per-partition work-in-progress)."""
    with conn.cursor() as cur:
        if partition_key is None:
            cur.execute(
                "SELECT count(*) FROM queue WHERE status IN ('ready', 'leased')"
            )
        else:
            cur.execute(
                "SELECT count(*) FROM queue WHERE status IN ('ready', 'leased') "
                "AND partition_key = %s",
                (partition_key,),
            )
        return int(cur.fetchone()[0])

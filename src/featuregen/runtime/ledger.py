from __future__ import annotations

import psycopg


def is_processed(conn: psycopg.Connection, message_id: str) -> bool:
    """True if this message id has already produced its one effect (§5.3)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
        return cur.fetchone() is not None


def record_processed(
    conn: psycopg.Connection,
    *,
    message_id: str,
    aggregate: str,
    aggregate_id: str,
    result_event_id: str | None,
    processed_seq: int,
) -> None:
    """Record that message_id was processed at global_seq=processed_seq (§5.3).
    The PRIMARY KEY on message_id makes a concurrent duplicate roll back the tx."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_messages "
            "(message_id, aggregate, aggregate_id, result_event_id, processed_seq) "
            "VALUES (%s, %s, %s, %s, %s)",
            (message_id, aggregate, aggregate_id, result_event_id, processed_seq),
        )


def processed_watermark(conn: psycopg.Connection) -> int:
    """Min applied checkpoint across all projections; ledger rows at/above this are
    still needed for in-flight projection replay (§5.3). 0 when no projections exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MIN(checkpoint_seq), 0) FROM projection_checkpoints")
        return int(cur.fetchone()[0])


def prune_processed_messages(conn: psycopg.Connection) -> int:
    """Delete ledger rows below the watermark; returns the number deleted."""
    watermark = processed_watermark(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM processed_messages WHERE processed_seq < %s", (watermark,))
        return cur.rowcount

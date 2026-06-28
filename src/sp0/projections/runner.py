from __future__ import annotations

from psycopg.rows import dict_row

from sp0.contracts import DbConn, Projection, ProjectionApplyError
from sp0.events.serde import row_to_event


def _ensure_checkpoint(conn: DbConn, name: str, is_analytics: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, is_analytics)
            VALUES (%s, %s)
            ON CONFLICT (projection_name) DO NOTHING
            """,
            (name, is_analytics),
        )


def _head_seq(conn: DbConn) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT max(global_seq) AS h FROM events")
        row = cur.fetchone()
    return row["h"] or 0


def run_projection(conn: DbConn, projection: Projection, *, batch: int = 500) -> int:
    """Consume events with global_seq > checkpoint_seq in order, calling apply(); advance the
    checkpoint to the last applied event. Returns the count applied.

    NOTE: this Task-12 version handles the happy path only. The §3.6 fail-closed degraded-halt and
    analytics fail-open branches (and the `projection_degraded` marking) are added in Task 13,
    where a failing test drives them in."""
    _ensure_checkpoint(conn, projection.name, projection.is_analytics)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints "
            "WHERE projection_name = %s FOR UPDATE",
            (projection.name,),
        )
        checkpoint = cur.fetchone()["checkpoint_seq"]
        cur.execute(
            "SELECT * FROM events WHERE global_seq > %s ORDER BY global_seq ASC LIMIT %s",
            (checkpoint, batch),
        )
        rows = cur.fetchall()

    applied = 0
    last_seq = checkpoint
    for row in rows:
        event = row_to_event(row)
        projection.apply(conn, event)
        last_seq = event.global_seq
        applied += 1

    head = _head_seq(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projection_checkpoints "
            "SET checkpoint_seq = %s, head_seq = %s, updated_at = now() "
            "WHERE projection_name = %s",
            (last_seq, head, projection.name),
        )
    return applied


def projection_lag(conn: DbConn, name: str) -> int:
    """Live head_seq - checkpoint_seq for the named projection."""
    head = _head_seq(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return head
    return head - row["checkpoint_seq"]


def read_as_of(conn: DbConn, name: str) -> int:
    """The global_seq the projection's data is current as-of (its checkpoint)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
            (name,),
        )
        row = cur.fetchone()
    return 0 if row is None else row["checkpoint_seq"]

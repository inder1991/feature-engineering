from __future__ import annotations

from psycopg.errors import LockNotAvailable
from psycopg.rows import dict_row

from featuregen.contracts import DbConn, Projection, ProjectionApplyError
from featuregen.events.serde import row_to_event
from featuregen.runtime.observability import counters


def try_lock_checkpoint_nowait(conn: DbConn, name: str) -> bool:
    """Try to take the named projection's ``projection_checkpoints`` row lock WITHOUT blocking.

    Returns True when the lock is held (to the end of the caller's transaction, so a following
    ``run_projection`` re-locks the same row for free) and False when a concurrent holder — typically
    an in-flight ``ingest_upload`` whose in-tx ``_drain_projection`` holds the 'overlay' checkpoint
    row until commit — already has it. NEVER blocks and NEVER leaves the caller's transaction
    aborted: the ``FOR UPDATE NOWAIT`` runs in its OWN savepoint, so a ``LockNotAvailable`` (SQLSTATE
    55P03) rolls back cleanly (a row lock survives the savepoint RELEASE on success; only a ROLLBACK
    drops it).

    The human confirm surfaces' synchronous drain-then-project uses this so a confirm whose drain
    cannot get the checkpoint lock DEFERS to its fail-closed projection-lag path instead of
    serializing behind a multi-minute ingest transaction (composition audit finding [9])."""
    try:
        with conn.transaction():
            conn.execute(
                "SELECT 1 FROM projection_checkpoints WHERE projection_name = %s FOR UPDATE NOWAIT",
                (name,))
        return True
    except LockNotAvailable:
        return False


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

    Fail-closed for a normal projection (§3.6): a poison event HALTS the projection (no advance
    past it) and marks the affected aggregate in `projection_degraded`. An analytics projection
    (`is_analytics`) fails OPEN: it records the skip in `projection_skips` (+ the `projection.skip`
    counter) and advances past the poison, so a completeness gap is never silent."""
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
        if projection.is_analytics:
            try:
                with conn.transaction():  # savepoint: discard the poison event's partial writes
                    projection.apply(conn, event)
            except ProjectionApplyError as exc:
                # Fail open (§3.6): analytics projections still advance past a poison event, but
                # the skip must NOT be silent (review MAJOR #20 — a BCBS 239 accuracy gap). Record
                # it durably in the skip ledger, in a SEPARATE statement outside the rolled-back
                # savepoint, so the omission is auditable. ON CONFLICT DO NOTHING keeps it
                # idempotent under re-runs of the same poison event.
                conn.execute(
                    "INSERT INTO projection_skips (projection_name, event_global_seq, reason) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (projection.name, event.global_seq, str(exc)[:500]),
                )
                counters.incr("projection.skip")  # surface the completeness gap as a metric
                last_seq = event.global_seq  # fail open, but the skip is now durable + auditable
                continue
            last_seq = event.global_seq
            applied += 1
        else:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT proj_apply")
            try:
                projection.apply(conn, event)
            except ProjectionApplyError as exc:
                # Fail-closed (§3.6): discard ANY partial writes the apply body made before it
                # raised (ROLLBACK TO SAVEPOINT), so no partial projection state survives; then
                # mark the affected aggregate degraded from the carried payload in a SEPARATE
                # statement (this marker persists), and HALT without advancing past the poison.
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT proj_apply")
                _mark_degraded(conn, projection.name, aggregate=exc.aggregate,
                               aggregate_id=exc.aggregate_id, reason=exc.reason, event=event)
                break
            except Exception as exc:  # noqa: BLE001 — an UNEXPECTED apply failure (e.g. a malformed
                # event whose payload is missing a key the projection indexes) must fail closed the
                # SAME way, not escape uncaught: that would crash the run past the poison leaving NO
                # degraded marker. Roll back partial writes, mark the poison EVENT's aggregate degraded,
                # and HALT. (ProjectionApplyError is the projection's SIGNALLED failure; this is the net.)
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT proj_apply")
                _mark_degraded(conn, projection.name, aggregate=event.aggregate,
                               aggregate_id=event.aggregate_id,
                               reason=f"unexpected {type(exc).__name__}: {exc}"[:500], event=event)
                break
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT proj_apply")
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


def _mark_degraded(conn: DbConn, projection_name: str, *, aggregate: str, aggregate_id: str,
                   reason: str, event) -> None:
    """Record the affected aggregate in the generic degraded ledger (§3.6). Idempotent under re-runs
    of the same poison event. aggregate/aggregate_id come from the carried ProjectionApplyError for a
    typed failure, or from the poison EVENT itself for an unexpected exception."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projection_degraded
                (projection_name, aggregate, aggregate_id, reason, poison_event_id, poison_seq)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (projection_name, aggregate, aggregate_id)
            DO UPDATE SET reason = EXCLUDED.reason,
                          poison_event_id = EXCLUDED.poison_event_id,
                          poison_seq = EXCLUDED.poison_seq,
                          degraded_at = now()
            """,
            (projection_name, aggregate, aggregate_id, reason, event.event_id, event.global_seq),
        )


def rebuild_projection(conn: DbConn, projection: Projection) -> None:
    """reset() then deterministically replay from global_seq=0 (§3.6)."""
    projection.reset(conn)
    # Clear this projection's stale skip ledger BEFORE replay: after a fix-and-replay, leftover
    # projection_skips rows would report a phantom completeness gap (m4). A clean replay either
    # re-records a genuine skip or leaves the ledger empty.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM projection_skips WHERE projection_name = %s", (projection.name,)
        )
    _ensure_checkpoint(conn, projection.name, projection.is_analytics)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projection_checkpoints SET checkpoint_seq = 0, head_seq = 0, updated_at = now() "
            "WHERE projection_name = %s",
            (projection.name,),
        )
    while run_projection(conn, projection) > 0:
        pass
    # Clear stale degraded markers ONLY on a clean replay to head (SP-0.5 round-2 review): if the
    # rebuild caught the projection fully up (lag 0, so no poison re-halted it), any surviving
    # marker is stale and the operator who fixed the cause + rebuilt should get the aggregate
    # un-blocked WITHOUT a separate resolve_degraded. A partial replay (still poisoned -> lag > 0)
    # keeps its markers (fail-closed).
    if projection_lag(conn, projection.name) == 0:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM projection_degraded WHERE projection_name = %s", (projection.name,)
            )


_REPAIR_REGISTRY: dict[str, Projection] = {}


def register_projection_for_repair(name: str, projection: Projection) -> None:
    """Register a projection under its name so `resolve_degraded` can re-run it to PROVE health
    before clearing a degraded marker (SP-0.5 round-2). Idempotent — last registration wins."""
    _REPAIR_REGISTRY[name] = projection


def projection_for_repair(name: str) -> Projection | None:
    """The projection registered under `name`, or None if none is (resolve then fail-closes)."""
    return _REPAIR_REGISTRY.get(name)


def _checkpoint_seq(conn: DbConn, name: str) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
            (name,),
        )
        row = cur.fetchone()
    return int(row["checkpoint_seq"]) if row else 0


def advance_projection_past(
    conn: DbConn, projection: Projection, aggregate: str, aggregate_id: str
) -> tuple[bool, int | None]:
    """Replay the projection to completion, then evaluate health for (aggregate, aggregate_id).
    Returns `(healthy, poison_seq)`:
      * `(False, None)` — still stuck (checkpoint < the current poison): the caller must REFUSE.
      * `(True, None)`  — no degraded marker remains for this aggregate: healthy, nothing to delete.
      * `(True, P)`     — proven past poison_seq P: the caller deletes EXACTLY that marker.

    Bounded-loop replay (SP-0.5 round-2 review #2): `run_projection` applies at most `batch` events
    per call, so on a long stream a SINGLE call may not even reach the poison — loop until it stops
    advancing (reached head or re-halted). Re-reading the marker AFTER the replay is load-bearing
    (review): a second-stage poison re-halts + re-marks the SAME row at a LATER seq, so the pre-run
    snapshot cannot be trusted. Returning the exact proven poison_seq lets the caller delete only
    that marker (review #1), never a concurrently-inserted fresh one."""
    while run_projection(conn, projection) > 0:
        pass
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT poison_seq FROM projection_degraded "
            "WHERE projection_name = %s AND aggregate = %s AND aggregate_id = %s",
            (projection.name, aggregate, aggregate_id),
        )
        marker = cur.fetchone()
    if marker is None:
        return (True, None)  # no marker for this aggregate after the replay — healthy, nothing to delete
    poison_seq = marker["poison_seq"]
    if _checkpoint_seq(conn, projection.name) >= poison_seq:
        return (True, poison_seq)  # proven past this exact poison
    return (False, None)  # still stuck below the (current) poison — refuse


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

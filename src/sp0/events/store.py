from __future__ import annotations

import datetime as _dt
from dataclasses import replace
from typing import Mapping, Optional

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ulid import ULID

from sp0.contracts import ConcurrencyError, DbConn, EventEnvelope, NewEvent
from sp0.events.registry import event_registry
from sp0.events.serde import identity_to_jsonb, provenance_to_jsonb, row_to_event
from sp0.privacy.classification import assert_no_inline_pii

_INSERT = """
INSERT INTO events (
    event_id, aggregate, aggregate_id, stream_version,
    request_id, feature_id, run_id, type, schema_version, table_version,
    actor, payload, provenance, caused_by, occurred_at
) VALUES (
    %(event_id)s, %(aggregate)s, %(aggregate_id)s, %(stream_version)s,
    %(request_id)s, %(feature_id)s, %(run_id)s, %(type)s, %(schema_version)s, %(table_version)s,
    %(actor)s, %(payload)s, %(provenance)s, %(caused_by)s, %(occurred_at)s
)
RETURNING global_seq, recorded_at
"""

# A single constant key for a transaction-scoped advisory lock that serializes global_seq
# allocation across ALL concurrent appends (any aggregate). global_seq is allocated by the
# table DEFAULT nextval(...) at INSERT time, but each append commits in its own transaction
# later; without serialization a higher global_seq can COMMIT before a lower one, letting
# run_projection() advance its checkpoint past the not-yet-committed lower seq and PERMANENTLY
# skip it (violates §3.2 "no gaps" / §3.6 fail-closed). Holding this lock from just-before
# allocation until the caller's transaction ends forces allocation order == commit order, so
# the global_seq sequence a projection observes is gapless. (Correctness over throughput; the
# spec defers allocator tuning.)
_GLOBAL_SEQ_LOCK_KEY = 4_201_873_355_201_001  # arbitrary fixed key, unique to sp0 seq alloc


def append_event(
    conn: DbConn,
    new_event: NewEvent,
    *,
    expected_version: int,
    table_version: int,
) -> EventEnvelope:
    """Append one event inside the caller's OPEN transaction (§5.1). Sets
    stream_version = expected_version + 1. Raises ConcurrencyError if the stream is not exactly
    at expected_version (stale OR ahead-of-head), and maps a lost UNIQUE race to ConcurrencyError
    via a savepoint so the caller's transaction stays usable. Validates payload first."""
    registry = event_registry()
    registry.assert_writable(new_event.type, new_event.schema_version)
    registry.validate(new_event.type, new_event.schema_version, new_event.payload)
    # §9 invariant: no raw PII/secrets inline. Enforced here (not just via a helper) so it holds
    # for EVERY caller, regardless of whether they remembered to scan first. Cheap/deterministic.
    assert_no_inline_pii(new_event.payload)

    # OCC pre-check: the stream must currently be EXACTLY at expected_version. This rejects
    # both stale (current > expected) and ahead-of-head (current < expected, gap) without
    # touching the connection's transaction state.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT coalesce(max(stream_version), 0) AS v FROM events "
            "WHERE aggregate = %s AND aggregate_id = %s",
            (new_event.aggregate, new_event.aggregate_id),
        )
        current = cur.fetchone()["v"]
    if current != expected_version:
        raise ConcurrencyError(
            f"{new_event.aggregate}:{new_event.aggregate_id} at stream_version {current}, "
            f"expected {expected_version}"
        )

    event_id = f"evt_{ULID()}"
    stream_version = expected_version + 1
    occurred_at = new_event.occurred_at or _dt.datetime.now(_dt.timezone.utc)
    payload = dict(new_event.payload)
    params = {
        "event_id": event_id,
        "aggregate": new_event.aggregate,
        "aggregate_id": new_event.aggregate_id,
        "stream_version": stream_version,
        "request_id": new_event.request_id,
        "feature_id": new_event.feature_id,
        "run_id": new_event.run_id,
        "type": new_event.type,
        "schema_version": new_event.schema_version,
        "table_version": table_version,
        "actor": Jsonb(identity_to_jsonb(new_event.actor)),
        "payload": Jsonb(payload),
        "provenance": Jsonb(provenance_to_jsonb(new_event.provenance)),
        "caused_by": new_event.caused_by,
        "occurred_at": occurred_at,
    }
    # Serialize global_seq allocation with commit order (§3.2 no-gaps / §3.6 fail-closed):
    # take the transaction-scoped advisory lock IMMEDIATELY before allocating global_seq. It is
    # held until the caller's top-level transaction commits/rolls back (savepoints below do not
    # release it), so concurrent cross-aggregate appends commit in the same order they allocated.
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_GLOBAL_SEQ_LOCK_KEY,))

    try:
        with conn.transaction():  # savepoint: a concurrent racer that wins the UNIQUE keeps
            with conn.cursor(row_factory=dict_row) as cur:  # THIS connection usable
                cur.execute(_INSERT, params)
                row = cur.fetchone()
    except UniqueViolation as exc:
        if "events_optimistic_concurrency" in str(exc):
            raise ConcurrencyError(
                f"{new_event.aggregate}:{new_event.aggregate_id} lost a concurrent append at "
                f"expected_version {expected_version}"
            ) from exc
        raise

    return EventEnvelope(
        event_id=event_id,
        global_seq=row["global_seq"],
        aggregate=new_event.aggregate,
        aggregate_id=new_event.aggregate_id,
        stream_version=stream_version,
        type=new_event.type,
        schema_version=new_event.schema_version,
        table_version=table_version,
        actor=new_event.actor,
        payload=payload,
        provenance=new_event.provenance,
        occurred_at=occurred_at,
        recorded_at=row["recorded_at"],
        request_id=new_event.request_id,
        feature_id=new_event.feature_id,
        run_id=new_event.run_id,
        caused_by=new_event.caused_by,
    )


def load_stream(
    conn: DbConn,
    aggregate: str,
    aggregate_id: str,
    *,
    upto_seq: Optional[int] = None,
    expected: Optional[Mapping[str, int]] = None,
) -> list[EventEnvelope]:
    """Load one aggregate instance's stream in stream_version order, upcasting each event
    to the consumer's expected schema_version via the registry (§3.3)."""
    sql = (
        "SELECT * FROM events "
        "WHERE aggregate = %(aggregate)s AND aggregate_id = %(aggregate_id)s"
    )
    params: dict[str, object] = {"aggregate": aggregate, "aggregate_id": aggregate_id}
    if upto_seq is not None:
        sql += " AND global_seq <= %(upto_seq)s"
        params["upto_seq"] = upto_seq
    sql += " ORDER BY stream_version ASC"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    registry = event_registry()
    out: list[EventEnvelope] = []
    for row in rows:
        event = row_to_event(row)
        if expected and event.type in expected:
            target = expected[event.type]
            if target != event.schema_version:
                upcast_payload = registry.upcast(
                    event.type, event.payload, event.schema_version, target
                )
                event = replace(event, payload=dict(upcast_payload), schema_version=target)
        out.append(event)
    return out

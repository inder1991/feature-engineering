from __future__ import annotations

import datetime as _dt

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ulid import ULID

from sp0.contracts import DbConn, EventEnvelope, NewEvent
from sp0.events.registry import event_registry
from sp0.events.serde import identity_to_jsonb, provenance_to_jsonb

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


def append_event(
    conn: DbConn,
    new_event: NewEvent,
    *,
    expected_version: int,
    table_version: int,
) -> EventEnvelope:
    """Append one event inside the caller's OPEN transaction (§5.1). Allocates global_seq +
    event_id and sets stream_version = expected_version + 1. (OCC conflict handling is added
    in Task 10.) Validates payload against the registry before insert."""
    registry = event_registry()
    registry.assert_writable(new_event.type, new_event.schema_version)
    registry.validate(new_event.type, new_event.schema_version, new_event.payload)

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
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_INSERT, params)
        row = cur.fetchone()

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

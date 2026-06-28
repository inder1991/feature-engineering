from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import psycopg
from psycopg.types.json import Json

from sp0.contracts import EventEnvelope


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A to-be-published transactional-outbox message (§5.2)."""

    message_id: str
    partition_key: str
    topic: str
    payload: Mapping[str, Any]
    caused_by_event: str | None = None


def partition_key_for(event: EventEnvelope) -> str:
    """Aggregate-key partition (§5.2): feature-/request-stream events (run_id null)
    still get per-aggregate ordering."""
    if event.aggregate == "run":
        return f"run:{event.run_id or event.aggregate_id}"
    if event.aggregate == "feature":
        return f"feature:{event.feature_id or event.aggregate_id}"
    if event.aggregate == "request":
        return f"request:{event.request_id or event.aggregate_id}"
    raise ValueError(f"unknown aggregate {event.aggregate!r}")


def outbox_messages_for_events(
    events: Iterable[EventEnvelope],
) -> tuple[OutboxMessage, ...]:
    """One outbox row per committed event; message_id = event_id (idempotency key)."""
    out: list[OutboxMessage] = []
    for e in events:
        out.append(
            OutboxMessage(
                message_id=e.event_id,
                partition_key=partition_key_for(e),
                topic=e.type,
                payload={
                    "event_id": e.event_id,
                    "aggregate": e.aggregate,
                    "aggregate_id": e.aggregate_id,
                    "run_id": e.run_id,
                    "feature_id": e.feature_id,
                    "request_id": e.request_id,
                    "type": e.type,
                    "global_seq": e.global_seq,
                    "stream_version": e.stream_version,
                },
                caused_by_event=e.event_id,
            )
        )
    return tuple(out)


def insert_outbox_message(conn: psycopg.Connection, msg: OutboxMessage) -> int:
    """Insert one outbox row inside the caller's open tx; idempotent on message_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, caused_by_event) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (msg.message_id, msg.partition_key, msg.topic, Json(msg.payload), msg.caused_by_event),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0])
        cur.execute("SELECT id FROM outbox WHERE message_id = %s", (msg.message_id,))
        return int(cur.fetchone()[0])

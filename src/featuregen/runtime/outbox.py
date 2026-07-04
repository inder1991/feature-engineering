from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from featuregen.contracts import EventEnvelope
from featuregen.runtime.backoff import compute_backoff
from featuregen.runtime.observability import counters
from featuregen.runtime.queue import BackpressureError, enqueue, queue_depth


class UnroutedOutboxTopic(Exception):
    """Raised by the queue publisher when a topic declared ROUTE-REQUIRED has no configured route.
    It is a LOUD delivery failure (relay backs off -> DLQ), distinct from a topic that is
    intentionally drain-only (no route, not required -> silent no-op). SP-0.5 round-2."""


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
    if event.aggregate == "overlay_fact":
        return f"overlay_fact:{event.overlay_fact_id or event.aggregate_id}"
    if event.aggregate == "feature_contract":
        return f"feature_contract:{event.feature_contract_id or event.aggregate_id}"
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


def relay_publish_batch(
    conn: psycopg.Connection,
    publish: Callable[[psycopg.Connection, OutboxMessage], None],
    *,
    owner: str,
    lease_seconds: int = 30,
    batch: int = 100,
) -> int:
    """Three-step leased relay (§5.2). The relay is a BACKGROUND DAEMON, not a §5.1 step
    participant: it OWNS its transactions. Each `with conn.transaction()` below is a durable
    COMMIT when the relay runs on its own autocommit connection (production) and a SAVEPOINT
    under the per-test transactional `db` fixture.

      Step 1 (own tx): lease a batch of `pending` rows (`FOR UPDATE SKIP LOCKED`) and COMMIT,
        so the lease is durable — a relay crash leaves a 'stuck' leased row that
        reclaim_stuck_outbox returns to 'pending'.
      Step 2 (no tx): call `publish` for each leased row (the external side effect).
      Step 3 (own tx): mark the row 'sent' and COMMIT. A crash between Step 2 and Step 3
        leaves the row 'leased' -> reclaimed -> re-published: a harmless at-least-once
        duplicate (§5.3).

    Publish failures back off ('pending') or route to DLQ ('dead') once attempts are
    exhausted; a BackpressureError is durable waiting ('pending', short delay, NO attempt
    bump, NO DLQ)."""
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE outbox SET status='leased', lease_owner=%s, "
                "lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE id IN (SELECT id FROM outbox WHERE status='pending' AND next_attempt_at <= now() "
                "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s) RETURNING *",
                (owner, lease_seconds, batch),
            )
            leased = cur.fetchall()

    sent = 0
    for row in leased:
        msg = OutboxMessage(
            message_id=row["message_id"],
            partition_key=row["partition_key"],
            topic=row["topic"],
            payload=row["payload"],
            caused_by_event=row["caused_by_event"],
        )
        try:
            publish(conn, msg)
        except BackpressureError as bp:
            # Durable waiting (§5.2): downstream is saturated. Return the row to 'pending'
            # with a delay WITHOUT bumping attempts or DLQ'ing — it is not a failure.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "UPDATE outbox SET status='pending', last_error=%s, lease_owner=NULL, "
                    "lease_expires_at=NULL, next_attempt_at = now() + make_interval(secs => %s) "
                    "WHERE id=%s",
                    (str(bp), lease_seconds, row["id"]),
                )
            continue
        except Exception as exc:  # noqa: BLE001 — failure classification is intentional
            if isinstance(exc, UnroutedOutboxTopic):
                # A route-required topic with no route: surface it LOUD so an operator configures
                # the route, then fall through to the normal backoff/DLQ handling below.
                counters.incr(f"outbox.unrouted.{row['topic']}")
            attempts = row["attempts"] + 1
            with conn.transaction(), conn.cursor() as cur:
                if attempts >= row["max_attempts"]:
                    cur.execute(
                        "UPDATE outbox SET status='dead', attempts=%s, last_error=%s, "
                        "lease_owner=NULL, lease_expires_at=NULL WHERE id=%s",
                        (attempts, str(exc), row["id"]),
                    )
                else:
                    delay = compute_backoff(attempts)  # default jitter=0.5 (review MINOR #23)
                    cur.execute(
                        "UPDATE outbox SET status='pending', attempts=%s, last_error=%s, "
                        "lease_owner=NULL, lease_expires_at=NULL, "
                        "next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
                        (attempts, str(exc), delay, row["id"]),
                    )
            continue
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("UPDATE outbox SET status='sent', sent_at=now() WHERE id=%s", (row["id"],))
        sent += 1
    return sent


def reclaim_stuck_outbox(conn: psycopg.Connection) -> int:
    """Return expired-lease rows to 'pending' (§5.2 stuck detection / §5.7 recovery)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE outbox SET status='pending', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='leased' AND lease_expires_at < now()"
        )
        return cur.rowcount


def outbox_pending_depth(conn: psycopg.Connection) -> int:
    """Backlog (pending+leased) — a backpressure signal for the relay (§5.2)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox WHERE status IN ('pending', 'leased')")
        return int(cur.fetchone()[0])


def make_queue_publisher(
    route: Mapping[str, str],
    *,
    max_partition_depth: int | None = None,
    route_required: frozenset[str] = frozenset(),
) -> Callable[[psycopg.Connection, OutboxMessage], None]:
    """Build a `publish` that turns a routed outbox topic into a worker-queue row. When
    `max_partition_depth` is set, it is admission control (§5.2 backpressure): if the target
    partition already holds that many `ready`+`leased` queue items, it raises BackpressureError
    so the relay leaves the outbox row durably `pending` (durable waiting) until the worker
    queue drains — bounding per-partition backlog without dropping or failing work.

    Route policy (SP-0.5 round-2): `commit_step` writes an outbox row for EVERY event, and most
    topics are NOT meant to fan out (no route -> intentional no-op drain). `route_required` names
    the topics that MUST fan out: an unrouted topic in that set is a LOUD failure
    (UnroutedOutboxTopic -> relay DLQ + `outbox.unrouted.<topic>` counter), never a silent drain.
    The default set is empty, so today every event topic drains and nothing breaks; the loud path
    engages only once a real fan-out topic is declared route-required but left unrouted."""

    def publish(conn: psycopg.Connection, msg: OutboxMessage) -> None:
        handler = route.get(msg.topic)
        if handler is None:
            if msg.topic in route_required:
                raise UnroutedOutboxTopic(msg.topic)  # must fan out but no route configured
            return  # drain-only topic: no internal step handler; nothing to enqueue
        if max_partition_depth is not None and (
            queue_depth(conn, partition_key=msg.partition_key) >= max_partition_depth
        ):
            raise BackpressureError(
                f"partition {msg.partition_key!r} at capacity ({max_partition_depth})"
            )
        enqueue(
            conn,
            message_id=msg.message_id,
            partition_key=msg.partition_key,
            handler=handler,
            payload=msg.payload,
        )

    return publish

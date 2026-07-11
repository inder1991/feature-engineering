"""Append-only, replayable field-decision events — the generic-field decision log (spec §5.2).

This is the persistence primitive for the *generic / advisory* fields a resolver decides on (e.g.
``concept``): each resolution, human confirmation, rejection, or staling is recorded as one
immutable ``field_decision_event`` row. Typed facts (grain / availability / approved_join) do NOT
live here — they stay in the ``OVERLAY_FACT_*`` events. This is the decision log that makes the
authority kernel replayable: the resolver's outputs are persisted rather than recomputed.

Write-once: a supersession is a NEW row (``event_type=confirmed/superseded`` with
``supersedes_event_id`` pointing at the prior decision), NEVER an in-place update. Replaying
:func:`read_field_decisions` in ``created_at`` order reconstructs a field's full decision history.

SCOPE (Phase 0): this task builds the persistence PRIMITIVE only. Wiring the Task-4 resolver to
EMIT one of these on each resolution is Phase 1 work — deferred until producers actually write
evidence — and is intentionally NOT done here.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn


class FieldDecisionEventType(StrEnum):
    """The lifecycle transitions recorded for a generic field's decision (§5.2).

    ``RESOLVED`` — the resolver picked a value from the active evidence set. ``CONFIRMED`` — a human
    vouched for a value (may supersede a prior ``RESOLVED``). ``REJECTED`` — a value was turned
    down. ``STALED`` — the inputs drifted and the prior decision no longer holds. ``SUPERSEDED`` — a
    newer decision replaced this one. Each is a fresh row; ``supersedes_event_id`` links the chain.
    """

    RESOLVED = "resolved"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STALED = "staled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class FieldDecisionEvent:
    decision_event_id: str
    logical_ref: str
    field_name: str
    event_type: str
    selected_evidence_ids: tuple[str, ...]
    evidence_set_hash: str
    display_value_hash: str | None
    load_bearing_value_hash: str | None
    conflict_status: str
    reason_codes: tuple[str, ...]
    field_policy_version: str
    resolver_version: str
    actor_ref: str | None
    supersedes_event_id: str | None
    created_at: object


def record_field_decision(
    conn: DbConn,
    *,
    logical_ref: str,
    field_name: str,
    event_type: FieldDecisionEventType | str,
    selected_evidence_ids: Sequence[str],
    evidence_set_hash: str,
    display_value_hash: str | None,
    load_bearing_value_hash: str | None,
    conflict_status: str,
    reason_codes: Sequence[str],
    field_policy_version: str,
    resolver_version: str,
    actor_ref: str | None,
    supersedes_event_id: str | None,
    now: datetime | None = None,
) -> str:
    """Record one immutable field-decision event (§5.2) and return its ``decision_event_id``.

    Append-only: every call mints a fresh ``fde_`` id and INSERTs a new row — there is no update
    path. A supersession is expressed as a NEW row whose ``supersedes_event_id`` points at the prior
    decision (never by mutating that prior row). ``event_type`` is validated against
    :class:`FieldDecisionEventType`. ``selected_evidence_ids`` and ``reason_codes`` are persisted as
    jsonb lists and round-trip as tuples on read.

    ``created_at`` is written explicitly from a per-call ``datetime.now(UTC)`` (the ``now`` seam)
    rather than the SQL ``DEFAULT now()``, so decisions recorded within a single transaction get
    distinct, monotonically increasing timestamps and :func:`read_field_decisions` replays them in
    the true order they were recorded (SQL ``now()`` is the constant transaction-start time)."""
    now = now or datetime.now(UTC)
    decision_event_id = mint_id("fde")
    event_type_value = FieldDecisionEventType(event_type).value  # validate + normalize to its value
    conn.execute(
        """
        INSERT INTO field_decision_event
            (decision_event_id, logical_ref, field_name, event_type, selected_evidence_ids,
             evidence_set_hash, display_value_hash, load_bearing_value_hash, conflict_status,
             reason_codes, field_policy_version, resolver_version, actor_ref, supersedes_event_id,
             created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_event_id, logical_ref, field_name, event_type_value,
            Jsonb(list(selected_evidence_ids)),
            evidence_set_hash, display_value_hash, load_bearing_value_hash, conflict_status,
            Jsonb(list(reason_codes)), field_policy_version, resolver_version, actor_ref,
            supersedes_event_id, now,
        ),
    )
    return decision_event_id


def read_field_decisions(
    conn: DbConn, logical_ref: str, field_name: str
) -> list[FieldDecisionEvent]:
    """Replay every decision recorded for ``(logical_ref, field_name)``, oldest first.

    Ordered by ``created_at`` then ``decision_event_id`` (a deterministic tiebreak for decisions
    that land in the same instant). Reconstructs the field's full, append-only decision history."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT * FROM field_decision_event
            WHERE logical_ref = %s AND field_name = %s
            ORDER BY created_at, decision_event_id
            """,
            (logical_ref, field_name),
        )
        rows = cur.fetchall()
    return [
        FieldDecisionEvent(
            decision_event_id=row["decision_event_id"],
            logical_ref=row["logical_ref"],
            field_name=row["field_name"],
            event_type=row["event_type"],
            # jsonb round-trips as a list; the frozen record's contract is an immutable tuple.
            selected_evidence_ids=tuple(row["selected_evidence_ids"]),
            evidence_set_hash=row["evidence_set_hash"],
            display_value_hash=row["display_value_hash"],
            load_bearing_value_hash=row["load_bearing_value_hash"],
            conflict_status=row["conflict_status"],
            reason_codes=tuple(row["reason_codes"]),
            field_policy_version=row["field_policy_version"],
            resolver_version=row["resolver_version"],
            actor_ref=row["actor_ref"],
            supersedes_event_id=row["supersedes_event_id"],
            created_at=row["created_at"],
        )
        for row in rows
    ]

"""Conflict-review lifecycle + audit history (spec §10).

When competing evidence disagrees on a governed field, a conflict is OPENed for human review. Its
identity is a STABLE ``fingerprint`` (see ``conflict_fingerprint``) so a re-upload of the same
disagreement UPDATES / REOPENs the existing conflict rather than duplicating it. This is distinct
from ingest quarantine (validation rows) and the fact STALE/REVERIFY flow (per-fact re-verify).

Every state change — the initial OPEN, a system REOPEN on re-upload, and each human transition —
appends one immutable ``conflict_review_event`` (``from_state`` -> ``to_state``, ``actor``,
``reason``), so the full review history is auditable via :func:`conflict_events`.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn

# Auto-generated OPEN / REOPEN transitions are not driven by a human, so their audit event records a
# synthetic actor. Human transitions carry the caller's real ``actor`` (see :func:`transition_conflict`).
_SYSTEM_ACTOR = "system"


class ConflictState(StrEnum):
    """Where a conflict sits in its review lifecycle (§10).

    ``OPEN`` — newly detected, awaiting review. ``ACKNOWLEDGED`` — a reviewer has picked it up.
    ``RESOLVED`` — the disagreement was settled. ``DISMISSED`` — judged a non-issue. ``STALE`` — the
    underlying evidence moved on before resolution. ``REOPENED`` — a re-upload re-detected a
    conflict that had already reached a terminal state.
    """

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    STALE = "stale"
    REOPENED = "reopened"


# The terminal-ish states that a re-upload of the same fingerprint REOPENs (rather than leaving as-is).
# A conflict still under active review (OPEN / ACKNOWLEDGED / REOPENED) is left untouched on re-upload.
_TERMINAL_STATES = frozenset(
    {ConflictState.RESOLVED.value, ConflictState.DISMISSED.value, ConflictState.STALE.value}
)


@dataclass(frozen=True, slots=True)
class ConflictReview:
    conflict_id: str
    fingerprint: str
    logical_ref: str
    field_name: str
    severity: str
    competing_evidence_ids: tuple[str, ...]
    competing_value_hashes: tuple[str, ...]
    state: str
    owner: str | None
    created_at: object
    updated_at: object


@dataclass(frozen=True, slots=True)
class ConflictReviewEvent:
    event_id: str
    conflict_id: str
    from_state: str | None
    to_state: str
    actor: str
    reason: str | None
    created_at: object


def conflict_fingerprint(
    logical_ref: str,
    field_name: str,
    competing_value_hashes: Sequence[str],
    field_policy_version: str,
) -> str:
    """Stable identity for one field-level conflict: sha256 over
    ``json([logical_ref, field_name, sorted(competing_value_hashes), field_policy_version])``.

    The value hashes are SORTED so the fingerprint is independent of the order the competing values
    are discovered — the same disagreement always yields the same fingerprint. The field policy
    version participates so that a policy change re-scopes the conflict (a new policy is a new
    fingerprint, hence a fresh conflict rather than a reopen of the old one)."""
    payload = json.dumps(
        [logical_ref, field_name, sorted(competing_value_hashes), field_policy_version],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _append_event(
    conn: DbConn,
    conflict_id: str,
    *,
    from_state: str | None,
    to_state: str,
    actor: str,
    reason: str | None,
    now: datetime,
) -> str:
    """Append one immutable audit event for a conflict transition; return the minted event id."""
    event_id = mint_id("cflev")
    conn.execute(
        """
        INSERT INTO conflict_review_event
            (event_id, conflict_id, from_state, to_state, actor, reason, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (event_id, conflict_id, from_state, to_state, actor, reason, now),
    )
    return event_id


def open_or_reopen_conflict(
    conn: DbConn,
    *,
    fingerprint: str,
    logical_ref: str,
    field_name: str,
    severity: str,
    competing_evidence_ids: Sequence[str],
    competing_value_hashes: Sequence[str],
    now: datetime | None = None,
) -> str:
    """Open a new conflict OR reopen an existing terminal one — idempotent on ``fingerprint``.

    First sighting of a fingerprint mints a ``cfl_`` id, writes state OPEN, and appends the initial
    ``conflict_review_event`` (to_state OPEN). A later call with the SAME fingerprint never creates a
    duplicate row (``fingerprint`` is UNIQUE); it returns the existing ``conflict_id``. If that
    existing conflict had reached a terminal state (resolved / dismissed / stale), the re-upload
    transitions it to REOPENED and appends a REOPENED event; a conflict still under active review is
    left untouched (no state change, no event). Returns the ``conflict_id`` in every case."""
    now = now or datetime.now(UTC)
    conflict_id = mint_id("cfl")
    # INSERT ... ON CONFLICT DO NOTHING is the idempotency backstop: the UNIQUE(fingerprint)
    # constraint guarantees at most one row per fingerprint, and RETURNING yields the freshly minted
    # id ONLY when this call actually inserted. A conflict (no returned row) means the fingerprint
    # already exists, and we branch to the reopen-if-terminal path below — see the escalation note in
    # the task brief: a SELECT-then-branch inside one transaction is the sanctioned approach.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO conflict_review
                (conflict_id, fingerprint, logical_ref, field_name, severity,
                 competing_evidence_ids, competing_value_hashes, state, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO NOTHING
            RETURNING conflict_id
            """,
            (
                conflict_id, fingerprint, logical_ref, field_name, severity,
                Jsonb(list(competing_evidence_ids)), Jsonb(list(competing_value_hashes)),
                ConflictState.OPEN.value, now, now,
            ),
        )
        inserted = cur.fetchone()
    if inserted is not None:
        _append_event(
            conn, conflict_id, from_state=None, to_state=ConflictState.OPEN.value,
            actor=_SYSTEM_ACTOR, reason=None, now=now,
        )
        return conflict_id
    # Fingerprint already exists: resolve the existing conflict and reopen it only from a terminal state.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT conflict_id, state FROM conflict_review WHERE fingerprint = %s", (fingerprint,)
        )
        existing = cur.fetchone()
    assert existing is not None  # UNIQUE + DO NOTHING conflict guarantees the row exists
    existing_id = existing["conflict_id"]
    existing_state = existing["state"]
    if existing_state in _TERMINAL_STATES:
        conn.execute(
            "UPDATE conflict_review SET state = %s, updated_at = %s WHERE conflict_id = %s",
            (ConflictState.REOPENED.value, now, existing_id),
        )
        _append_event(
            conn, existing_id, from_state=existing_state, to_state=ConflictState.REOPENED.value,
            actor=_SYSTEM_ACTOR, reason=None, now=now,
        )
    return existing_id


def transition_conflict(
    conn: DbConn,
    conflict_id: str,
    new_state: ConflictState | str,
    *,
    actor: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """Move a conflict to ``new_state`` and record the edge in its audit history.

    Updates ``conflict_review.state`` AND appends one ``conflict_review_event`` carrying the
    ``from_state`` -> ``to_state`` edge, the ``actor`` who drove it, and an optional ``reason``.
    Raises ``KeyError`` if ``conflict_id`` is unknown."""
    now = now or datetime.now(UTC)
    to_state = ConflictState(new_state).value  # validate + normalize (accepts a member or its value)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT state FROM conflict_review WHERE conflict_id = %s", (conflict_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown conflict_id {conflict_id!r}")
    from_state = row["state"]
    conn.execute(
        "UPDATE conflict_review SET state = %s, updated_at = %s WHERE conflict_id = %s",
        (to_state, now, conflict_id),
    )
    _append_event(
        conn, conflict_id, from_state=from_state, to_state=to_state,
        actor=actor, reason=reason, now=now,
    )


def read_conflict(conn: DbConn, conflict_id: str) -> ConflictReview:
    """Resolve a ``conflict_id`` to its current conflict record. Raises ``KeyError`` if unknown."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM conflict_review WHERE conflict_id = %s", (conflict_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown conflict_id {conflict_id!r}")
    return ConflictReview(
        conflict_id=row["conflict_id"],
        fingerprint=row["fingerprint"],
        logical_ref=row["logical_ref"],
        field_name=row["field_name"],
        severity=row["severity"],
        # jsonb round-trips as a list; the frozen record's contract is an immutable tuple.
        competing_evidence_ids=tuple(row["competing_evidence_ids"]),
        competing_value_hashes=tuple(row["competing_value_hashes"]),
        state=row["state"],
        owner=row["owner"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def conflict_events(conn: DbConn, conflict_id: str) -> list[ConflictReviewEvent]:
    """The full audit history for a conflict, oldest first (ordered by created_at, then event_id as a
    deterministic tiebreak for events that land in the same instant)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT * FROM conflict_review_event
            WHERE conflict_id = %s
            ORDER BY created_at, event_id
            """,
            (conflict_id,),
        )
        rows = cur.fetchall()
    return [
        ConflictReviewEvent(
            event_id=row["event_id"],
            conflict_id=row["conflict_id"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            actor=row["actor"],
            reason=row["reason"],
            created_at=row["created_at"],
        )
        for row in rows
    ]

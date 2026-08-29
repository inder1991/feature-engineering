"""B2: persistence for ``JoinValidationPolicyRevisionV1`` — the guard policy a preview compiles in
(migration 1136).

Step 3 built the contract; B1 stored its id on every physical execution plan and said so plainly:
``join_validation_policy_revision_id`` is "stored with NO existence check: its store is 1136's
(B2/B2b) and does not exist yet — an honest unchecked pin, never a fabricated FK." This module is
that store, and with it the pin becomes CHECKABLE — :func:`require_join_validation_policy` is wired
into B1's ``ensure_physical_execution_plan``, so a physical plan can no longer be persisted naming a
guard policy nobody declared.

**The hash is semantic; the declarer is provenance.** ``content_payload()`` deliberately omits
``declared_by``/``declared_at``, so re-declaring one policy under another name is the SAME revision
and the SAME row. The declarer columns therefore keep the FIRST declaration's provenance (the insert
is ``ON CONFLICT DO NOTHING``) — which is what "the same policy declared twice is one policy" means,
and is why they are columns here rather than a side-car: unlike a logical plan's hypothesis, they
identify nothing and change nothing.

**Verifying load, not an existence probe** (B1's doctrine): every read REBUILDS the typed contract
from the stored payload and RECOMPUTES its content hash through the contract's own constructor. A
row that cannot reproduce its own identity is corruption and is never served — the fan-out law
included, because reconstruction runs ``__post_init__``.

Store discipline: ``conn`` positional, everything else keyword-only; typed refusals BEFORE any SQL;
content-addressed idempotency (``ON CONFLICT DO NOTHING`` + a content-verified read-back).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from featuregen.overlay.upload.planner.physical_plan_v1 import (
    JOIN_VALIDATION_POLICY_ID_PREFIX,
    CoverageDenominatorV1,
    CoverageNumeratorV1,
    FanOutControlOperatorV1,
    JoinOrientationV1,
    JoinValidationPolicyRevisionV1,
    NullKeyBehaviorV1,
    SnapshotSelectionRuleV1,
    UnmatchedRowBehaviorV1,
)

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

__all__ = [
    "JOIN_VALIDATION_POLICY_ID_PREFIX",
    "JoinPolicyPersistenceDefect",
    "JoinPolicyStoreConflict",
    "ensure_join_validation_policy",
    "join_validation_policy_from_payload",
    "load_join_validation_policy",
    "require_join_validation_policy",
]

_VALIDATION_POLICY_CONTRACT = "join_validation_policy_revision_v1"


class JoinPolicyPersistenceDefect(ValueError):
    """A refused request: a foreign type, a malformed payload, or a pin naming a policy nobody
    declared — raised BEFORE any write."""


class JoinPolicyStoreConflict(RuntimeError):
    """The store and the table disagree — a row that cannot rebuild its own contract or reproduce
    its own content hash. Corruption, never served."""


def _field(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise JoinPolicyPersistenceDefect(
            f"join validation policy payload is missing required field {key!r}")
    return payload[key]


def join_validation_policy_from_payload(
    payload: Mapping[str, Any], *, declared_by: str, declared_at: str,
) -> JoinValidationPolicyRevisionV1:
    """Rebuild the contract from its canonical payload plus the provenance columns.

    The exact inverse of ``content_payload()``; the provenance arrives separately because it never
    appears in the payload."""
    if not isinstance(payload, Mapping):
        raise JoinPolicyPersistenceDefect(
            f"join validation policy payload must be a JSON object, got {type(payload).__name__}")
    if payload.get("contract") != _VALIDATION_POLICY_CONTRACT:
        raise JoinPolicyPersistenceDefect(
            f"join validation policy payload must carry contract "
            f"{_VALIDATION_POLICY_CONTRACT!r}, got {payload.get('contract')!r} — a payload is "
            "rebuilt as the contract it declares, never as the one a caller hoped for")
    operator = payload.get("fan_out_control_operator")
    return JoinValidationPolicyRevisionV1(
        null_key_behavior=NullKeyBehaviorV1(_field(payload, "null_key_behavior")),
        unmatched_row_behavior=UnmatchedRowBehaviorV1(_field(payload, "unmatched_row_behavior")),
        coverage_numerator=CoverageNumeratorV1(_field(payload, "coverage_numerator")),
        coverage_denominator=CoverageDenominatorV1(_field(payload, "coverage_denominator")),
        minimum_coverage_ratio=_field(payload, "minimum_coverage_ratio"),
        orientation=JoinOrientationV1(_field(payload, "orientation")),
        max_matches_per_left_row=_field(payload, "max_matches_per_left_row"),
        snapshot_selection_rule=SnapshotSelectionRuleV1(
            _field(payload, "snapshot_selection_rule")),
        applies_to_final_grain_aggregate=_field(payload, "applies_to_final_grain_aggregate"),
        fan_out_control_operator=(None if operator is None
                                  else FanOutControlOperatorV1(operator)),
        declared_by=declared_by,
        declared_at=declared_at,
    )


def ensure_join_validation_policy(conn: DbConn, *,
                                  policy: JoinValidationPolicyRevisionV1) -> str:
    """Persist one guard-policy revision and return its ``revision_id``.

    Content-addressed on the SEMANTIC payload: the same policy declared by two people is one row,
    and the first declaration's provenance is the one kept."""
    if not isinstance(policy, JoinValidationPolicyRevisionV1):
        raise JoinPolicyPersistenceDefect(
            f"policy must be a JoinValidationPolicyRevisionV1, got {type(policy).__name__}")
    conn.execute(
        "INSERT INTO join_validation_policy_revision "
        "  (revision_id, content, content_hash, declared_by, declared_at) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (revision_id) DO NOTHING",
        (policy.revision_id, Jsonb(policy.content_payload()), policy.content_hash,
         policy.declared_by, policy.declared_at))
    if load_join_validation_policy(conn, policy.revision_id) is None:
        raise JoinPolicyStoreConflict(
            f"join validation policy {policy.revision_id} did not persist")
    return policy.revision_id


def load_join_validation_policy(conn: DbConn,
                                revision_id: str) -> JoinValidationPolicyRevisionV1 | None:
    """Load and CONTENT-VERIFY one guard-policy revision; ``None`` when absent, corruption raises."""
    row = conn.execute(
        "SELECT content, content_hash, declared_by, declared_at "
        "FROM join_validation_policy_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    try:
        policy = join_validation_policy_from_payload(
            row[0], declared_by=row[2], declared_at=row[3])
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise JoinPolicyStoreConflict(
            f"join validation policy {revision_id} could not be rebuilt from its stored "
            f"payload: {exc}") from exc
    if policy.content_payload() != row[0]:
        raise JoinPolicyStoreConflict(
            f"join validation policy {revision_id} is not stored in its canonical serialization")
    if policy.content_hash != row[1] or policy.revision_id != revision_id:
        raise JoinPolicyStoreConflict(
            f"join validation policy {revision_id} fails content verification")
    return policy


def require_join_validation_policy(conn: DbConn, revision_id: str) -> None:
    """The pin, checked. Raises when the named policy was never declared.

    This is the leg 1134 could only store: the physical-plan table carries the policy id and
    Postgres cannot check it (an FK onto this append-only table would make it refuse a TRUNCATE
    before its own raiser fired), so the check is a VERIFYING LOAD — which proves more than an FK
    could, because a policy that can no longer reproduce its own identity stops the write too."""
    if not isinstance(revision_id, str) or not revision_id.strip():
        raise JoinPolicyPersistenceDefect(
            f"join_validation_policy_revision_id must be a non-empty string, got {revision_id!r}")
    if load_join_validation_policy(conn, revision_id) is None:
        raise JoinPolicyPersistenceDefect(
            f"the join validation policy {revision_id!r} was never declared — a physical plan pins "
            "the exact guard policy a preview compiles in, never one it would have to invent")

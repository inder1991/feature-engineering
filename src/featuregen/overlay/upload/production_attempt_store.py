"""§9.1 — both production state machines, exactly as the 2026-08-23 spec wrote them.

Engineering BEHIND unavailable actions: §0.1.0 keeps both production acts refusing at the
decision service, so nothing can create these rows until the owner opens the policy — and every
transition here is compare-and-set so that day inherits a machine, not a promise.

The load-bearing shapes, each learned once already this programme:

* one LIVE attempt per (environment, group) — partial unique, terminals release the slot;
* claims are leases with a rising fence; UNKNOWN_OUTCOME is FIRST-CLASS for materialization and
  DESIGNED OUT for publication (the pointer is a database row; swap and terminal commit
  together — see the spec §1.0, and the reconciler asserts the invariant that keeps it legal);
* the publication swap is CAS on the pointer's fence: a zombie loses INSIDE the statement.
"""
from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Mapping

from featuregen.canonical import jcs_sha256

__all__ = [
    "InvalidProductionMove",
    "MaterializationStatusV1",
    "ProductionMovedUnderneath",
    "PublicationStatusV1",
    "advance_materialization",
    "advance_publication",
    "current_active_revision",
    "publish_swap",
    "read_materialization",
    "read_publication",
    "record_materialization_attempt",
    "record_output_revision",
    "record_publication_attempt",
]


class MaterializationStatusV1(StrEnum):
    REQUESTED = "REQUESTED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    #: The state crash recovery FINDS — never guessed out of, only reconciled out of.
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    STAGED = "STAGED"
    SUCCEEDED = "SUCCEEDED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (MaterializationStatusV1.SUCCEEDED, MaterializationStatusV1.REFUSED,
                        MaterializationStatusV1.FAILED, MaterializationStatusV1.CANCELLED)


_MAT_FORWARD: dict[MaterializationStatusV1, frozenset[MaterializationStatusV1]] = {
    MaterializationStatusV1.REQUESTED: frozenset({
        MaterializationStatusV1.CLAIMED, MaterializationStatusV1.CANCELLED}),
    MaterializationStatusV1.CLAIMED: frozenset({
        MaterializationStatusV1.RUNNING, MaterializationStatusV1.REFUSED,
        MaterializationStatusV1.FAILED}),
    MaterializationStatusV1.RUNNING: frozenset({
        MaterializationStatusV1.UNKNOWN_OUTCOME, MaterializationStatusV1.STAGED,
        MaterializationStatusV1.FAILED}),
    MaterializationStatusV1.UNKNOWN_OUTCOME: frozenset({
        MaterializationStatusV1.RUNNING, MaterializationStatusV1.STAGED,
        MaterializationStatusV1.FAILED}),
    MaterializationStatusV1.STAGED: frozenset({
        MaterializationStatusV1.SUCCEEDED, MaterializationStatusV1.FAILED}),
    MaterializationStatusV1.SUCCEEDED: frozenset(),
    MaterializationStatusV1.REFUSED: frozenset(),
    MaterializationStatusV1.FAILED: frozenset(),
    MaterializationStatusV1.CANCELLED: frozenset(),
}


class PublicationStatusV1(StrEnum):
    REQUESTED = "REQUESTED"
    CLAIMED = "CLAIMED"
    PUBLISHED = "PUBLISHED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (PublicationStatusV1.PUBLISHED, PublicationStatusV1.REFUSED,
                        PublicationStatusV1.FAILED, PublicationStatusV1.CANCELLED)


_PUB_FORWARD: dict[PublicationStatusV1, frozenset[PublicationStatusV1]] = {
    PublicationStatusV1.REQUESTED: frozenset({
        PublicationStatusV1.CLAIMED, PublicationStatusV1.CANCELLED}),
    PublicationStatusV1.CLAIMED: frozenset({
        PublicationStatusV1.PUBLISHED, PublicationStatusV1.REFUSED,
        PublicationStatusV1.FAILED}),
    PublicationStatusV1.PUBLISHED: frozenset(),
    PublicationStatusV1.REFUSED: frozenset(),
    PublicationStatusV1.FAILED: frozenset(),
    PublicationStatusV1.CANCELLED: frozenset(),
}


class InvalidProductionMove(ValueError):
    """A move neither machine defines — a programmer error, never a race."""


class ProductionMovedUnderneath(RuntimeError):
    """The row is not in the state the caller read. Re-read; never re-apply."""


def record_materialization_attempt(
    conn, *, attempt_id: str, sealed_artifact_id: str, environment_id: str,
    logical_group_name: str, action_decision_revision_id: str, requested_by: str,
    requested_at: str, target_ref: str | None = None,
) -> tuple[str, bool]:
    """Record one attempt, or return the LIVE one for this (environment, group)."""
    inserted = conn.execute(
        "INSERT INTO production_materialization_attempt (attempt_id, sealed_artifact_id, "
        "environment_id, logical_group_name, target_ref, action_decision_revision_id, "
        "requested_by, requested_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (environment_id, logical_group_name) "
        "WHERE status IN ('REQUESTED', 'CLAIMED', 'RUNNING', 'UNKNOWN_OUTCOME', 'STAGED') "
        "DO NOTHING RETURNING attempt_id",
        (attempt_id, sealed_artifact_id, environment_id, logical_group_name, target_ref,
         action_decision_revision_id, requested_by, requested_at)).fetchone()
    if inserted is not None:
        return attempt_id, True
    live = conn.execute(
        "SELECT attempt_id FROM production_materialization_attempt "
        "WHERE environment_id = %s AND logical_group_name = %s "
        "AND status IN ('REQUESTED', 'CLAIMED', 'RUNNING', 'UNKNOWN_OUTCOME', 'STAGED')",
        (environment_id, logical_group_name)).fetchone()
    return live[0], False


def advance_materialization(
    conn, attempt_id: str, from_status: MaterializationStatusV1,
    to_status: MaterializationStatusV1, *, terminal_detail: Mapping[str, Any] | None = None,
    external_operation_id: str | None = None, staging_path: str | None = None,
    quarantine_path: str | None = None,
) -> None:
    """Compare-and-set, with the stage's evidence landing IN the same statement — the external
    operation id is stored on the move INTO the submit, never after it (the crash window's whole
    point is that "after" may not come)."""
    if to_status not in _MAT_FORWARD[from_status]:
        raise InvalidProductionMove(f"{from_status} -> {to_status} is not a move the "
                                    f"materialization machine defines")
    moved = conn.execute(
        "UPDATE production_materialization_attempt SET status = %s, "
        "terminal_detail_json = COALESCE(%s::jsonb, terminal_detail_json), "
        "external_operation_id = COALESCE(%s, external_operation_id), "
        "staging_path = COALESCE(%s, staging_path), "
        "quarantine_path = COALESCE(%s, quarantine_path), "
        "lease_owner = CASE WHEN %s THEN NULL ELSE lease_owner END, "
        "lease_expires_at = CASE WHEN %s THEN NULL ELSE lease_expires_at END "
        "WHERE attempt_id = %s AND status = %s RETURNING attempt_id",
        (to_status.value,
         None if terminal_detail is None else json.dumps(dict(terminal_detail)),
         external_operation_id, staging_path, quarantine_path,
         to_status.is_terminal, to_status.is_terminal,
         attempt_id, from_status.value)).fetchone()
    if moved is None:
        current = conn.execute(
            "SELECT status FROM production_materialization_attempt WHERE attempt_id = %s",
            (attempt_id,)).fetchone()
        raise ProductionMovedUnderneath(
            f"materialization {attempt_id} is "
            f"{'absent' if current is None else current[0]!r}, not {from_status.value!r}")


def record_output_revision(
    conn, *, attempt_id: str, manifest: Mapping[str, Any], row_count: int,
) -> str:
    """The output IDENTITY: content-addressed over the staged manifest, one per attempt."""
    output_revision_id = jcs_sha256(dict(manifest))
    conn.execute(
        "INSERT INTO materialized_output_revision (output_revision_id, attempt_id, "
        "output_manifest_hash, row_count) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (attempt_id) DO NOTHING",
        (output_revision_id, attempt_id, output_revision_id, row_count))
    return output_revision_id


def read_materialization(conn, attempt_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT sealed_artifact_id, environment_id, logical_group_name, status, "
        "external_operation_id, staging_path, quarantine_path, terminal_detail_json, "
        "action_decision_revision_id, lease_fence, attempts "
        "FROM production_materialization_attempt WHERE attempt_id = %s",
        (attempt_id,)).fetchone()
    if row is None:
        return None
    return {"attempt_id": attempt_id, "sealed_artifact_id": row[0], "environment_id": row[1],
            "logical_group_name": row[2], "status": MaterializationStatusV1(row[3]),
            "external_operation_id": row[4], "staging_path": row[5], "quarantine_path": row[6],
            "terminal_detail": row[7], "action_decision_revision_id": row[8],
            "lease_fence": row[9], "attempts": row[10]}


# ── publication ─────────────────────────────────────────────────────────────────────────────────
def record_publication_attempt(
    conn, *, attempt_id: str, materialization_attempt_id: str, output_revision_id: str,
    environment_id: str, logical_group_name: str, action_decision_revision_id: str,
    requested_by: str, requested_at: str,
) -> tuple[str, bool]:
    """One attempt, or the LIVE one. The composite FK refuses an output the named
    materialization did not produce — the forgery rule as schema."""
    inserted = conn.execute(
        "INSERT INTO production_publication_attempt (attempt_id, materialization_attempt_id, "
        "output_revision_id, environment_id, logical_group_name, action_decision_revision_id, "
        "requested_by, requested_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (environment_id, logical_group_name) "
        "WHERE status IN ('REQUESTED', 'CLAIMED') DO NOTHING RETURNING attempt_id",
        (attempt_id, materialization_attempt_id, output_revision_id, environment_id,
         logical_group_name, action_decision_revision_id, requested_by,
         requested_at)).fetchone()
    if inserted is not None:
        return attempt_id, True
    live = conn.execute(
        "SELECT attempt_id FROM production_publication_attempt "
        "WHERE environment_id = %s AND logical_group_name = %s "
        "AND status IN ('REQUESTED', 'CLAIMED')",
        (environment_id, logical_group_name)).fetchone()
    return live[0], False


def advance_publication(
    conn, attempt_id: str, from_status: PublicationStatusV1, to_status: PublicationStatusV1,
    *, terminal_detail: Mapping[str, Any] | None = None,
) -> None:
    if to_status not in _PUB_FORWARD[from_status]:
        raise InvalidProductionMove(f"{from_status} -> {to_status} is not a move the "
                                    f"publication machine defines")
    moved = conn.execute(
        "UPDATE production_publication_attempt SET status = %s, "
        "terminal_detail_json = COALESCE(%s::jsonb, terminal_detail_json), "
        "lease_owner = CASE WHEN %s THEN NULL ELSE lease_owner END, "
        "lease_expires_at = CASE WHEN %s THEN NULL ELSE lease_expires_at END "
        "WHERE attempt_id = %s AND status = %s RETURNING attempt_id",
        (to_status.value,
         None if terminal_detail is None else json.dumps(dict(terminal_detail)),
         to_status.is_terminal, to_status.is_terminal,
         attempt_id, from_status.value)).fetchone()
    if moved is None:
        current = conn.execute(
            "SELECT status FROM production_publication_attempt WHERE attempt_id = %s",
            (attempt_id,)).fetchone()
        raise ProductionMovedUnderneath(
            f"publication {attempt_id} is "
            f"{'absent' if current is None else current[0]!r}, not {from_status.value!r}")


def publish_swap(
    conn, *, attempt_id: str, environment_id: str, logical_group_name: str,
    output_revision_id: str, fence: int,
) -> bool:
    """THE swap: pointer CAS and nothing else, in the caller's transaction — the caller advances
    the attempt to PUBLISHED in the SAME transaction, which is what removes UNKNOWN_OUTCOME from
    this machine (spec §1.0). Returns whether the swap won.

    A zombie loses INSIDE the statement: the update applies only when the standing fence is
    lower. Returns False on a lost race — the caller REFUSES its own attempt rather than
    publishing over a newer act.
    """
    row = conn.execute(
        "INSERT INTO production_active_revision (environment_id, logical_group_name, "
        "output_revision_id, publication_attempt_id, fence) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (environment_id, logical_group_name) DO UPDATE SET "
        "output_revision_id = EXCLUDED.output_revision_id, "
        "publication_attempt_id = EXCLUDED.publication_attempt_id, "
        "fence = EXCLUDED.fence, published_at = now() "
        "WHERE production_active_revision.fence < EXCLUDED.fence "
        "RETURNING publication_attempt_id",
        (environment_id, logical_group_name, output_revision_id, attempt_id,
         fence)).fetchone()
    return row is not None


def current_active_revision(
    conn, *, environment_id: str, logical_group_name: str,
) -> dict[str, Any] | None:
    """What is actually out there right now — §9.1's question, answerable by one read."""
    row = conn.execute(
        "SELECT output_revision_id, publication_attempt_id, fence, published_at "
        "FROM production_active_revision WHERE environment_id = %s AND logical_group_name = %s",
        (environment_id, logical_group_name)).fetchone()
    if row is None:
        return None
    return {"output_revision_id": row[0], "publication_attempt_id": row[1], "fence": row[2],
            "published_at": str(row[3])}


def read_publication(conn, attempt_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT materialization_attempt_id, output_revision_id, environment_id, "
        "logical_group_name, status, terminal_detail_json, action_decision_revision_id "
        "FROM production_publication_attempt WHERE attempt_id = %s", (attempt_id,)).fetchone()
    if row is None:
        return None
    return {"attempt_id": attempt_id, "materialization_attempt_id": row[0],
            "output_revision_id": row[1], "environment_id": row[2],
            "logical_group_name": row[3], "status": PublicationStatusV1(row[4]),
            "terminal_detail": row[5], "action_decision_revision_id": row[6]}

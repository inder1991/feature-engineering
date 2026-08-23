"""§15.1 applied to the production tables — the reconcilers ship WITH the machines, not after.

Both tables are empty today (the acts are unavailable), so these sweeps do nothing — which is
exactly the point of shipping them now: the day the policy opens, crash recovery is already
running, instead of being the lesson a wedged attempt teaches (the fifth time).
"""
from __future__ import annotations

from typing import Any, Callable

from featuregen.overlay.upload.production_attempt_store import (
    MaterializationStatusV1,
    advance_materialization,
)
from featuregen.runtime.observability import counters, log

__all__ = ["reconcile_unknown_materializations", "sweep_publication_pointer_invariant"]


def reconcile_unknown_materializations(
    conn, *, cluster_status: Callable[[str], str] | None = None,
) -> dict[str, int]:
    """The spec's §2.2 algorithm over every UNKNOWN_OUTCOME attempt.

    ``cluster_status(external_operation_id)`` answers ``running | succeeded | failed | absent``;
    it is the step-0b substrate's adapter. With NONE configured (today's posture), every unknown
    attempt is HELD and gauged — the released-message discipline: held is not judged, and a
    machine without a cluster to ask must never guess an outcome for one.
    """
    tallies = {"held": 0, "resumed_running": 0, "staged": 0, "failed": 0}
    rows = conn.execute(
        "SELECT attempt_id, external_operation_id, staging_path "
        "FROM production_materialization_attempt WHERE status = 'UNKNOWN_OUTCOME' "
        "FOR UPDATE SKIP LOCKED").fetchall()
    for attempt_id, external_operation_id, staging_path in rows:
        if cluster_status is None or external_operation_id is None:
            tallies["held"] += 1
            continue
        answer = cluster_status(external_operation_id)
        if answer == "running":
            advance_materialization(conn, attempt_id, MaterializationStatusV1.UNKNOWN_OUTCOME,
                                    MaterializationStatusV1.RUNNING)
            tallies["resumed_running"] += 1
        elif answer == "succeeded":
            # The manifest must still be verified under the FENCED path before promotion — the
            # move to STAGED says "the cluster finished", not "the output is proven".
            advance_materialization(conn, attempt_id, MaterializationStatusV1.UNKNOWN_OUTCOME,
                                    MaterializationStatusV1.STAGED)
            tallies["staged"] += 1
        elif answer in ("failed", "absent"):
            advance_materialization(
                conn, attempt_id, MaterializationStatusV1.UNKNOWN_OUTCOME,
                MaterializationStatusV1.FAILED,
                terminal_detail={"failure": f"cluster reports {answer}",
                                 "external_operation_id": external_operation_id},
                quarantine_path=staging_path)
            tallies["failed"] += 1
        else:
            tallies["held"] += 1     # an unrecognized answer is an unreachable cluster, held
    counters.gauge("featuregen.production_materialization.unknown_held", tallies["held"])
    if any(v for k, v in tallies.items() if k != "held"):
        log("featuregen.production_materialization.reconciled", **tallies)
    return tallies


def sweep_publication_pointer_invariant(conn) -> dict[str, Any]:
    """The invariant that makes publication's missing UNKNOWN state LEGAL (spec §1.0): every
    PUBLISHED attempt's group pointer names an output that attempt's fence could have written,
    and every pointer names a PUBLISHED attempt. A violation is a LOUD gauge — reported, never
    repaired by guesswork."""
    orphaned_pointers = conn.execute(
        "SELECT COUNT(*) FROM production_active_revision a "
        "WHERE NOT EXISTS (SELECT 1 FROM production_publication_attempt p "
        "  WHERE p.attempt_id = a.publication_attempt_id AND p.status = 'PUBLISHED')"
    ).fetchone()[0]
    counters.gauge("featuregen.production_publication.pointer_invariant_violations",
                   orphaned_pointers)
    if orphaned_pointers:
        log("featuregen.production_publication.pointer_invariant_violated",
            orphaned_pointers=orphaned_pointers)
    return {"orphaned_pointers": orphaned_pointers}

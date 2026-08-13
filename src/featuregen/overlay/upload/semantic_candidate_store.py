"""SE-10 (slice 1) — the semantic-candidate observation store: shadow truth as rows.

Every semantic run (shadow today, `semantic_v1` serving later) persists ONE row per candidate:
which definition, under which frozen context, bound in which state, with the binder's verdicts
and the full per-candidate eligibility audit serialized whole, and the policy identities the
decision was made under. Append-only (migration 1062's guard triggers) — an observation is what
a run SAW; a changed catalog produces a NEW row under a NEW context hash, never an edit.

This is what turns SE-14's shadow metrics into queries instead of grepped logs, and it is the
foundation SE-10's considered-revision extension builds on when `semantic_v1` serves.
"""
from __future__ import annotations

from dataclasses import asdict

from featuregen.idgen import mint_id


def persist_semantic_candidates(conn, *, generation_run_id: str, context,
                                candidates) -> dict[str, str]:
    """Append one observation per candidate; returns {source_definition_id: observation_id}
    so callers can LINK derived records (the A1b option decision) to the exact row — never
    "newest for the definition" (LIFE-03's wrong-row defect)."""
    from psycopg.types.json import Jsonb

    from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
    from featuregen.overlay.upload.semantic_eligibility import (
        SEMANTIC_AUTHORITY_POLICY_VERSION,
        authority_matrix_hash,
    )

    context_hash = context.context_hash()
    policy_hashes = {
        "authority_matrix_hash": authority_matrix_hash(),
        "semantic_authority_policy_version": SEMANTIC_AUTHORITY_POLICY_VERSION,
        "operand_class_map_version": OPERAND_CLASS_MAP_VERSION,
    }
    observation_ids: dict[str, str] = {}
    for candidate in candidates:
        eligibility = [
            {"role": role, "object_ref": ref, **asdict(verdict)}
            for (role, ref), verdict in getattr(candidate, "eligibility", {}).items()]
        conn.execute(
            "INSERT INTO semantic_candidate_observation "
            "(observation_id, generation_run_id, catalog_source, context_hash, source_origin, "
            " source_definition_id, planning_request_hash, relationship, binding_state, "
            " readiness, review_current, temporal_blocked, verdicts, eligibility, policy_hashes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (observation_ids.setdefault(candidate.recipe_id, mint_id("sco")),
             generation_run_id, context.catalog_source, context_hash,
             candidate.planning_request.origin, candidate.recipe_id,
             candidate.planning_request_hash, candidate.relationship,
             candidate.binding_state, candidate.readiness, candidate.review_current,
             bool(candidate.temporal_blocker),
             Jsonb([asdict(v) for v in candidate.verdicts]),
             Jsonb(eligibility), Jsonb(policy_hashes)))
    return observation_ids


__all__ = ["persist_semantic_candidates"]

def semantic_shadow_metrics(conn) -> dict:
    """SE-14's shadow metrics, computed from the observation rows — the MEASURED inputs the
    cutover gate consumes. Every number here is a fact about recorded runs, never a projection:
    an empty store yields zeros and ``observation_rows_present: False``, which the gate reads
    as blocking (absence of evidence blocks a cutover; it never passes one)."""
    from collections import Counter

    rows = conn.execute(
        "SELECT binding_state, source_origin, verdicts, temporal_blocked "
        "FROM semantic_candidate_observation").fetchall()
    by_state: Counter = Counter()
    by_origin: Counter = Counter()
    reason_codes: Counter = Counter()
    identifier_preventions = 0
    snapshot_preventions = 0
    temporal_blocked = 0
    for binding_state, origin, verdicts, blocked in rows:
        by_state[binding_state] += 1
        by_origin[origin] += 1
        temporal_blocked += bool(blocked)
        for verdict in verdicts or ():
            for code in verdict.get("reason_codes", ()):
                reason_codes[code] += 1
                if code == "IDENTIFIER_NOT_A_MEASURE":
                    identifier_preventions += 1
                elif code == "SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW":
                    snapshot_preventions += 1
    runs = conn.execute(
        "SELECT COUNT(DISTINCT generation_run_id), COUNT(DISTINCT context_hash) "
        "FROM semantic_candidate_observation").fetchone()
    return {
        "observation_rows_present": bool(rows),
        "observations": len(rows),
        "generation_runs": runs[0],
        "distinct_contexts": runs[1],
        "candidates_by_binding_state": dict(sorted(by_state.items())),
        "candidates_by_origin": dict(sorted(by_origin.items())),
        "reason_code_counts": dict(sorted(reason_codes.items())),
        "identifier_as_measure_preventions": identifier_preventions,
        "snapshot_as_event_preventions": snapshot_preventions,
        "temporal_blocked_candidates": temporal_blocked,
    }

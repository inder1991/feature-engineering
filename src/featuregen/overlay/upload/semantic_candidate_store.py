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
                                candidates) -> int:
    """Append one observation per candidate; returns the row count written."""
    from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
    from featuregen.overlay.upload.semantic_eligibility import (
        SEMANTIC_AUTHORITY_POLICY_VERSION,
        authority_matrix_hash,
    )
    from psycopg.types.json import Jsonb

    context_hash = context.context_hash()
    policy_hashes = {
        "authority_matrix_hash": authority_matrix_hash(),
        "semantic_authority_policy_version": SEMANTIC_AUTHORITY_POLICY_VERSION,
        "operand_class_map_version": OPERAND_CLASS_MAP_VERSION,
    }
    written = 0
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
            (mint_id("sco"), generation_run_id, context.catalog_source, context_hash,
             candidate.planning_request.origin, candidate.recipe_id,
             candidate.planning_request_hash, candidate.relationship,
             candidate.binding_state, candidate.readiness, candidate.review_current,
             bool(candidate.temporal_blocker),
             Jsonb([asdict(v) for v in candidate.verdicts]),
             Jsonb(eligibility), Jsonb(policy_hashes)))
        written += 1
    return written


__all__ = ["persist_semantic_candidates"]

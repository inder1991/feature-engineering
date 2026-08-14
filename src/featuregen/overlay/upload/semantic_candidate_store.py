"""SE-10 (slice 1) — the semantic-candidate observation store: what each run actually saw.

Every semantic run persists ONE row per candidate: which definition, under which frozen context,
bound in which state, with the binder's verdicts and the full per-candidate eligibility audit
serialized whole, and the policy identities the decision was made under. Append-only (migration
1062's guard triggers) — an observation is what a run SAW; a changed catalog produces a NEW row
under a NEW context hash, never an edit.

E4 (2026-08-14): the SHADOW half is gone — `semantic_shadow_metrics` existed to compare a
candidate engine against the legacy path and feed the cutover gate, and both of those are
spent. The store itself is NOT shadow machinery and stays: it is the serving path's audit
trail, the row every frozen decision fact links to by exact observation id (LIFE-03).
"""
from __future__ import annotations

from dataclasses import asdict

from featuregen.idgen import mint_id


def persist_semantic_candidates(conn, *, generation_run_id: str, context,
                                candidates) -> dict[str, str]:
    """Append one observation per candidate; returns {variant_key: observation_id} (the
    variant key falls back to the recipe id when a candidate has no parameters) so callers
    LINK derived records to the exact row — never "newest for the definition" (LIFE-03)."""
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
            (observation_ids.setdefault(
                getattr(candidate, "variant_key", "") or candidate.recipe_id,
                mint_id("sco")),
             generation_run_id, context.catalog_source, context_hash,
             candidate.planning_request.origin, candidate.recipe_id,
             candidate.planning_request_hash, candidate.relationship,
             candidate.binding_state, candidate.readiness, candidate.review_current,
             bool(candidate.temporal_blocker),
             Jsonb([asdict(v) for v in candidate.verdicts]),
             Jsonb(eligibility), Jsonb(policy_hashes)))
    return observation_ids


__all__ = ["persist_semantic_candidates"]

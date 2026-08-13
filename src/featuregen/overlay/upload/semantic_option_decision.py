"""Remediation A1b — the minimal immutable option-decision store (migration 1063).

One row per SERVED semantic option: the frozen facts the user's card was built from. The
activation policy's frozen layer reads from HERE — never from ``FeatureIdea`` (which carries
no review validity, readiness, or computation identity) and never from live catalog reads.
Task D1 enriches this record; it does not replace it.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from featuregen.overlay.upload.activation_policy import FrozenOptionFactsV1
from featuregen.idgen import mint_id

#: origin (planning contract) -> server-assigned generation_source. The DECISION row is honest
#: about origin even while the wire projection still labels intents as recipes (GEN-05 — fixed
#: by remediation B4); activation must never inherit that lie.
_GENERATION_SOURCE_BY_ORIGIN = {
    "recipe_v2": "recipe",
    "llm_intent": "llm_intent",
    "user_definition": "user_defined",
}


def decision_facts_for_candidate(candidate, idea, observation_id: str | None,
                                 context_hash: str) -> dict:
    """Assemble ONE candidate's frozen facts at serving time (gate1's semantic branch), where
    the candidate, its projection (`idea`), and its observation id are all in hand."""
    from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
        has_reviewed_expectation,
    )
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash

    request = candidate.planning_request
    return {
        "source_definition_id": candidate.recipe_id,
        "generation_source": _GENERATION_SOURCE_BY_ORIGIN.get(request.origin, request.origin),
        "computation_kind": request.computation_kind,
        "planning_request_hash": candidate.planning_request_hash,
        "parameter_values": [[name, repr(value)] for name, value in request.parameter_values],
        "binding_state": candidate.binding_state,
        "confirmation_required_roles": sorted(
            binding.role for binding in idea.input_role_bindings
            if binding.confirmation_required),
        "readiness": candidate.readiness,
        "review_current": candidate.review_current,
        "recipe_revision_hash": candidate.recipe_revision_hash,
        "validation_status": idea.validation_status,
        "outstanding_requirement_codes": sorted({req.code for req in idea.requirements}),
        "has_reviewed_formula_expectation": has_reviewed_expectation(candidate.recipe_id),
        "formula_expectation_revision": "",   # pinned when the formula seam mints one (Phase E)
        "plan_envelope_present": False,       # honest until B7 wires the planner
        "dataset_story": ({"population_ref": candidate.dataset_story.population_ref,
                           "dataset_tables": list(candidate.dataset_story.dataset_tables),
                           "cross_dataset": candidate.dataset_story.cross_dataset,
                           "codes": list(candidate.dataset_story.codes)}
                          if candidate.dataset_story is not None else {}),
        "policy_revision_pins": {"authority_matrix_hash": authority_matrix_hash()},
        "observation_id": observation_id,
        "context_hash": context_hash,
    }


def persist_option_decisions(conn, *, considered_revision_id: str, generation_run_id: str,
                             metadata_snapshot_id: str | None,
                             facts_by_option_id: dict[str, dict]) -> int:
    """Append one decision row per served option — same transaction as the revision."""
    written = 0
    for option_id, facts in sorted(facts_by_option_id.items()):
        conn.execute(
            "INSERT INTO semantic_option_decision "
            "(decision_id, considered_revision_id, option_id, generation_run_id, "
            " source_definition_id, generation_source, computation_kind, "
            " planning_request_hash, parameter_values, binding_state, "
            " confirmation_required_roles, readiness, review_current, recipe_revision_hash, "
            " validation_status, outstanding_requirement_codes, "
            " has_reviewed_formula_expectation, formula_expectation_revision, "
            " plan_envelope_present, dataset_story, policy_revision_pins, "
            " metadata_snapshot_id, observation_id, context_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (considered_revision_id, option_id) DO NOTHING",
            (mint_id("sod"), considered_revision_id, option_id, generation_run_id,
             facts["source_definition_id"], facts["generation_source"],
             facts["computation_kind"], facts["planning_request_hash"],
             Jsonb(facts["parameter_values"]), facts["binding_state"],
             Jsonb(facts["confirmation_required_roles"]), facts["readiness"],
             facts["review_current"], facts["recipe_revision_hash"],
             facts["validation_status"], Jsonb(facts["outstanding_requirement_codes"]),
             facts["has_reviewed_formula_expectation"],
             facts["formula_expectation_revision"], facts["plan_envelope_present"],
             Jsonb(facts["dataset_story"]), Jsonb(facts["policy_revision_pins"]),
             metadata_snapshot_id, facts.get("observation_id"), facts["context_hash"]))
        written += 1
    return written


def load_frozen_option_facts(conn, *, considered_revision_id: str,
                             option_id: str) -> FrozenOptionFactsV1 | None:
    """The activation policy's frozen layer, from the exact (revision, option) key."""
    row = conn.execute(
        "SELECT binding_state, generation_source, computation_kind, readiness, "
        "       source_definition_id, "
        "       review_current, recipe_revision_hash, confirmation_required_roles, "
        "       has_reviewed_formula_expectation, plan_envelope_present, validation_status, "
        "       outstanding_requirement_codes, policy_revision_pins, "
        "       formula_expectation_revision, metadata_snapshot_id "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if row is None:
        return None
    (binding_state, generation_source, computation_kind, readiness, source_definition_id,
     review_current, recipe_revision_hash, confirmation_roles, has_reviewed, plan_present,
     validation_status, outstanding, pins, formula_revision, snapshot_id) = row
    return FrozenOptionFactsV1(
        binding_state=binding_state,
        generation_source=generation_source,
        computation_kind=computation_kind,
        readiness=readiness,
        review_current=review_current,
        source_definition_id=source_definition_id,
        recipe_revision_hash=recipe_revision_hash,
        confirmation_required_roles=tuple(confirmation_roles or ()),
        has_reviewed_formula_expectation=has_reviewed,
        plan_envelope_present=plan_present,
        validation_status=validation_status,
        outstanding_requirement_codes=tuple(outstanding or ()),
        policy_revision_pins=tuple(sorted(f"{k}:{v}" for k, v in (pins or {}).items())),
        formula_expectation_revision=formula_revision,
        snapshot_id=snapshot_id or "")


def assemble_current_activation_state(conn, *, frozen: FrozenOptionFactsV1,
                                      snapshot_id: str | None):
    """The activation policy's CURRENT layer — the small re-read at the durable write.

    Everything here fails toward blocking (the same posture as the dataclass defaults):
    a review that cannot be re-verified is not current; an absent snapshot is unverifiable;
    a policy hash that moved since generation is drift. NOTHING here re-binds or substitutes —
    divergence surfaces as a typed regenerate blocker in the fold."""
    from featuregen.overlay.upload.activation_policy import CurrentActivationStateV1
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash

    review_now = False
    if frozen.generation_source == "recipe" and frozen.source_definition_id:
        try:
            from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
            from featuregen.overlay.upload.recipe_review_validity import (
                review_validity,
                reviews_by_role_for_revision,
            )

            definition = v2_recipe_by_id(frozen.source_definition_id)
            by_role = reviews_by_role_for_revision(
                conn, recipe_id=definition.recipe_id,
                recipe_revision_hash=frozen.recipe_revision_hash)
            review_now = review_validity(definition, by_role).current
        except Exception:                     # unknown recipe / store error → NOT current
            review_now = False

    pins_current = (f"authority_matrix_hash:{authority_matrix_hash()}"
                    in frozen.policy_revision_pins)

    freshness = "unverifiable"
    if snapshot_id:
        try:
            from featuregen.overlay.upload.feature_metadata_snapshot import (
                compare_snapshot_to_current,
            )

            freshness = compare_snapshot_to_current(conn, snapshot_id).status
        except Exception:
            freshness = "unverifiable"

    return CurrentActivationStateV1(
        review_current=review_now,
        policy_revisions_current=pins_current,
        snapshot_freshness=freshness,
        # Effective readiness is the FROZEN readiness until a re-fold exists (C-phase): honest,
        # and materialization stays blocked regardless (readiness != MATERIALIZATION_READY for
        # every recipe today, schema unsupported, execution authority unevaluated).
        effective_readiness=frozen.readiness,
        formula_expectation_revision=frozen.formula_expectation_revision,
        formula_schema_supported=False,
        requirements_closed=False,
        execution_authority_evaluated=False,
        execution_floor_met=False)


__all__ = ["assemble_current_activation_state", "decision_facts_for_candidate",
           "load_frozen_option_facts", "persist_option_decisions"]

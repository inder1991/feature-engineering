"""Remediation A1b — the minimal immutable option-decision store (migration 1063).

One row per SERVED semantic option: the frozen facts the user's card was built from. The
activation policy's frozen layer reads from HERE — never from ``FeatureIdea`` (which carries
no review validity, readiness, or computation identity) and never from live catalog reads.
Task D1 enriches this record; it does not replace it.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from featuregen.idgen import mint_id
from featuregen.overlay.upload.activation_policy import FrozenOptionFactsV1

#: origin (planning contract) -> server-assigned generation_source. The DECISION row is honest
#: about origin even while the wire projection still labels intents as recipes (GEN-05 — fixed
#: by remediation B4); activation must never inherit that lie.
_GENERATION_SOURCE_BY_ORIGIN = {
    "recipe_v2": "recipe",
    "llm_intent": "llm_intent",
    "user_definition": "user_defined",
}


def decision_facts_for_candidate(candidate, idea, observation_id: str | None,
                                 context_hash: str, *, uoa_entity: str | None = None,
                                 spine_ref: str | None = None) -> dict:
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
        # The outstanding codes are the GAUNTLET's raw vocabulary, not the card's legacy
        # translation: the projection maps data checks onto the closed legacy registry and
        # deliberately drops the policy/setup states (STATUS_POLICY_UNRESOLVED,
        # PERSONAL_DATA_POLICY_REQUIRED, ...) from the card's requirement list — but the
        # FROZEN facts must carry them, or activation's rules (C4) can never fire. Found by
        # the E1 gold corpus: the card-only read silently dropped every non-legacy code.
        "outstanding_requirement_codes": sorted(
            {req.code for req in idea.requirements}
            | {req.code for req in _candidate_validation(candidate).requirements}),
        "has_reviewed_formula_expectation": has_reviewed_expectation(candidate.recipe_id),
        "formula_expectation_revision": "",   # pinned when the formula seam mints one (Phase E)
        # B7: a REAL gate now — True iff the frozen-bindings plan folded (single-source, bound,
        # declared population, compiled temporal). The plan itself rides the story jsonb until
        # D1 enriches the record with its own column.
        "plan_envelope_present": candidate.binding_plan is not None,
        # B10: the grain decision, frozen with the option — the candidate's own grain and
        # the UOA the human had confirmed when this card was served. Rides the story jsonb
        # (like binding_plan) until D1 gives the record real columns.
        "dataset_story": {
            **({"population_ref": candidate.dataset_story.population_ref,
                "dataset_tables": list(candidate.dataset_story.dataset_tables),
                "cross_dataset": candidate.dataset_story.cross_dataset,
                "codes": list(candidate.dataset_story.codes),
                "binding_plan": candidate.binding_plan,
                "plan_refusals": list(candidate.plan_refusals)}
               if candidate.dataset_story is not None else {}),
            "output_grain": request.output_grain,
            "confirmed_uoa_entity": uoa_entity,
            "confirmed_spine_ref": spine_ref,
            # C2: the bound operands' MEASURED authorities at serving — what the execution
            # floor re-checks against current resolutions at the durable write.
            "operand_authorities": {binding.ref[1]: binding.authority
                                    for binding in idea.input_role_bindings}},
        "policy_revision_pins": {"authority_matrix_hash": authority_matrix_hash()},
        "observation_id": observation_id,
        "context_hash": context_hash,
        # D1 — the FULL evidence record: what the card was built from, verbatim.
        "evidence": _evidence_record(candidate, idea),
        "decision_manifest": _decision_manifest(candidate, context_hash),
    }


def _candidate_validation(candidate):
    from featuregen.overlay.upload.typed_gauntlet import validate_candidate

    return validate_candidate(candidate)


def _evidence_record(candidate, idea) -> dict:
    """D1: the complete audit riding the decision — planning request + every verdict + the
    per-candidate eligibility (the LOSING shortlist and its truncation marker included) +
    the typed validation with its family tri-state. Serialized verbatim, never re-derived."""
    from dataclasses import asdict

    from featuregen.overlay.upload.typed_gauntlet import validate_candidate

    validation = validate_candidate(candidate)
    eligibility = candidate.eligibility or {}
    return {
        "planning_request": asdict(candidate.planning_request),
        "verdicts": [asdict(v) for v in candidate.verdicts],
        "eligibility_audit": [
            {"role": role, "object_ref": ref, **asdict(verdict)}
            for (role, ref), verdict in sorted(eligibility.items())],
        "validation": {
            "status": validation.status,
            "refusals": list(validation.refusals),
            "requirements": [asdict(r) for r in validation.requirements],
            "families": [asdict(f) for f in validation.families],
        },
    }


def _decision_manifest(candidate, context_hash: str) -> dict:
    """PLAN-15's seal: the content hashes of every consumed input. A reader can prove WHAT
    this decision consumed without trusting prose."""
    from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
    from featuregen.overlay.upload.field_resolution import canonical_hash
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash
    from featuregen.overlay.upload.typed_gauntlet import TYPED_GAUNTLET_VERSION

    return {
        "semantic_context_hash": context_hash,
        "authority_matrix_hash": authority_matrix_hash(),
        "typed_gauntlet_version": TYPED_GAUNTLET_VERSION,
        "operand_class_map_version": OPERAND_CLASS_MAP_VERSION,
        "planning_request_hash": candidate.planning_request_hash,
        "binding_plan_hash": (canonical_hash(candidate.binding_plan)
                              if candidate.binding_plan else ""),
        "recipe_revision_hash": candidate.recipe_revision_hash,
    }


def load_option_decision_record(conn, *, considered_revision_id: str,
                                option_id: str) -> dict | None:
    """D1 read side: the FULL stored record by its exact primary key — LIFE-03's wrong-row
    risk is structurally gone (never "newest row for the definition"). Verification is the
    caller's: the manifest's planning_request_hash must match the option identity it serves."""
    row = conn.execute(
        "SELECT decision_id, source_definition_id, generation_source, planning_request_hash, "
        "       binding_state, readiness, review_current, validation_status, dataset_story, "
        "       evidence, decision_manifest, observation_id, context_hash, recorded_at "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if row is None:
        return None
    return {
        "decision_id": row[0], "source_definition_id": row[1], "generation_source": row[2],
        "planning_request_hash": row[3], "binding_state": row[4], "readiness": row[5],
        "review_current": row[6], "validation_status": row[7], "dataset_story": row[8],
        "evidence": row[9], "decision_manifest": row[10], "observation_id": row[11],
        "context_hash": row[12], "recorded_at": str(row[13]),
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
            " metadata_snapshot_id, observation_id, context_hash, evidence, decision_manifest) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s) "
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
             metadata_snapshot_id, facts.get("observation_id"), facts["context_hash"],
             Jsonb(facts.get("evidence") or {}),
             Jsonb(facts.get("decision_manifest") or {})))
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
        "       formula_expectation_revision, metadata_snapshot_id, dataset_story "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if row is None:
        return None
    (binding_state, generation_source, computation_kind, readiness, source_definition_id,
     review_current, recipe_revision_hash, confirmation_roles, has_reviewed, plan_present,
     validation_status, outstanding, pins, formula_revision, snapshot_id, story) = row
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
        snapshot_id=snapshot_id or "",
        plan_refusal_codes=tuple((story or {}).get("plan_refusals") or ()),
        confirmed_uoa_entity=(story or {}).get("confirmed_uoa_entity") or "",
        read_set=tuple(((story or {}).get("binding_plan") or {}).get("read_set") or ()),
        plan_catalog_source=(((story or {}).get("binding_plan") or {})
                             .get("catalog_source") or ""),
        operand_authorities=tuple(sorted(
            ((story or {}).get("operand_authorities") or {}).items())))


def _latest_confirmed_uoa(conn, intent_id: str) -> str | None:
    """The UOA the intent most recently generated under — read from the NEWEST frozen
    decision row (a UOA confirmation always rides a generation, so the newest row carries
    the newest confirmed value). ``None`` when the intent never confirmed one — the UOA is
    OPTIONAL by design (2026-08-13 steer): absence never blocks anything."""
    row = conn.execute(
        "SELECT d.dataset_story->>'confirmed_uoa_entity' "
        "FROM semantic_option_decision d "
        "JOIN contract_considered_revision r "
        "  ON r.considered_revision_id = d.considered_revision_id "
        "WHERE r.intent_id = %s ORDER BY d.recorded_at DESC, d.decision_id DESC LIMIT 1",
        (intent_id,)).fetchone()
    return row[0] if row is not None else None


def assemble_current_activation_state(conn, *, frozen: FrozenOptionFactsV1,
                                      snapshot_id: str | None,
                                      intent_id: str | None = None):
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

    # C2 — the authoring + execution floors, re-read at the durable write: every ref in the
    # frozen plan's read set must STILL clear the matrix column under its CURRENT resolved
    # authority (C1's read-only pins — the same resolver law generation served from). No
    # read set (no plan) → the floors are honestly UNEVALUATED and fail closed.
    authoring_now = False
    execution_evaluated = False
    execution_now = False
    if frozen.read_set and frozen.plan_catalog_source:
        try:
            from featuregen.overlay.upload.field_resolution import current_resolution_pins
            from featuregen.overlay.upload.object_ref import normalize_ref
            from featuregen.overlay.upload.semantic_eligibility import clears

            logical_by_ref = {}
            for ref in frozen.read_set:
                parts = ref.split(".")
                if len(parts) >= 3:
                    logical_by_ref[ref] = normalize_ref(
                        frozen.plan_catalog_source, parts[-3], parts[-2], parts[-1])
            pins = current_resolution_pins(
                conn, logical_refs=list(logical_by_ref.values()), fields=("concept",))
            authorities = []
            for logical in logical_by_ref.values():
                pin = pins.get((logical, "concept"))
                authorities.append(f"{pin.producer}/{pin.strength}"
                                   if pin is not None and pin.producer else "absent")
            authoring_now = bool(authorities) and all(
                clears(a, "authoring") for a in authorities)
            execution_now = bool(authorities) and all(
                clears(a, "execution_at_governed") for a in authorities)
            execution_evaluated = True
        except Exception:                     # unreadable floors → unevaluated, fail closed
            authoring_now = False
            execution_evaluated = False
            execution_now = False

    # B10 item 4 — the UOA re-read. Absence is FREE both ways (the confirmation is
    # optional); a frozen UOA the caller cannot re-verify fails closed like everything else.
    def _norm(value):                         # human vocabulary: case never means drift
        return value.strip().casefold() if value else None

    frozen_uoa = _norm(frozen.confirmed_uoa_entity)
    if intent_id is not None:
        try:
            uoa_now = frozen_uoa == _norm(_latest_confirmed_uoa(conn, intent_id))
        except Exception:
            uoa_now = frozen_uoa is None
    else:
        uoa_now = frozen_uoa is None

    return CurrentActivationStateV1(
        review_current=review_now,
        policy_revisions_current=pins_current,
        uoa_current=uoa_now,
        snapshot_freshness=freshness,
        # Effective readiness is the FROZEN readiness until a re-fold exists (C-phase): honest,
        # and materialization stays blocked regardless (readiness != MATERIALIZATION_READY for
        # every recipe today, schema unsupported, execution authority unevaluated).
        effective_readiness=frozen.readiness,
        formula_expectation_revision=frozen.formula_expectation_revision,
        formula_schema_supported=False,
        requirements_closed=False,
        execution_authority_evaluated=execution_evaluated,
        execution_floor_met=execution_now,
        authoring_floor_met=authoring_now)


__all__ = ["assemble_current_activation_state", "decision_facts_for_candidate",
           "load_frozen_option_facts", "load_option_decision_record",
           "persist_option_decisions"]

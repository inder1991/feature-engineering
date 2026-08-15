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


class OptionDecisionIntegrityError(RuntimeError):
    """A stored decision row disagrees with its own seal — B1's load-time refusal.

    ``semantic_option_decision`` is append-only (1063's two triggers refuse UPDATE, DELETE and
    TRUNCATE) and the plan and its hash are written by ONE statement from ONE facts dict, so the
    two can only disagree if the stored bytes were altered out of band. That is exactly the state
    in which a governed reader must refuse rather than carry on: everything the activation policy
    decides about execution authority is derived from ``read_set``, and a read set nobody can
    authenticate is not a read set. Named and typed for the same reason
    ``ShadowIntegrityError`` is — a caller that wants to classify this must not be reduced to
    matching a message."""

#: origin (planning contract) -> server-assigned generation_source. The DECISION row is honest
#: about origin even while the wire projection still labels intents as recipes (GEN-05 — fixed
#: by remediation B4); activation must never inherit that lie.
_GENERATION_SOURCE_BY_ORIGIN = {
    "recipe_v2": "recipe",
    "llm_intent": "llm_intent",
    "user_definition": "user_defined",
}


def has_reviewed_formula_expectation(recipe_id: str) -> bool:
    """A0 — the reviewed-expectation fact, asked the way the registry is keyed.

    ``RECIPE_FORMULA_V2_EXPECTATIONS`` is keyed by EXPECTATION REF, and a recipe's ref is its
    own name for only 3 of the 317 registry recipes (``retail:balance_slope`` vs
    ``balance_slope`` is the shape the other 295 carry). Passing the recipe id agreed with the
    contract only by that coincidence. A candidate the recipe registry never minted (an LLM
    intent, a user definition) and a recipe that declares no formula (a conceptual pattern, a
    governed model output) both have nothing to have reviewed — honestly ``False``, not an
    error."""
    from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
        has_reviewed_expectation,
    )
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    definition = v2_recipe_by_id(recipe_id)
    if definition is None or definition.formula is None:
        return False
    return has_reviewed_expectation(definition.formula.expectation_ref)


def decision_facts_for_candidate(candidate, idea, observation_id: str | None,
                                 context_hash: str, *, uoa_entity: str | None = None,
                                 spine_ref: str | None = None) -> dict:
    """Assemble ONE candidate's frozen facts at serving time (gate1's semantic branch), where
    the candidate, its projection (`idea`), and its observation id are all in hand."""
    from featuregen.overlay.upload.field_resolution import canonical_hash
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash

    request = candidate.planning_request
    # B1: ONE computation of the plan's identity, handed to BOTH the column and the manifest, so
    # the seal and the sealed thing can never be two answers. `""` for a candidate with no plan —
    # the same empty string the manifest has always carried there.
    binding_plan_hash = (canonical_hash(candidate.binding_plan)
                         if candidate.binding_plan else "")
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
        "has_reviewed_formula_expectation": has_reviewed_formula_expectation(
            candidate.recipe_id),
        "formula_expectation_revision": "",   # pinned when the formula seam mints one (Phase E)
        # B7: a REAL gate now — True iff the frozen-bindings plan folded (single-source, bound,
        # declared population, compiled temporal).
        "plan_envelope_present": candidate.binding_plan is not None,
        # B1: the plan itself, as a FIRST-CLASS fact. `persist_option_decisions` writes it to
        # migration 1066's own column; the `dataset_story` copy below stays for one release so a
        # rollback to the pre-1066 reader still finds it. The two are byte-identical by
        # construction — one object, referenced twice, never re-derived.
        "binding_plan": candidate.binding_plan,
        "binding_plan_hash": binding_plan_hash,
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
        "decision_manifest": _decision_manifest(candidate, context_hash, binding_plan_hash),
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


def _decision_manifest(candidate, context_hash: str, binding_plan_hash: str) -> dict:
    """PLAN-15's seal: the content hashes of every consumed input. A reader can prove WHAT
    this decision consumed without trusting prose.

    ``binding_plan_hash`` is PASSED IN rather than recomputed (B1): it is the seal migration
    1066's column is checked against at load, and a second ``canonical_hash`` call here would be a
    second chance to answer differently about the same object."""
    from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash
    from featuregen.overlay.upload.typed_gauntlet import TYPED_GAUNTLET_VERSION

    return {
        "semantic_context_hash": context_hash,
        "authority_matrix_hash": authority_matrix_hash(),
        "typed_gauntlet_version": TYPED_GAUNTLET_VERSION,
        "operand_class_map_version": OPERAND_CLASS_MAP_VERSION,
        "planning_request_hash": candidate.planning_request_hash,
        "binding_plan_hash": binding_plan_hash,
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
            " metadata_snapshot_id, observation_id, context_hash, evidence, decision_manifest, "
            " binding_plan, binding_plan_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
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
             Jsonb(facts.get("decision_manifest") or {}),
             # B1 — migration 1066. NULL, never `Jsonb(None)` and never `{}`: a candidate that
             # folded no plan has no plan, and an empty object would read as one that authorizes
             # nothing. `.get` because a facts dict assembled by an older build carries neither
             # key, and such a row must still be writable — it simply reads through the story.
             (Jsonb(facts["binding_plan"]) if facts.get("binding_plan") is not None else None),
             facts.get("binding_plan_hash")))
        written += 1
    return written


def frozen_binding_plan(conn, *, considered_revision_id: str,
                        option_id: str) -> dict | None:
    """The FROZEN PLAN ENVELOPE of one served option, or ``None`` when it folded none.

    B1's public read side, and the one place the two storage generations are reconciled:
    migration 1066's ``binding_plan`` column when the row has it, the ``dataset_story``'s
    ``binding_plan`` key when it does not (every row written before 1066). Both are checked
    against the seal the decision manifest already carried.

    Raises:
        OptionDecisionIntegrityError: the stored plan does not hash to the manifest's
            ``binding_plan_hash``.
    """
    row = conn.execute(
        "SELECT binding_plan, dataset_story, decision_manifest "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if row is None:
        return None
    return _verified_plan(row[0], row[1], row[2],
                          f"{considered_revision_id}/{option_id}")


def _verified_plan(column, story, manifest, label: str) -> dict | None:
    """The plan the row carries, proven against the manifest's seal.

    The COLUMN wins where both exist — it is the record's own field, the story copy is the
    compatibility copy, and B1 writes them from one object so they cannot differ. The seal is
    checked whichever side answered: a legacy row's story copy is exactly as tamper-worthy as a
    new row's column, and checking only the new path would make the guard opt-out for every row
    that predates it.

    A manifest with no ``binding_plan_hash`` (a row written before migration 1065 added the
    manifest at all) is not checked and not refused: there is nothing to compare against, and
    inventing a hash for it here would seal a value this deployment computed rather than the one
    generation froze. That is the honest limit of what an unseated row can prove about itself.
    """
    plan = column if column is not None else ((story or {}).get("binding_plan"))
    sealed = (manifest or {}).get("binding_plan_hash")
    if not sealed:
        return plan
    from featuregen.overlay.upload.field_resolution import canonical_hash

    actual = canonical_hash(plan) if plan else ""
    if actual != sealed:
        raise OptionDecisionIntegrityError(
            f"the frozen binding plan of option decision {label} hashes to {actual!r}, but its "
            f"decision manifest seals binding_plan_hash={sealed!r}: the stored plan is not the "
            f"one this decision recorded, and the read set an execution authority is judged "
            f"against cannot be taken from it")
    return plan


def load_frozen_option_facts(conn, *, considered_revision_id: str,
                             option_id: str) -> FrozenOptionFactsV1 | None:
    """The activation policy's frozen layer, from the exact (revision, option) key.

    ``read_set`` and ``plan_catalog_source`` come from the frozen PLAN — migration 1066's column
    where the row has one, the ``dataset_story`` copy where it does not (:func:`_verified_plan`) —
    so a legacy row keeps answering exactly as it did while a new row reads its own field.

    Raises:
        OptionDecisionIntegrityError: the stored plan disagrees with the manifest's seal.
    """
    row = conn.execute(
        "SELECT binding_state, generation_source, computation_kind, readiness, "
        "       source_definition_id, "
        "       review_current, recipe_revision_hash, confirmation_required_roles, "
        "       has_reviewed_formula_expectation, plan_envelope_present, validation_status, "
        "       outstanding_requirement_codes, policy_revision_pins, "
        "       formula_expectation_revision, metadata_snapshot_id, dataset_story, "
        "       binding_plan, decision_manifest "
        "FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if row is None:
        return None
    (binding_state, generation_source, computation_kind, readiness, source_definition_id,
     review_current, recipe_revision_hash, confirmation_roles, has_reviewed, plan_present,
     validation_status, outstanding, pins, formula_revision, snapshot_id, story,
     plan_column, manifest) = row
    plan = _verified_plan(plan_column, story, manifest,
                          f"{considered_revision_id}/{option_id}") or {}
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
        read_set=tuple(plan.get("read_set") or ()),
        plan_catalog_source=plan.get("catalog_source") or "",
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


def _formula_schema_supported(recipe_id: str) -> bool:
    """C2 — can the selected engine run this recipe's REVIEWED formula expectation?

    The reviewed object production can resolve is the CAPTURE BLUEPRINT
    (``recipe_formula_shadow.capture_blueprint_for`` — the same object a review event's hash
    covers, chosen by the recipe's own declared schema version). Its expressions carry exactly
    the engine-relevant demands; the pinned ``gold_v2`` fixture stays the TEST-side proof (it
    lives under ``tests/`` and a deployed backend has no tests tree — the plan's "parse the
    pinned fixture here" was not buildable). Every failure path — unreviewed, no bindable
    blueprint, unknown engine, any error — is ``False``, the dataclass's fail-closed posture.
    """
    try:
        from featuregen.formula.capability_v2 import classify_demands_for_engine
        from featuregen.formula.schema_v2 import WindowBasisV2
        from featuregen.materialize.engine_capability import engine_capability_for
        from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
            RecipeFormulaExpectationBlueprintV2,
        )
        from featuregen.overlay.upload.recipe_formula_shadow import capture_blueprint_for

        if not has_reviewed_formula_expectation(recipe_id):
            return False
        capture = capture_blueprint_for(recipe_id)
        if capture is None:
            return False
        blueprint = capture.blueprint
        if isinstance(blueprint, RecipeFormulaExpectationBlueprintV2):
            demands = {expr.aggregation.value for expr in blueprint.expressions}
            uses_offset = any(expr.window.offset_periods > 0
                              for expr in blueprint.expressions)
            uses_future = any(expr.window.basis is WindowBasisV2.FUTURE_HORIZON
                              for expr in blueprint.expressions)
        else:                                 # a reviewed v1 blueprint: v1 has neither fork
            demands = {expr.aggregation.value for expr in blueprint.expressions}
            uses_offset = False
            uses_future = False
        return classify_demands_for_engine(
            demands, uses_window_offset=uses_offset, uses_future_horizon=uses_future,
            engine=engine_capability_for("kedro-pyspark")) == "ok"
    except Exception:                         # unresolvable → unsupported, fail closed
        return False


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
        # C2 — a real read: the reviewed expectation's demands against the selected engine's
        # advertisement. Only a recipe has a reviewed expectation to ask about; every other
        # source keeps the fail-closed default.
        formula_schema_supported=(
            _formula_schema_supported(frozen.source_definition_id)
            if frozen.generation_source == "recipe" and frozen.source_definition_id
            else False),
        requirements_closed=False,
        execution_authority_evaluated=execution_evaluated,
        execution_floor_met=execution_now,
        authoring_floor_met=authoring_now)


__all__ = ["OptionDecisionIntegrityError", "assemble_current_activation_state",
           "decision_facts_for_candidate", "frozen_binding_plan",
           "has_reviewed_formula_expectation", "load_frozen_option_facts",
           "load_option_decision_record", "persist_option_decisions"]

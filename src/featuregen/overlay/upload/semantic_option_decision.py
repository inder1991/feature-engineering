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


#: S1A-5a §4 — the WIRE vocabulary every route serves this refusal under. It lives beside the error
#: rather than inline at each call site because three routes load frozen facts, and three
#: independently-spelled literals for ONE condition is how a client ends up handling two of them.
#: The condition is a 409, never a 500: a governed record disagreeing with itself is not a fault,
#: it is a conflict a person resolves by regenerating.
OPTION_DECISION_INTEGRITY_CODE = "OPTION_DECISION_INTEGRITY"
OPTION_DECISION_INTEGRITY_NEXT_STEP = "regenerate from the current considered set"

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
            # C3: the serving fold's measured blockers (temporal/binding/policy). The durable
            # write cannot re-measure them — no context, no binder there — so the re-fold
            # carries them verbatim; drift in what they measured surfaces through the pins
            # and snapshot checks instead. Rides the story jsonb like its neighbours.
            "readiness_blockers": list(candidate.readiness_blockers),
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
        # C2a / migration 1135 — the PLANNED-lane marker, and it is FALSE here by construction.
        # This producer serves the semantic engine's single-catalog candidates: they are honestly
        # PRE-PLAN (no logical feature plan is minted for them), and 1135's totality trigger fires
        # only for rows that declare themselves planned. Stated rather than defaulted because the
        # value is INSERT-only — 1063 refuses UPDATE — so a producer that left the key out would
        # be deciding by omission the one fact that can never be corrected afterwards.
        "requires_logical_plan_binding": False,
    }


def decision_facts_for_governed_option(conn, option, *, context_hash: str,
                                       uoa_entity: str | None = None,
                                       spine_ref: str | None = None) -> dict:
    """S1A-5b — ONE governed cross-catalog option's frozen facts, KEY-FOR-KEY with
    :func:`decision_facts_for_candidate`.

    The key set is a CONTRACT, not a convention: ``persist_option_decisions`` maps these keys onto
    migration 1063/1066's columns positionally, so a producer that spelled one key differently
    would write the wrong column or raise. ``test_the_governed_facts_carry_EXACTLY_the_candidate_
    contracts_keys`` asserts the two dicts against each other rather than against a literal.

    What this producer adds over the candidate one, and why:

    * **``source_definition_id`` is the CANONICAL definition id — never the variant id (V11).** The
      persisted column feeds ``v2_recipe_by_id`` and ``_formula_schema_supported`` at every durable
      write; a ``gvar_…`` there resolves to no recipe, so the option would be review-blocked forever
      with nothing naming why. That is exactly why
      :class:`~featuregen.overlay.upload.contract.governed_identity.GovernedVariantIdentityV1` keeps
      canonical id and variant identity as SEPARATE fields, and why the variant identity rides the
      evidence record instead.
    * **origin purity (R14).** Only a ``recipe_v2`` request asks the expectation registry; every
      other origin gets an honest ``False`` having read nothing.
    * **the ``evidence.governed_plan`` block.** The variant identity, the physical plan content
      hash, the parameter binding hash, the origin, the review gaps and the policy revisions the
      governance read returned — the audit a governed cross-catalog decision must carry and that
      no single-source candidate has.
    * **``dataset_story.operand_authorities`` is keyed by the QUALIFIED logical ref**, where the
      candidate producer keys by the bare ``object_ref``. A bare ref is ambiguous across catalogs:
      two catalogs' ``public.accounts.account_id`` are two objects with two authorities, and one
      bare key would silently keep only one of them.

    ``binding_plan`` and ``binding_plan_hash`` are computed ONCE and handed to both the column and
    the manifest, so :func:`_verified_plan`'s seal check holds by construction — the same property
    B1 gave the candidate path, reached the same way.
    """
    from dataclasses import asdict

    from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
    from featuregen.overlay.upload.contract.governed_lens import fold_governed_binding_plan
    from featuregen.overlay.upload.field_resolution import canonical_hash
    from featuregen.overlay.upload.object_ref import normalize_ref
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash
    from featuregen.overlay.upload.typed_gauntlet import TYPED_GAUNTLET_VERSION

    del conn        # nothing here reads the store; the parameter keeps the producers' one shape
    idea, request, identity = option.idea, option.request, option.identity
    binding_plan = fold_governed_binding_plan(idea)
    binding_plan_hash = canonical_hash(binding_plan) if binding_plan else ""
    bound = [b for b in idea.input_role_bindings if b.ref]
    return {
        "source_definition_id": identity.canonical_definition_id,
        "generation_source": ("recipe" if request.origin == "recipe_v2" else "llm_intent"),
        "computation_kind": request.computation_kind,
        "planning_request_hash": identity.planning_request_hash,
        "parameter_values": [[name, repr(value)] for name, value in request.parameter_values],
        # Every governed option this producer sees has EVERY REQUIRED OPERAND BOUND, and that is
        # structural on both paths into it: a SELECTED RESOLVED contract plan has bound them all by
        # construction, and C2b's execution-blocked CARD is refused outright unless it has
        # (`governed_lens._execution_blocked_card`'s fourth gate — a logical identity built over
        # the operands that happened to bind would be a different feature wearing this one's
        # canonical id). There is no unbound governed option to describe, so the state is not
        # re-derived from a weaker signal.
        "binding_state": "bound",
        # The governed path has no per-role confirmation funnel: authority is MEASURED from the
        # resolution pins (`_role_bindings`) and re-measured at the durable write by C2's floors,
        # which is the drift rule this empty list hands over to.
        "confirmation_required_roles": [],
        "readiness": option.readiness.state,
        "review_current": option.governance.review_current,
        "recipe_revision_hash": request.source_content_hash,
        "validation_status": idea.validation_status,
        "outstanding_requirement_codes": sorted(
            {req.code for req in idea.requirements} | set(option.unmapped_requirement_codes)),
        "has_reviewed_formula_expectation": (
            has_reviewed_formula_expectation(identity.canonical_definition_id)
            if request.origin == "recipe_v2" else False),
        "formula_expectation_revision": "",   # pinned when the formula seam mints one (Phase E)
        "plan_envelope_present": binding_plan is not None,
        "binding_plan": binding_plan,
        "binding_plan_hash": binding_plan_hash,
        "dataset_story": {
            "output_grain": request.output_grain,
            "confirmed_uoa_entity": uoa_entity,
            "confirmed_spine_ref": spine_ref,
            "readiness_blockers": list(option.readiness.blockers),
            "operand_authorities": {
                normalize_ref(b.ref[0], *b.ref[1].split(".")[-3:]): b.authority for b in bound},
        },
        "policy_revision_pins": {"authority_matrix_hash": authority_matrix_hash()},
        "observation_id": None,
        "context_hash": context_hash,
        "evidence": {
            "planning_request": asdict(request),
            "verdicts": [],
            "eligibility_audit": [
                {"role": b.role, "catalog_source": b.ref[0], "object_ref": b.ref[1],
                 "authority": b.authority, "evidence_ids": list(b.evidence_ids)}
                for b in bound],
            "validation": {"status": idea.validation_status, "refusals": [],
                           "requirements": [asdict(r) for r in idea.requirements],
                           "families": []},
            "governed_plan": {
                "physical_plan_id": idea.plan_envelope.physical_plan_id
                if idea.plan_envelope is not None else "",
                "ordered_path": list(idea.plan_envelope.ordered_path)
                if idea.plan_envelope is not None else [],
                "governed_variant_id": identity.governed_variant_id,
                "physical_plan_content_hash": identity.physical_plan_content_hash,
                "parameter_binding_hash": identity.parameter_binding_hash,
                "definition_origin": identity.definition_origin,
                "review_missing_roles": list(option.governance.review_missing_roles),
                "policy_revision_ids": list(option.governance.policy_revision_ids)},
        },
        # Mirrors `_decision_manifest` EXACTLY — same keys, same imports, same one-hash rule.
        "decision_manifest": {
            "semantic_context_hash": context_hash,
            "authority_matrix_hash": authority_matrix_hash(),
            "typed_gauntlet_version": TYPED_GAUNTLET_VERSION,
            "operand_class_map_version": OPERAND_CLASS_MAP_VERSION,
            "planning_request_hash": identity.planning_request_hash,
            "binding_plan_hash": binding_plan_hash,
            "recipe_revision_hash": request.source_content_hash,
        },
        # C2a / migration 1135 — THE ARMING MARKER, and this is the only moment it can be set.
        # A governed cross-catalog option IS a logical plan, so its row declares itself planned:
        # 1135's deferred totality trigger then REQUIRES a `considered_option_plan_binding` for it
        # by COMMIT, and `binding_chain.bind_considered_option_plan` refuses to bind any option
        # whose marker is false. The value is INSERT-only (1063 refuses UPDATE and the writer ends
        # in ON CONFLICT DO NOTHING), so an option served without it can NEVER be armed later —
        # which is why the serving lane fails CLOSED instead, dropping an option whose logical
        # identity it could not persist rather than serving a card nothing could ever be drafted
        # against. See `gate1._scoped_governed_cross_catalog_lens`.
        "requires_logical_plan_binding": True,
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
            " binding_plan, binding_plan_hash, requires_logical_plan_binding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
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
             facts.get("binding_plan_hash"),
             # C2a — migration 1135's arming marker, written in THIS insert or never. The column
             # is INSERT-only (1063 refuses UPDATE) and this statement ends in ON CONFLICT DO
             # NOTHING, so a second, marked write of the same option is silently dropped: an
             # option written unmarked can never be armed afterwards. `.get` with a False default
             # because a facts dict assembled by an older build carries no such key and must stay
             # writable — and PRE-PLAN is the honest reading of its silence.
             bool(facts.get("requires_logical_plan_binding", False))))
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


GOVERNED_CROSS_CATALOG = "governed_cross_catalog"


def _governed_read_set_pairs(plan: dict, label: str) -> tuple[tuple[str, str], ...]:
    """S1A-5a — a ``governed_cross_catalog`` read set, PARSED into (catalog, object_ref) pairs.

    A single-catalog plan stores bare ``schema.table.column`` refs and one ``catalog_source`` every
    entry inherits. A cross-catalog plan has no single catalog to inherit from, so each entry is a
    fully qualified ``source::schema.table.column`` and must be attributed to its OWN catalog
    before any authority can be measured against it.

    The parsing is strict and it is all-or-nothing, for the same reason :func:`_verified_plan`
    refuses a plan that fails its seal: the read set is the complete statement of what a
    materialization may read. An entry nobody can parse cannot be attributed to a catalog, and
    skipping it would produce an authority floor measured over a SUBSET of the plan while
    reporting it as the whole. So one malformed entry refuses the whole load, and the refusal
    carries the option key (which decision to regenerate) and the parser's own message (what is
    wrong with it).

    The count equality is not decoration either: it is the invariant that nothing was deduped,
    dropped or expanded between the bytes the decision froze and the pairs the floor will measure.
    """
    from featuregen.overlay.upload.qualified_ref import parse_qualified_ref

    stored = list(plan.get("read_set") or ())
    parsed = []
    for entry in stored:
        try:
            parsed.append(parse_qualified_ref(entry))
        except ValueError as e:
            raise OptionDecisionIntegrityError(
                f"the frozen governed cross-catalog plan of option decision {label} carries a "
                f"read-set entry that is not a governed qualified ref: {e}. Every entry of a "
                f"cross-catalog read set must name its own catalog "
                f"(source::schema.table.column), because there is no single plan catalog for it "
                f"to inherit — an entry nobody can attribute is one nobody can measure an "
                f"execution authority against") from e
    if len(parsed) != len(stored):    # pragma: no cover — the loop appends exactly once per entry
        raise OptionDecisionIntegrityError(
            f"the frozen governed cross-catalog plan of option decision {label} stores "
            f"{len(stored)} read-set entries but {len(parsed)} parsed: a read set that changed "
            f"size between storage and measurement is not the one this decision recorded")
    return tuple((ref.catalog_source, f"{ref.schema_name}.{ref.table}.{ref.column}")
                 for ref in parsed)


def load_frozen_option_facts(conn, *, considered_revision_id: str,
                             option_id: str) -> FrozenOptionFactsV1 | None:
    """The activation policy's frozen layer, from the exact (revision, option) key.

    ``read_set`` and ``plan_catalog_source`` come from the frozen PLAN — migration 1066's column
    where the row has one, the ``dataset_story`` copy where it does not (:func:`_verified_plan`) —
    so a legacy row keeps answering exactly as it did while a new row reads its own field.

    S1A-5a adds ``plan_kind`` and, for a ``governed_cross_catalog`` plan only,
    ``read_set_pairs`` — every entry parsed and proven by :func:`_governed_read_set_pairs`. The
    gate is the plan KIND: a ``single_source`` read set holds BARE refs that the governed parser
    refuses by design, so parsing unconditionally would refuse every legacy row in the table.
    Legacy rows are byte-identical to what they loaded before.

    Raises:
        OptionDecisionIntegrityError: the stored plan disagrees with the manifest's seal, or a
            governed cross-catalog read set carries an entry that is not a qualified ref.
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
    label = f"{considered_revision_id}/{option_id}"
    plan = _verified_plan(plan_column, story, manifest, label) or {}
    plan_kind = plan.get("plan_kind") or ""
    read_set_pairs = (_governed_read_set_pairs(plan, label)
                      if plan_kind == GOVERNED_CROSS_CATALOG else ())
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
        readiness_blockers=tuple((story or {}).get("readiness_blockers") or ()),
        confirmed_uoa_entity=(story or {}).get("confirmed_uoa_entity") or "",
        read_set=tuple(plan.get("read_set") or ()),
        plan_catalog_source=plan.get("catalog_source") or "",
        operand_authorities=tuple(sorted(
            ((story or {}).get("operand_authorities") or {}).items())),
        plan_kind=plan_kind,
        read_set_pairs=read_set_pairs)


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


def _formula_schema_supported(recipe_id: str) -> str:
    """C2 + §6 — can the selected engine run this recipe's REVIEWED formula expectation?

    Returns one of ``activation_policy``'s ``SCHEMA_SUPPORT_*`` values, because the old bool's
    ``False`` was three different facts wearing one engine-capability claim — and for an
    unreviewed recipe the claim was FALSE: engine support was never measured. Now:

    * no reviewed expectation → ``UNMEASURED_UNREVIEWED`` — a governance gap, named as one;
    * reviewed but nothing bindable to classify, unknown engine, or any raise →
      ``UNMEASURED_ENGINE`` — measurement absence, still fail-closed downstream;
    * ``classify_demands_for_engine`` actually ran → ``SUPPORTED`` or ``UNSUPPORTED``, the only
      two outcomes entitled to speak about the engine.

    The reviewed object production can resolve is the CAPTURE BLUEPRINT
    (``recipe_formula_shadow.capture_blueprint_for`` — the same object a review event's hash
    covers, chosen by the recipe's own declared schema version). Its expressions carry exactly
    the engine-relevant demands; the pinned ``gold_v2`` fixture stays the TEST-side proof.
    """
    from featuregen.overlay.upload.activation_policy import (
        SCHEMA_SUPPORT_SUPPORTED,
        SCHEMA_SUPPORT_UNMEASURED_ENGINE,
        SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED,
        SCHEMA_SUPPORT_UNSUPPORTED,
    )

    try:
        from featuregen.formula.capability_v2 import classify_demands_for_engine
        from featuregen.formula.schema_v2 import WindowBasisV2
        from featuregen.materialize.engine_capability import engine_capability_for
        from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
            RecipeFormulaExpectationBlueprintV2,
        )
        from featuregen.overlay.upload.recipe_formula_shadow import capture_blueprint_for

        if not has_reviewed_formula_expectation(recipe_id):
            return SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED
        capture = capture_blueprint_for(recipe_id)
        if capture is None:
            return SCHEMA_SUPPORT_UNMEASURED_ENGINE
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
        verdict = classify_demands_for_engine(
            demands, uses_window_offset=uses_offset, uses_future_horizon=uses_future,
            engine=engine_capability_for("kedro-pyspark"))
        return (SCHEMA_SUPPORT_SUPPORTED if verdict == "ok"
                else SCHEMA_SUPPORT_UNSUPPORTED)
    except Exception:                         # measurement absent → unmeasured, fail closed
        return SCHEMA_SUPPORT_UNMEASURED_ENGINE


def _gold_evaluation_recorded(conn, recipe_id: str) -> bool:
    """C3 — has a gold + provider evaluation PASSED for this recipe's reviewed expectation, under
    the world that is current NOW?

    ▲ **THIS USED TO BE A HARDCODED `False`**, and its docstring named exactly what was missing: not
    a store — migration 1029 has recorded evaluation outcomes for a long time — but *"a READER WITH
    A VALIDITY CONTRACT"*, because *"a stale pass is not a pass"* and returning `True` for one would
    launder an old verdict into a present authority.

    That reader now exists (`current_evaluation_validity`), and the validity contract it checks is
    the evaluation contract of migration 1097: the grammar, output-policy and canonicalization
    versions, the reviewed corpus, the expectation registry and the byte-frozen author and critic
    provider contracts. All derivable from this build, so "was this produced under the current
    world" is a hash comparison rather than a judgement.

    **It still answers `False` today, and for a reason that is now a NAMED one with a path to
    `True`**: the reviewed corpus holds a single clean case, so no run over it is certifiable
    (§0.10 step 5B). The difference from the old constant is not the answer — it is that the answer
    is now derived, and it will change by itself when the reviewed corpus grows.

    Keyed by EXPECTATION REF, never by recipe id: a recipe's ref is its own name for only 3 of the
    317 registry recipes. A candidate the registry never minted, or a recipe declaring no formula,
    has nothing to have evaluated — honestly `False`, not an error.
    """
    from featuregen.overlay.upload.current_evaluation_validity import current_evaluation_validity
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    definition = v2_recipe_by_id(recipe_id)
    if definition is None or definition.formula is None:
        return False
    return current_evaluation_validity(conn, definition.formula.expectation_ref).is_current


def _requirements_closed(conn, contract_id: str | None,
                         codes: tuple[str, ...]) -> bool:
    """C3 — every frozen outstanding requirement code has a recorded CURRENT pass.

    The validation store is CONTRACT-keyed (``feature_validation_requirement`` +
    the 1009 event stream) and an option row carries no contract identity — the plan's
    "a real read over the option" needed the caller to say WHICH contract, so the read rides
    an explicit ``contract_id``. No codes → closed (nothing outstanding). No contract →
    ``False``, fail closed: nothing recorded is not the same as nothing owed."""
    if not codes:
        return True
    if not contract_id:
        return False
    try:
        from featuregen.overlay.upload.feature_validation_projection import (
            passed_requirement_codes,
        )

        return set(codes) <= set(passed_requirement_codes(conn, contract_id))
    except Exception:                         # unreadable store → not closed, fail closed
        return False


def _authority_floors(conn, logical_refs: list[str]) -> tuple[bool, bool]:
    """C2's two floors over an already-qualified set of logical refs: ``(authoring, execution)``.

    ONE batched resolver read (C1's read-only pins — the same law generation served from), one
    authority per ref through ``field_resolution.pin_authority``, and one ``clears`` fold per floor.
    The set is authorized as a WHOLE or not at all: a materialization reads every column in its read
    set, so one operand short of the floor is the whole plan short of it.

    ``pin_authority`` is IMPORTED, never re-stated: S1A-5b promoted it beside
    :class:`ResolutionPinV1` so the durable write's floors and the serving lens
    (``contract/governed_lens``) apply ONE rule to one pin. They previously carried two — the
    accepted conflict-gate here, the verbatim D4 clause there — which meant one option could be
    told its operand had ``human/confirmed`` authority at the floor and ``absent`` on the card.

    An empty ref list clears nothing — ``all()`` over nothing is vacuously true, and a floor that
    passed because it measured no columns would be the most dangerous answer this function could
    give. The CALLER decides whether an empty measurement counts as evaluated at all.
    """
    from featuregen.overlay.upload.field_resolution import (
        current_resolution_pins,
        pin_authority,
    )
    from featuregen.overlay.upload.semantic_eligibility import clears

    refs = list(dict.fromkeys(logical_refs))
    pins = current_resolution_pins(conn, logical_refs=refs, fields=("concept",))
    authorities = [pin_authority(pins.get((logical, "concept"))) for logical in refs]
    return (bool(authorities) and all(clears(a, "authoring") for a in authorities),
            bool(authorities) and all(clears(a, "execution_at_governed") for a in authorities))


def assemble_current_activation_state(conn, *, frozen: FrozenOptionFactsV1,
                                      snapshot_id: str | None,
                                      intent_id: str | None = None,
                                      contract_id: str | None = None):
    """The activation policy's CURRENT layer — the small re-read at the durable write.

    Everything here fails toward blocking (the same posture as the dataclass defaults):
    a review that cannot be re-verified is not current; an absent snapshot is unverifiable;
    a policy hash that moved since generation is drift. NOTHING here re-binds or substitutes —
    divergence surfaces as a typed regenerate blocker in the fold.

    ``contract_id`` is the governed contract this option minted, when one exists — the key
    ``requirements_closed`` reads the validation store under (C3). ``None`` is honest for an
    option that never reached a contract, and fails that one read closed."""
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
    #
    # S1A-5a: TWO arms, ONE fold. The arms differ only in how a read-set entry is qualified into a
    # logical ref — a governed cross-catalog plan carries each entry's own catalog, a single-source
    # plan has one the whole read set inherits. Everything after that (the batched pin read,
    # `field_resolution.pin_authority`, the two `clears` folds) is `_authority_floors`, shared, so
    # the two cannot drift into two answers about the same pin. The PAIR arm goes first: where both
    # are present the per-entry attribution is the precise one, and falling back to a plan-wide
    # catalog is exactly what a cross-catalog plan must never do.
    authoring_now = False
    execution_evaluated = False
    execution_now = False
    if frozen.read_set_pairs:
        try:
            from featuregen.overlay.upload.object_ref import normalize_ref

            authoring_now, execution_now = _authority_floors(
                conn, [normalize_ref(catalog, *ref.split(".")[-3:])
                       for catalog, ref in frozen.read_set_pairs])
            execution_evaluated = True        # pairs were measured, whatever they measured to
        except Exception:                     # unreadable floors → unevaluated, fail closed
            authoring_now = False
            execution_evaluated = False
            execution_now = False
    elif frozen.read_set and frozen.plan_catalog_source:
        try:
            from featuregen.overlay.upload.object_ref import normalize_ref

            logical_by_ref = {}
            for ref in frozen.read_set:
                parts = ref.split(".")
                if len(parts) >= 3:
                    logical_by_ref[ref] = normalize_ref(
                        frozen.plan_catalog_source, parts[-3], parts[-2], parts[-1])
            authoring_now, execution_now = _authority_floors(
                conn, list(logical_by_ref.values()))
            execution_evaluated = True
        except Exception:                     # unreadable floors → unevaluated, fail closed
            authoring_now = False
            execution_evaluated = False
            execution_now = False

    # B10 item 4 — the UOA re-read, and the rule is EQUALITY, not "absence is free".
    #
    # An earlier revision of this comment said "absence is FREE both ways", which is true of only
    # ONE of the three shapes and misleading about the other two. What the code does, and means:
    #
    #   * absence vs absence — the intent has never confirmed a unit of analysis and this card was
    #     served without one: they AGREE, and nothing blocks. The confirmation is optional by
    #     design (2026-08-13 steer) and an intent that never made one may still materialize.
    #   * a value vs the SAME value — agreement, nothing blocks.
    #   * absence vs a LATER confirmation (and any two different values) — DRIFT. The human
    #     confirmed a unit of analysis after this card was served, so the card answers a question
    #     the intent no longer asks. That is a regenerate, not a pass. It is the same answer the
    #     create_contract ladder has always given (draft/confirm have always passed `intent_id`);
    #     the materialization ladder passed none until S1A-5b, which is why it used to answer
    #     differently about the same option.
    #
    # `intent_id is None` (a caller with no intent to compare against) and a read that RAISES both
    # fall back to `frozen_uoa is None` — unverifiable, so an option carrying a frozen UOA fails
    # closed and one carrying none is not blocked on a comparison nobody could make.
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

    # C2 — a real read: the reviewed expectation's demands against the selected engine's
    # advertisement. Only a recipe has a reviewed expectation to ask about; every other
    # source keeps the fail-closed default.
    is_recipe = frozen.generation_source == "recipe" and bool(frozen.source_definition_id)
    from featuregen.overlay.upload.activation_policy import (
        SCHEMA_SUPPORT_SUPPORTED,
        SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED,
    )
    formula_support = (_formula_schema_supported(frozen.source_definition_id)
                       if is_recipe else SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED)

    # C3 — effective readiness is a RE-FOLD at the durable write, not an inheritance. The
    # serving fold's measured blockers (temporal/binding/policy — nothing here can re-measure
    # them) carry verbatim; the review fact and the engine verdict are re-read NOW, so an
    # un-review demotes and an advertised engine promotes, through the one fold (BR-7).
    # The fold's own vocabulary is stripped from the carried codes first — it re-derives those
    # itself, and carrying them would double-count a fact the re-read may have changed.
    effective = frozen.readiness
    if is_recipe:
        try:
            from featuregen.overlay.upload.recipe_readiness import (
                BLOCKER_ENGINE_UNSUPPORTED,
                BLOCKER_GOLD_UNPROVEN,
                BLOCKER_GRAMMAR_UNSUPPORTED,
                BLOCKER_NO_REVIEWED_EXPECTATION,
                ReadinessInputsV1,
                fold_readiness,
            )

            fold_owned = {BLOCKER_NO_REVIEWED_EXPECTATION, BLOCKER_GRAMMAR_UNSUPPORTED,
                          BLOCKER_GOLD_UNPROVEN, BLOCKER_ENGINE_UNSUPPORTED,
                          "model_feature_spec_owns_readiness"}
            effective = fold_readiness(ReadinessInputsV1(
                computation_kind=frozen.computation_kind,
                governed_policy_blockers=tuple(
                    code for code in frozen.readiness_blockers if code not in fold_owned),
                reviewed_expectation=has_reviewed_formula_expectation(
                    frozen.source_definition_id),
                grammar_verdict="ok",
                gold_validated=_gold_evaluation_recorded(conn, frozen.source_definition_id),
                engine_verdict=("ok" if formula_support == SCHEMA_SUPPORT_SUPPORTED
                                else "unsupported_engine"),
            )).state
        except Exception:                     # unfoldable → the frozen truth, never a promotion
            effective = frozen.readiness

    return CurrentActivationStateV1(
        review_current=review_now,
        policy_revisions_current=pins_current,
        uoa_current=uoa_now,
        snapshot_freshness=freshness,
        effective_readiness=effective,
        formula_expectation_revision=frozen.formula_expectation_revision,
        formula_schema_support=formula_support,
        # C3 — a real read over the validation store, keyed by the governed contract the
        # caller names; no contract or no recorded pass fails closed.
        requirements_closed=_requirements_closed(
            conn, contract_id, frozen.outstanding_requirement_codes),
        execution_authority_evaluated=execution_evaluated,
        execution_floor_met=execution_now,
        authoring_floor_met=authoring_now)


__all__ = ["GOVERNED_CROSS_CATALOG", "OPTION_DECISION_INTEGRITY_CODE",
           "OPTION_DECISION_INTEGRITY_NEXT_STEP", "OptionDecisionIntegrityError",
           "assemble_current_activation_state", "decision_facts_for_candidate",
           "decision_facts_for_governed_option",
           "frozen_binding_plan", "has_reviewed_formula_expectation",
           "load_frozen_option_facts", "load_option_decision_record",
           "persist_option_decisions"]

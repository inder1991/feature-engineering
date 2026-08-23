"""Task S1A-5b — the governed option's decision facts, proven by ROUND TRIP.

S1A-4b builds a :class:`GovernedOptionV1`; S1A-5a taught the loader to parse a governed
cross-catalog read set. Nothing joined the two: the governed builder's output could not be
FROZEN, so no governed option had ever reached the activation fold. This suite is that join,
and it is deliberately end-to-end rather than unit-shaped —

    plan → option → facts → persist → load → assemble → decide

— because every defect this seam can carry lives BETWEEN two of those steps, not inside one.
A key the writer spells and the loader does not read, a plan whose hash is computed twice, a
read-set entry stored bare where the parser demands qualified: each of those passes a unit
test of either side and fails the round trip.

**The fixtures are the S1A-4b suite's own** (imported, never re-seeded): one real V2 registry
recipe, two real catalogs, a real verified bridge, the real planner. The G3 boundary means a
SHIPPED recipe request still refuses (``physical_cardinality_unavailable``), so the resolving
leg here is the same operand-trimmed request S1A-4b uses — the one path that reaches a
resolved cross-catalog contract today.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.overlay.upload.contract.test_governed_lens_requests import (
    RECIPE_ID,
    _intent_request,
    _options,
    _plannable_request,
    _two_catalogs,
)
from tests.featuregen.overlay.upload.planner.test_plan import _freshness

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.activation_policy import activation_decision
from featuregen.overlay.upload.contract.governed_lens import fold_governed_binding_plan
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import record_review_event
from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles
from featuregen.overlay.upload.semantic_option_decision import (
    assemble_current_activation_state,
    decision_facts_for_candidate,
    decision_facts_for_governed_option,
    load_frozen_option_facts,
    persist_option_decisions,
)

# ── the harness: one governed option, frozen through the REAL writers ────────────────────────


def _recipe_option(db):
    """The recipe-origin governed cross-catalog option, from the real planner."""
    options, rejections = _options(db, [_plannable_request(v2_recipe_by_id(RECIPE_ID))])
    assert rejections == [], rejections
    (option,) = options
    return option


def _intent_option(db):
    options, rejections = _options(db, [_intent_request(v2_recipe_by_id(RECIPE_ID))])
    assert rejections == [], rejections
    (option,) = options
    return option


def _revision(db, key: str) -> tuple[str, str, str]:
    """The intent → generation run → considered revision spine one decision row hangs from."""
    db.execute("INSERT INTO contract_intent (intent_id,hypothesis,intake_mode,"
               "redacted_hypothesis) VALUES (%s,'h','hypothesis','h') ON CONFLICT DO NOTHING",
               (f"intent-{key}",))
    db.execute("INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
               "VALUES (%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING",
               (f"genrun-{key}", f"intent-{key}"))
    db.execute("INSERT INTO contract_considered_revision (considered_revision_id,intent_id,"
               "generation_run_id,considered_json,considered_content_hash,"
               "canonicalization_version) VALUES (%s,%s,%s,'{}'::jsonb,%s,'test-v1')",
               (f"rev-{key}", f"intent-{key}", f"genrun-{key}", f"hash-{key}"))
    return f"intent-{key}", f"genrun-{key}", f"rev-{key}"


def _freeze(db, option, key: str, *, snapshot_id: str | None = None, **facts_overrides):
    """Facts → row → frozen facts, through ``persist_option_decisions``/
    ``load_frozen_option_facts`` verbatim. Returns ``(revision_id, option_id, frozen)``."""
    _intent, genrun, revision = _revision(db, key)
    facts = decision_facts_for_governed_option(db, option, context_hash=f"ctx-{key}")
    facts.update(facts_overrides)
    assert persist_option_decisions(
        db, considered_revision_id=revision, generation_run_id=genrun,
        metadata_snapshot_id=snapshot_id, facts_by_option_id={f"opt-{key}": facts}) == 1
    frozen = load_frozen_option_facts(db, considered_revision_id=revision,
                                      option_id=f"opt-{key}")
    assert frozen is not None
    return revision, f"opt-{key}", frozen


def _confirm_concepts(db, pairs) -> None:
    """One HUMAN CONFIRMATION of ``concept`` per bound ref, read back through the REAL resolver.

    This is the authoring/execution floors' only input, and it is seeded as evidence rather
    than as a pin: the floors resolve it through ``current_resolution_pins``, so a stubbed pin
    would prove nothing about the law they actually apply."""
    for index, (catalog, ref) in enumerate(sorted(set(pairs))):
        db.execute(
            "INSERT INTO field_evidence (evidence_id, logical_ref, field_name, proposed_value, "
            "proposed_value_hash, producer, strength, lifecycle, producer_ref, "
            "source_snapshot_id, input_hash) VALUES (%s, %s, 'concept', '\"account_identifier\"',"
            " %s, 'human', 'confirmed', 'active', 'test', 'snap', %s)",
            (f"ev-fc-{index}", normalize_ref(catalog, *ref.split(".")[-3:]),
             f"vh-fc-{index}", f"ih-fc-{index}"))


# ── 1. the fold ──────────────────────────────────────────────────────────────────────────────


def test_the_fold_projects_the_governed_plan_verbatim(db):
    _two_catalogs(db)
    option = _recipe_option(db)
    plan = fold_governed_binding_plan(option.idea)
    envelope = option.idea.plan_envelope

    assert plan["plan_kind"] == "governed_cross_catalog"
    assert plan["catalog_sources"] == ["ops", "rev"]
    assert plan["physical_plan_id"] == envelope.physical_plan_id
    assert plan["ordered_path"] == list(envelope.ordered_path)
    assert plan["output_grain"] == envelope.target_entity == "account"
    assert plan["grain_refs"] == [["rev", "public.accounts.account_id"]]
    # every read-set entry is FULLY qualified — the dialect S1A-5a's loader parses strictly
    assert plan["read_set"] == sorted(
        normalize_ref(catalog, *ref.split(".")[-3:]) for catalog, ref in option.idea.derives_pairs)
    assert all("::" in entry for entry in plan["read_set"])
    assert set(plan["role_bindings"]) == {b.role for b in option.idea.input_role_bindings if b.ref}
    assert plan["role_bindings"]["event_ts"] == "ops::public.transactions.event_ts"


def test_the_fold_is_none_without_a_plan_envelope(db):
    _two_catalogs(db)
    idea = dataclasses.replace(_recipe_option(db).idea, plan_envelope=None)
    assert fold_governed_binding_plan(idea) is None


def test_the_fold_refuses_a_TABLE_level_read_set_entry(db):
    """A two-component ref would mis-split into a bogus qualified ref rather than fail: the
    planner's read sets are column-level, so a table-level entry reaching here is an upstream
    defect and must NAME itself instead of being silently attributed."""
    _two_catalogs(db)
    idea = dataclasses.replace(_recipe_option(db).idea,
                               derives_pairs=(("ops", "transactions.account_id"),))
    with pytest.raises(ValueError) as excinfo:
        fold_governed_binding_plan(idea)
    assert "transactions.account_id" in str(excinfo.value)


# ── 2. the facts: key-for-key with the candidate contract ────────────────────────────────────


def test_the_governed_facts_carry_EXACTLY_the_candidate_contracts_keys(db):
    """``persist_option_decisions`` maps these keys to columns POSITIONALLY, so a governed facts
    dict that spelled one key differently would write the wrong column or raise a KeyError. The
    key set is the contract, and it is asserted against the other producer rather than a
    literal."""
    from tests.featuregen.overlay.upload.test_reviewed_expectation_seam import (
        EXEMPLAR,
        _candidate,
    )

    from featuregen.overlay.upload.feature_assist import FeatureIdea

    _two_catalogs(db)
    governed = decision_facts_for_governed_option(db, _recipe_option(db), context_hash="ctx")
    candidate = decision_facts_for_candidate(
        _candidate(EXEMPLAR),
        FeatureIdea(name=EXEMPLAR, description="", derives_from=[], aggregation=None,
                    grain_table=None, source_definition_id=EXEMPLAR),
        None, "ctx")
    assert set(governed) == set(candidate)


def test_the_persisted_definition_id_is_the_CANONICAL_id_never_the_variant(db):
    """V11, stated as a test. The stored ``source_definition_id`` feeds ``v2_recipe_by_id`` and
    ``_formula_schema_supported`` at every durable write; a variant id there resolves to no
    recipe, so the option would be review-blocked forever with nothing naming why."""
    _two_catalogs(db)
    option = _recipe_option(db)
    _revision_id, _option_id, frozen = _freeze(db, option, "v11")

    assert frozen.source_definition_id == RECIPE_ID
    assert v2_recipe_by_id(frozen.source_definition_id) is not None
    # the control: the variant id is an opaque digest and resolves to NOTHING
    assert option.identity.governed_variant_id.startswith("gvar_")
    assert v2_recipe_by_id(option.identity.governed_variant_id) is None


# ── 3. the round trip ────────────────────────────────────────────────────────────────────────


def test_a_recipe_origin_option_round_trips_and_blocks_on_ITS_REVIEW(db):
    """The whole seam in one test: a governed cross-catalog option is frozen, loaded back with
    its plan kind and its per-catalog read set, re-assembled against the world NOW, and judged.
    It blocks — the recipe carries no current review — and the refusal NAMES that."""
    _two_catalogs(db)
    option = _recipe_option(db)
    _revision_id, _option_id, frozen = _freeze(db, option, "rt")

    assert frozen.plan_kind == "governed_cross_catalog"
    assert frozen.binding_state == "bound"
    assert frozen.generation_source == "recipe"
    assert frozen.plan_envelope_present is True
    # the loader's own parse: every entry attributed to ITS catalog, nothing dropped or expanded
    assert set(frozen.read_set_pairs) == set(option.idea.derives_pairs)
    assert len({catalog for catalog, _ref in frozen.read_set_pairs}) == 2

    current = assemble_current_activation_state(
        db, frozen=frozen, snapshot_id=frozen.snapshot_id or None)
    decision = activation_decision(frozen, current, "create_contract")
    codes = {blocker.code for blocker in decision.blockers}
    assert decision.allowed is False
    assert R.RECIPE_REVIEW_NOT_CURRENT in codes, codes
    assert all(blocker.next_step.strip() for blocker in decision.blockers)


def test_an_intent_origin_option_round_trips_and_blocks_as_NOT_AUTHORABLE(db, monkeypatch):
    """Origin purity, through the facts producer: an ``llm_intent`` option asks the V2 registry
    NOTHING (the monkeypatched module raises if it does), its reviewed-expectation fact is an
    honest ``False``, and it blocks as a conceptual pattern rather than as a governance gap."""
    from featuregen.overlay.upload import recipe_registry_v2

    _two_catalogs(db)
    option = _intent_option(db)

    def _never(*args, **kwargs):
        raise AssertionError("registry read on the intent path")

    monkeypatch.setattr(recipe_registry_v2, "v2_recipe_by_id", _never)
    facts = decision_facts_for_governed_option(db, option, context_hash="ctx-intent")
    assert facts["has_reviewed_formula_expectation"] is False
    assert facts["generation_source"] == "llm_intent"

    _intent, genrun, revision = _revision(db, "intent")
    assert persist_option_decisions(
        db, considered_revision_id=revision, generation_run_id=genrun,
        metadata_snapshot_id=None, facts_by_option_id={"opt-intent": facts}) == 1
    frozen = load_frozen_option_facts(db, considered_revision_id=revision,
                                      option_id="opt-intent")
    assert frozen is not None
    assert frozen.plan_kind == "governed_cross_catalog"
    assert frozen.computation_kind == "conceptual_pattern"

    current = assemble_current_activation_state(db, frozen=frozen, snapshot_id=None)
    codes = {b.code for b in activation_decision(frozen, current, "create_contract").blockers}
    assert R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE in codes, codes
    # ...and NOT the recipe-review code: an intent has no recipe whose review could be stale
    assert R.RECIPE_REVIEW_NOT_CURRENT not in codes


def test_unmapped_requirement_codes_reach_the_FROZEN_facts(db):
    """``unmapped_requirement_codes`` is the honest carrier for a reason code with no closed
    requirement builder. It is a fact about the option's evidence, so it must survive the
    freeze — the activation rules (C4) read the frozen codes and can never fire on a code the
    writer dropped."""
    _two_catalogs(db)
    option = _recipe_option(db)
    assert option.unmapped_requirement_codes == ()          # the resolving leg carries none...
    carrying = dataclasses.replace(
        option, unmapped_requirement_codes=("physical_cardinality_unavailable",))

    _revision_id, _option_id, frozen = _freeze(db, carrying, "unmapped")
    assert frozen.outstanding_requirement_codes == ("physical_cardinality_unavailable",)


# ── 4. the positive control: fully reviewed, floors cleared ──────────────────────────────────


def test_a_fully_reviewed_option_with_cleared_floors_MAY_create_a_contract(db):
    """THE POSITIVE CONTROL — without it every refusal above could be passing for the wrong
    reason.

    Everything that opens the gate here is REAL and seeded through a production writer:

    * the review — one ``record_review_event`` per role ``required_reviewer_roles`` names, at
      ``canonical_recipe_v2_hash``'s revision, from THREE distinct reviewers (the
      single-identity guard refuses one person signing a multi-role requirement);
    * the floors — one human ``concept`` confirmation per bound ref, resolved through the real
      ``current_resolution_pins``, measured per catalog by S1A-5a's pair arm;
    * the snapshot — a REAL ``build_metadata_snapshot`` over the plan's own refs, verified by
      the real ``compare_snapshot_to_current``. This is why the connection is pinned to
      REPEATABLE READ: the snapshot builder refuses any other isolation, and monkeypatching
      the freshness comparator would have faked exactly the fact the rung is about;
    * the policy pins — written by the facts producer itself from the live authority matrix.

    Nothing is monkeypatched, and ``activation_decision`` is the real fold over a real frozen
    row. ``create_contract`` is the rung asserted: the materialization rungs above it stay
    blocked on the recipe's absent reviewed formula expectation and on gold, which no seeding
    in this suite may invent.
    """
    import psycopg

    from featuregen.overlay.upload.feature_metadata_snapshot import (
        build_metadata_snapshot,
        compare_snapshot_to_current,
    )
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash

    db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    _two_catalogs(db)
    definition = v2_recipe_by_id(RECIPE_ID)
    revision_hash = canonical_recipe_v2_hash(definition)
    roles = required_reviewer_roles(definition)
    assert len(roles) >= 2
    for index, role in enumerate(roles):
        record_review_event(db, recipe_id=RECIPE_ID, recipe_revision_hash=revision_hash,
                            decision="approved", reviewer=f"reviewer-{index}",
                            reviewer_role=role)

    option = _recipe_option(db)
    assert option.governance.review_current is True         # read, never asserted
    _confirm_concepts(db, option.idea.derives_pairs)

    _intent, genrun, revision = _revision(db, "clear")
    _freshness(db, "ops", "rev")            # the review events moved the event head
    snapshot = build_metadata_snapshot(
        db, generation_run_id=genrun, refs=list(option.idea.derives_pairs),
        read_scope_hash="sha256:s1a5b-scope")
    assert compare_snapshot_to_current(db, snapshot.snapshot_id).status == "current"

    facts = decision_facts_for_governed_option(db, option, context_hash="ctx-clear")
    assert persist_option_decisions(
        db, considered_revision_id=revision, generation_run_id=genrun,
        metadata_snapshot_id=snapshot.snapshot_id,
        facts_by_option_id={"opt-clear": facts}) == 1
    frozen = load_frozen_option_facts(db, considered_revision_id=revision,
                                      option_id="opt-clear")
    assert frozen is not None and frozen.review_current is True

    current = assemble_current_activation_state(
        db, frozen=frozen, snapshot_id=frozen.snapshot_id or None)
    assert current.review_current is True
    assert current.snapshot_freshness == "current"
    assert current.policy_revisions_current is True
    assert (current.execution_authority_evaluated, current.execution_floor_met,
            current.authoring_floor_met) == (True, True, True)

    decision = activation_decision(frozen, current, "create_contract")
    assert decision.blockers == (), [b.code for b in decision.blockers]
    assert decision.allowed is True

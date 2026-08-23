"""S1A-4b — the complete governed option builder, driven by planning REQUESTS.

Read the module docstring of ``contract/governed_lens.py`` first: it names the three planner
gaps this suite measured on the way in, which decide what each test here can honestly claim.

The short version, because it shapes every fixture below:

* the governed cross-catalog path resolves for a request whose operands DECLARE their binding
  roles (``join_role`` / ``temporal_role``) — an LLM intent may declare them, and the platform's
  own ``need_metadata.derive_need_metadata`` derives them for any probe;
* no V2 recipe declares one (0 of 1195 operands across the 317-recipe registry), and
  ``metadata_resolution_mode="request_contract"`` deliberately does not consult the legacy
  resolved-need registry, so a recipe-origin request built by ``planning_request_from_recipe``
  alone reaches NO cross-catalog plan at all. ``test_as_shipped_recipe_request_reaches_no_plan``
  pins that as the honest current state, with the seed proved sound by the resolving leg.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import seed_verified_bridge
from tests.featuregen.overlay.upload.planner.test_plan import _freshness, _seed

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import key_entities_for, key_entity
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.contract import governed_lens as lens
from featuregen.overlay.upload.contract.governed_lens import (
    REQUIREMENT_BUILDER_CODES,
    DefinitionGovernanceStateV1,
    GovernedOptionV1,
    ReasonContextV1,
    governed_options_from_requests,
    governed_requests_for_scope,
)
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.need_metadata import derive_need_metadata
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.planner.requests import planning_probe
from featuregen.overlay.upload.recipe_contract_v2 import OutputSpecV2
from featuregen.overlay.upload.recipe_readiness import fold_request_readiness
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipes.transaction_foundation import (
    TRANSACTION_FOUNDATION_RECIPES,
)

_NOW = datetime(2026, 7, 14, tzinfo=UTC)

# ── the DISCOVERY RULE, executable ────────────────────────────────────────────────────────────
#
# The pinned recipe is `posted_transaction_average_amount`, chosen by the rule below over the
# 24-recipe transaction-foundation pack:
#
#   1. computation_kind == "deterministic_formula"            — 24 of 24 survive
#   2. exactly ONE entity_key operand, and its concept links to the "account" entity  — 24 of 24
#   3. exactly ONE measure operand, declared at the "transaction" grain                — 9 of 24
#      (`fee_amount` carries two measures; the 14 count/rate recipes carry none)
#   4. no policy-bearing operand BEYOND the pack-mandatory posted-eligibility `status` operand,
#      and no governed_policy parameter                                                — 4 of 24
#   5. the MINIMAL operand set — nothing beyond the key / time / measure / status quartet, so the
#      seed binds exactly what the contract declares and no more                       — 1 of 24
#
# Filter 4 DIVERGES from the task brief, which asked for "no status_policy_ref/governed_policy_ref
# operands" at full strength. That filter is unsatisfiable by construction: `_base_operands` gives
# every one of the 24 recipes the governed `status` operand carrying
# `eligible_status:foundation-posted-events`, and every one of them also carries eligibility
# policy_refs — so ALL 24 are eliminated and the rule yields nothing.
# `test_the_literal_policy_free_filter_eliminates_every_recipe` pins that measurement, so the
# relaxation is visible rather than assumed.

RECIPE_ID = "posted_transaction_average_amount"
_BINDABLE_ROLES = ("account", "event_ts")


def _entity_of(concept_name: str) -> str | None:
    resolved = concept(concept_name)
    return resolved.entity_link if resolved is not None else None


def _discovery_candidates(*, max_policy_operands: int, minimal_operand_set: bool) -> list[str]:
    out = []
    for recipe in TRANSACTION_FOUNDATION_RECIPES:
        if recipe.computation_kind != "deterministic_formula":
            continue
        keys = [o for o in recipe.operands if o.operand_class == "entity_key"]
        if len(keys) != 1 or _entity_of(keys[0].concept) != "account":
            continue
        measures = [o for o in recipe.operands if o.operand_class == "measure"]
        if len(measures) != 1 or measures[0].allowed_source_grains != ("transaction",):
            continue
        if len([o for o in recipe.operands if o.status_policy_ref]) > max_policy_operands:
            continue
        if any(p.governed_policy_ref for p in recipe.parameters):
            continue
        if minimal_operand_set and len(recipe.operands) != 4:
            continue
        out.append(recipe.recipe_id)
    return out


def test_the_discovery_rule_pins_exactly_one_recipe():
    assert _discovery_candidates(max_policy_operands=1, minimal_operand_set=True) == [RECIPE_ID]
    # ...and dropping only the minimality tie-break leaves the four near-misses it settles.
    assert _discovery_candidates(max_policy_operands=1, minimal_operand_set=False) == [
        RECIPE_ID, "return_amount", "cross_border_amount", "cash_withdrawal_amount"]


def test_the_literal_policy_free_filter_eliminates_every_recipe():
    """The brief's filter 4 at full strength: no recipe in the pack survives it."""
    assert _discovery_candidates(max_policy_operands=0, minimal_operand_set=False) == []
    # every one of the 24 is eliminated by the SAME pack-mandatory operand, not by chance
    assert all(any(o.status_policy_ref == "eligible_status:foundation-posted-events"
                   for o in r.operands) for r in TRANSACTION_FOUNDATION_RECIPES)


# ── the seed: the chosen recipe's own operands, split across two catalogs ─────────────────────


def _two_catalogs(db) -> None:
    """`ops` holds the transaction-grain table the recipe's operands bind to (account key, event
    time, amount, booking status); `rev` holds the account-grain landing table. The roll-up to the
    account grain completes ONLY over the verified bridge, so every plan below spans BOTH."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("ops", "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow("ops", "transactions", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("ops", "transactions", "status", "text"), "booking_status"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    seed_verified_bridge(db, "bfk_s1a4b", entity="account", left_source="ops",
                         left_ref="public.transactions.account_id", right_source="rev",
                         right_ref="public.accounts.account_id")
    _freshness(db, "ops", "rev")


def _plannable_request(recipe, **overrides) -> FeaturePlanningRequestV1:
    """The chosen recipe's request, reduced to the operands the planner can bind TODAY and
    carrying the binding roles the platform's OWN ``derive_need_metadata`` resolves for its probe.

    Both adjustments compensate for planner gaps this task may not fix (see the lens docstring):
    G1 — ``request_contract`` metadata resolution derives no join/temporal role, and the assembler
    starts only from a ``source_entity_key`` binding; G2 — the derivation maps every non-entity,
    non-temporal operand (``amount``, ``status``) to ``JoinRole.MEASURE``, and a measure on a
    fan-in hop needs a physical cardinality the governed bridge hop cannot supply (G3), so keeping
    them makes the contract unresolvable for a reason that has nothing to do with this builder.
    Everything identity-, governance- and display-bearing is the REGISTRY's: id, content hash,
    objective, output, temporal spec, formula reference, parameters."""
    base = planning_request_from_recipe(recipe)
    trimmed = dataclasses.replace(
        base, operands=tuple(op for op in base.operands if op.role in _BINDABLE_ROLES))
    resolved = {m.role: m for m in derive_need_metadata(planning_probe(trimmed))}
    return dataclasses.replace(trimmed, operands=tuple(
        dataclasses.replace(op, join_role=str(resolved[op.role].join_role),
                            temporal_role=str(resolved[op.role].temporal_role))
        for op in trimmed.operands), **overrides)


_INTENT_OUTPUT = OutputSpecV2(
    output_id="account_transaction_activity", display_label="Account transaction activity",
    output_type="integer", additivity="additive", unit_kind="count",
    null_input_policy="ineligible rows are excluded, not nulled",
    empty_population_policy="an empty window returns zero")


def _intent_request(recipe, *, window: int = 90) -> FeaturePlanningRequestV1:
    """The SAME physical shape as the recipe leg, arriving as an LLM intent — the origin-neutrality
    proof. An intent MAY declare its operands' binding roles: the roles are the planner's governed
    vocabulary, not physical references, so nothing here chooses a column."""
    plannable = _plannable_request(recipe)
    return FeaturePlanningRequestV1(
        origin="llm_intent", source_definition_id=f"intent:account_activity:{window}",
        source_revision="1", source_content_hash=f"intent-hash-{window}",
        primary_objective=recipe.primary_objective, output=_INTENT_OUTPUT,
        operands=tuple(RequiredOperandV1(
            role=op.role, concept=op.concept, operand_class=op.operand_class,
            required=op.required, allowed_source_grains=op.allowed_source_grains,
            join_role=op.join_role, temporal_role=op.temporal_role)
            for op in plannable.operands),
        source_grain=recipe.source_grain, output_grain=recipe.output_grain,
        temporal=recipe.temporal, computation_kind="conceptual_pattern",
        conceptual_reason="a governed pattern: account activity rolled up from transaction events",
        parameter_values=(("window", window),))


def _options(db, requests, **kwargs):
    return governed_options_from_requests(
        db, requests=tuple(requests), target_entity="account", roles=(), now=_NOW, **kwargs)


# ── 1. the real registry recipe, resolved across two catalogs ────────────────────────────────


def test_recipe_origin_option_from_the_real_registry_recipe(db):
    _two_catalogs(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    options, rejections = _options(db, [_plannable_request(recipe)])
    assert rejections == []
    assert len(options) == 1
    option = options[0]
    assert isinstance(option, GovernedOptionV1)

    # the envelope genuinely spans BOTH catalogs
    envelope = option.idea.plan_envelope
    assert envelope is not None
    assert set(envelope.catalog_sources) == {"ops", "rev"}

    # identity + origin purity
    assert option.identity.canonical_definition_id == RECIPE_ID
    assert option.identity.definition_origin == "recipe_v2"
    assert option.identity.planning_request_hash == planning_request_hash(
        _plannable_request(recipe))
    assert len(option.identity.physical_plan_content_hash) == 64
    assert option.identity.governed_variant_id.startswith("gvar_")
    assert option.idea.recipe_id == RECIPE_ID
    assert option.idea.source_definition_id == RECIPE_ID
    assert option.idea.generation_source == "recipe"
    assert option.idea.origin == "governed_planner"
    assert option.idea.path_authority == "governed_cross_catalog"
    assert option.idea.planner_applicability == "applicable_cross_catalog"

    # every required operand of the request carries a role binding
    bound_roles = {b.role for b in option.idea.input_role_bindings}
    assert bound_roles == {op.role for op in _plannable_request(recipe).operands}
    # …and the two derives_pairs invariants
    pairs = set(option.idea.derives_pairs)
    assert {b.ref for b in option.idea.input_role_bindings if b.ref} <= pairs
    assert len({cs for cs, _ref in option.idea.derives_pairs}) > 1
    assert ("rev", "public.accounts.account_id") in pairs

    # the qualified output grain, in the LANDING catalog — and the landing TABLE, carried only
    # because the batched governed key-entity read confirms the grain key names "account"
    assert option.idea.grain_refs == (("rev", "public.accounts.account_id"),)
    assert option.idea.grain_table == "public.accounts"
    assert option.idea.time_ref == ("ops", "public.transactions.event_ts")

    # display is the registry's, verbatim
    assert option.display_name == "Average amount"
    assert option.business_definition == (
        "Posted amount sum divided by posted count over the window.")

    # governance is READ, never asserted
    assert option.governance.retired is False
    assert option.governance.review_current is False
    assert option.governance.review_missing_roles == (
        "banking_sme", "data_semantic_owner", "formula_engineering")
    assert option.governance.reviewed_expectation is False
    assert option.governance.policy_revision_ids == (
        "eligible_status:foundation-posted-events",
        "reversal_correction:foundation-flag-or-code")
    assert option.readiness.state == "FORMULA_BLOCKED"
    assert "no_reviewed_formula_expectation" in option.readiness.blockers
    assert option.unmapped_requirement_codes == ()


def test_as_shipped_recipe_request_reaches_no_plan(db):
    """The honest measurement of the gap the fixture above compensates for.

    ``governed_requests_for_scope`` mints the request the SHIPPED adapter produces — operands with
    no declared binding role — and no cross-catalog plan is assembled from it at all. The seed is
    proved sound by the resolving leg in the same test, so this can never be a silent fixture bug.
    """
    _two_catalogs(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    shipped = governed_requests_for_scope(db, eligible_recipe_ids=frozenset({RECIPE_ID}))
    assert [r.source_definition_id for r in shipped] == [RECIPE_ID]
    assert all(op.join_role == "" for op in shipped[0].operands)

    options, rejections = _options(db, shipped)
    assert options == []
    assert len(rejections) == 1
    rejection = rejections[0]
    assert rejection["lens"] == "governed"
    assert rejection["recipe_id"] == RECIPE_ID
    assert rejection["request_hash"] == shipped[0].source_content_hash
    assert isinstance(rejection["reason"], str) and rejection["reason"]
    assert rejection["unmet_hops"] == []

    # the control: the SAME seed, the SAME recipe, with the binding roles the platform derives
    control, control_rejections = _options(db, [_plannable_request(recipe)])
    assert control_rejections == [] and len(control) == 1


# ── 2. the same builder, an intent origin ────────────────────────────────────────────────────


def test_intent_origin_through_the_same_builder(db, monkeypatch):
    _two_catalogs(db)

    def _never(*args, **kwargs):   # origin purity: no registry read for an llm_intent request
        raise AssertionError("registry read on the intent path")

    monkeypatch.setattr(lens, "v2_recipe_by_id", _never)
    monkeypatch.setattr(lens, "review_events_all", _never)

    recipe = v2_recipe_by_id(RECIPE_ID)
    options, rejections = _options(db, [_intent_request(recipe)])
    assert rejections == []
    (option,) = options
    assert option.idea.generation_source == "llm_intent"
    assert option.idea.recipe_id is None
    assert option.identity.definition_origin == "llm_intent"
    assert option.identity.canonical_definition_id == "intent:account_activity:90"
    assert option.idea.source_definition_id == "intent:account_activity:90"
    assert option.governance == DefinitionGovernanceStateV1(
        retired=False, review_current=False, review_missing_roles=(),
        reviewed_expectation=False, policy_revision_ids=())
    assert option.readiness.state == "CONCEPTUAL_ONLY"          # folded from the request's kind
    assert option.display_name == "Account transaction activity"
    assert option.business_definition == recipe.primary_objective
    assert option.idea.window == "90"
    assert option.idea.operation_kind == "conceptual_pattern"
    assert option.idea.operation_class == ""
    assert option.idea.aggregation == "conceptual"
    assert len({cs for cs, _ref in option.idea.derives_pairs}) > 1


# ── 3. conflict-gated authority ──────────────────────────────────────────────────────────────


def test_authority_is_gated_on_the_pin_conflict_state(db, monkeypatch):
    _two_catalogs(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    real = lens.current_resolution_pins

    def _pins(conn, *, logical_refs, fields):
        from featuregen.overlay.upload.field_resolution import ResolutionPinV1

        del conn, fields
        out = {}
        for ref in logical_refs:
            conflicted = ref.endswith("account_id")
            out[(ref, "concept")] = ResolutionPinV1(
                value="account_id" if conflicted else "event_timestamp",
                producer="human", strength="confirmed", evidence_id=f"ev-{ref}",
                conflict_state="conflict" if conflicted else "resolved", load_bearing=True)
        return out

    assert real is not _pins
    monkeypatch.setattr(lens, "current_resolution_pins", _pins)
    (option,) = _options(db, [_plannable_request(recipe)])[0]
    by_role = {b.role: b for b in option.idea.input_role_bindings}
    assert by_role["account"].authority == "absent"          # conflicted → never load-bearing
    assert by_role["event_ts"].authority == "human/confirmed"
    assert by_role["event_ts"].evidence_ids == (
        f"ev-{by_role['event_ts'].ref[0]}::public.transactions.event_ts",)
    assert by_role["event_ts"].confirmation_required is False


# ── 4. the batched enrichment ────────────────────────────────────────────────────────────────


def _counting(monkeypatch, name):
    calls = []
    original = getattr(lens, name)

    def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(lens, name, wrapper)
    return calls


def test_enrichment_reads_are_batched_and_constant(db, monkeypatch):
    _two_catalogs(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    pins = _counting(monkeypatch, "current_resolution_pins")
    entities = _counting(monkeypatch, "key_entities_for")
    reviews = _counting(monkeypatch, "review_events_all")

    one, rejected = _options(db, [_plannable_request(recipe)])
    assert len(one) == 1 and rejected == []
    assert (len(pins), len(entities), len(reviews)) == (1, 1, 1)

    three, rejected = _options(db, [
        _plannable_request(recipe),
        _plannable_request(recipe, parameter_values=(("window", 90),)),
        _plannable_request(recipe, parameter_values=(("window", 180),))])
    assert len(three) == 3 and rejected == []
    assert (len(pins), len(entities), len(reviews)) == (2, 2, 2)   # ONE batch each, not four


# ── 5. retirement ────────────────────────────────────────────────────────────────────────────


def test_a_retired_definition_folds_readiness_retired(db, monkeypatch):
    _two_catalogs(db)
    recipe = v2_recipe_by_id(RECIPE_ID)
    retired = dataclasses.replace(recipe, readiness="RETIRED")
    monkeypatch.setattr(lens, "v2_recipe_by_id", lambda recipe_id: retired)
    (option,) = _options(db, [_plannable_request(recipe)])[0]
    assert option.governance.retired is True
    assert option.readiness.state == "RETIRED"


# ── 6. the closed requirement builders ───────────────────────────────────────────────────────


def test_the_requirement_builder_key_set_is_declared_verbatim():
    assert tuple(lens._REQUIREMENT_BUILDERS) == REQUIREMENT_BUILDER_CODES
    assert REQUIREMENT_BUILDER_CODES == (
        ReasonCode.physical_cardinality_unavailable,
        ReasonCode.ingredient_not_connected_to_path,
        ReasonCode.aggregation_axis_unsupported,
        ReasonCode.aggregation_strategy_missing,
        ReasonCode.aggregation_declaration_conflict,
        ReasonCode.semi_additive_temporal_strategy_missing,
        ReasonCode.temporal_anchor_missing,
        ReasonCode.temporal_anchor_ambiguous,
        ReasonCode.leakage_anchor_read,
        ReasonCode.protected_attribute_read,
        ReasonCode.binding_safety_rejected,
    )
    assert all(isinstance(code, ReasonCode) for code in REQUIREMENT_BUILDER_CODES)


def test_requirements_are_built_from_the_registry_and_unmapped_codes_are_carried():
    ref = ("rev", "public.accounts.account_id")
    contexts = (
        ReasonContextV1(code=ReasonCode.physical_cardinality_unavailable,
                        role="output_grain", catalog_source=ref[0], object_ref=ref[1]),
        ReasonContextV1(code=ReasonCode.temporal_anchor_missing, role="event_ts",
                        catalog_source="ops", object_ref="public.transactions.event_ts"),
        ReasonContextV1(code=ReasonCode.freshness_stamp_unavailable, role="",
                        catalog_source="ops", object_ref="public.transactions.event_ts"),
    )
    requirements, unmapped = lens._project_requirements(contexts)
    assert [r.code for r in requirements] == ["GRAIN_IS_UNIQUE"]
    assert requirements[0].operand == ref
    assert requirements[0].schema_version == "v1"       # the REGISTRY's version for this code
    assert requirements[0].params == ()
    # a builder that returns None keeps its code a refusal, never a requirement and never unmapped
    assert unmapped == ("freshness_stamp_unavailable",)


# ── 7. the one identity helper's boundary assertions ─────────────────────────────────────────


class _FakePlan:
    physical_plan_id = "bp_0adcb0a8c5748e1a"
    recipe_id = RECIPE_ID


def test_identity_refuses_a_truncated_display_id():
    request = _plannable_request(v2_recipe_by_id(RECIPE_ID))
    with pytest.raises(ValueError) as excinfo:
        lens._governed_variant_identity(
            request, physical_plan_content_hash="bp_0adcb0a8c5748e1a",
            parameter_binding_hash="")
    assert "physical_plan_content_hash" in str(excinfo.value)


def test_identity_refuses_a_none_parameter_binding_hash():
    request = _plannable_request(v2_recipe_by_id(RECIPE_ID))
    with pytest.raises(ValueError) as excinfo:
        lens._governed_variant_identity(request, physical_plan_content_hash="a" * 64,
                                        parameter_binding_hash=None)
    assert "parameter_binding_hash" in str(excinfo.value)


def test_identity_refuses_a_separator_in_the_canonical_id():
    request = dataclasses.replace(_plannable_request(v2_recipe_by_id(RECIPE_ID)),
                                  source_definition_id="posted|average")
    with pytest.raises(ValueError) as excinfo:
        lens._governed_variant_identity(request, physical_plan_content_hash="a" * 64,
                                        parameter_binding_hash="")
    assert "canonical_definition_id" in str(excinfo.value)


# ── 8. request minting: dedup by request hash, NEVER by canonical id ─────────────────────────


def test_requests_dedup_by_request_hash_never_by_canonical_id(db, caplog):
    recipe = v2_recipe_by_id(RECIPE_ID)
    intent = _intent_request(recipe)
    duplicate = _intent_request(recipe)                     # byte-identical content → one hash
    variant = _plannable_request(recipe, parameter_values=(("window", 180),))
    with caplog.at_level("INFO"):
        requests = governed_requests_for_scope(
            db, eligible_recipe_ids=frozenset({RECIPE_ID, "no_such_recipe"}),
            intent_requests=(intent, duplicate, variant))
    hashes = [planning_request_hash(r) for r in requests]
    assert len(hashes) == len(set(hashes)) == 3             # recipe primary + intent + variant
    # the variant shares the recipe's canonical id and is KEPT — dedup is never by id
    assert [r.source_definition_id for r in requests].count(RECIPE_ID) == 2
    assert "no_such_recipe" in caplog.text


# ── 9. the additive batched catalog read + the additive readiness fold ───────────────────────


def test_grain_table_is_absent_when_the_key_entity_read_cannot_confirm_it(db, monkeypatch):
    """The plan's own ``output_grain_ref`` stands as the planner's fact; the landing TABLE is a
    governed confirmation, so it is absent when the batched read cannot make it."""
    _two_catalogs(db)
    monkeypatch.setattr(lens, "key_entities_for", lambda conn, refs: dict.fromkeys(refs))
    (option,) = _options(db, [_plannable_request(v2_recipe_by_id(RECIPE_ID))])[0]
    assert option.idea.grain_refs == (("rev", "public.accounts.account_id"),)
    assert option.idea.grain_table is None


def test_key_entities_for_matches_key_entity_per_ref(db):
    _two_catalogs(db)
    refs = [("ops", "public.transactions.account_id"), ("ops", "public.transactions.amount"),
            ("rev", "public.accounts.account_id"), ("rev", "public.accounts.missing")]
    batched = key_entities_for(db, refs)
    assert batched == {ref: key_entity(db, *ref) for ref in refs}
    assert batched[("rev", "public.accounts.account_id")] == "account"
    assert batched[("rev", "public.accounts.missing")] is None


def test_fold_request_readiness_folds_the_requests_own_kind():
    recipe = v2_recipe_by_id(RECIPE_ID)
    request = _plannable_request(recipe)
    governance = DefinitionGovernanceStateV1(
        retired=False, review_current=False, review_missing_roles=(),
        reviewed_expectation=True, policy_revision_ids=())
    clean = fold_request_readiness(request, governance)
    assert clean.state == "FORMULA_AUTHORABLE"              # gold is the only thing left
    blocked = fold_request_readiness(
        request, governance, temporal_blockers=("temporal_anchor_missing",))
    assert blocked.state == "FORMULA_BLOCKED"
    assert "temporal_anchor_missing" in blocked.blockers
    retired = fold_request_readiness(
        request, dataclasses.replace(governance, retired=True))
    assert retired.state == "RETIRED"

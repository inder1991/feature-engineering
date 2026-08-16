"""SE-1 — the neutral planning contracts: every recipe adapts, origins converge, hashes move."""
from __future__ import annotations

from dataclasses import fields, replace

import pytest

from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    PlanningContractError,
    planning_request_from_feature_intent,
    planning_request_from_recipe,
    planning_request_from_user_definition,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import _canonical_dataclass
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_recipe_by_id

EXEMPLAR = v2_recipe_by_id("posted_debit_amount")


def test_every_v2_recipe_adapts_to_the_planning_contract():
    """The SE-1 acceptance proof, derived from the registry — never a literal count."""
    requests = [planning_request_from_recipe(recipe) for recipe in V2_RECIPES]
    assert len(requests) == len(V2_RECIPES) >= 317
    for recipe, request in zip(V2_RECIPES, requests):
        assert request.origin == "recipe_v2"
        assert request.source_definition_id == recipe.recipe_id
        assert len(request.operands) == len(recipe.operands)
        assert request.computation_kind == recipe.computation_kind


def test_operand_projection_is_a_copy_not_a_translation():
    request = planning_request_from_recipe(EXEMPLAR)
    for spec, projected in zip(EXEMPLAR.operands, request.operands):
        assert (projected.role, projected.concept, projected.operand_class) \
            == (spec.role, spec.concept, spec.operand_class)
        assert projected.allowed_source_grains == spec.allowed_source_grains
        assert projected.suggestion_authority == spec.suggestion_authority
        assert projected.execution_authority == spec.execution_authority


def test_parameter_resolution_defaults_bounds_and_unknowns():
    request = planning_request_from_recipe(EXEMPLAR)
    declared = {p.name: p for p in EXEMPLAR.parameters}
    for name, value in request.parameter_values:
        parameter = declared[name]
        if parameter.parameter_class != "governed_policy":
            assert value == parameter.allowed_values[0]      # authored default = first
    windowed = next((p for p in EXEMPLAR.parameters
                     if p.parameter_class != "governed_policy"), None)
    if windowed is not None:
        with pytest.raises(PlanningContractError, match="outside the bounded"):
            planning_request_from_recipe(EXEMPLAR, {windowed.name: object()})
    with pytest.raises(PlanningContractError, match="unknown parameters"):
        planning_request_from_recipe(EXEMPLAR, {"no_such_parameter": 1})


def test_two_variants_are_two_identities():
    windowed = next((p for p in EXEMPLAR.parameters
                     if p.parameter_class != "governed_policy"
                     and len(p.allowed_values) > 1), None)
    if windowed is None:
        pytest.skip("exemplar has no multi-value parameter")
    a = planning_request_from_recipe(EXEMPLAR, {windowed.name: windowed.allowed_values[0]})
    b = planning_request_from_recipe(EXEMPLAR, {windowed.name: windowed.allowed_values[1]})
    assert planning_request_hash(a) != planning_request_hash(b)


def test_hash_is_deterministic_and_field_exhaustive():
    """Same input → same hash, and EVERY dataclass field is hash-bearing — the canonical dict
    carries every field name, so adding a field moves the hash by mechanism."""
    a = planning_request_from_recipe(EXEMPLAR)
    b = planning_request_from_recipe(EXEMPLAR)
    assert planning_request_hash(a) == planning_request_hash(b)
    canonical = _canonical_dataclass(a)
    assert set(canonical) == {f.name for f in fields(FeaturePlanningRequestV1)}
    moved = replace(a, source_grain=a.source_grain + "_x")
    assert planning_request_hash(moved) != planning_request_hash(a)


def test_non_user_origins_may_not_carry_binding_hints():
    request = planning_request_from_recipe(EXEMPLAR)
    hinted = tuple(replace(op, binding_hint_refs=("public.t.c",)) if i == 0 else op
                   for i, op in enumerate(request.operands))
    with pytest.raises(PlanningContractError, match="may not choose columns"):
        replace(request, operands=hinted)


def test_the_user_adapter_demotes_refs_to_hints_and_stays_conceptual():
    base = planning_request_from_recipe(EXEMPLAR)
    hinted_operand = replace(base.operands[0], binding_hint_refs=("public.txns.txn_amt",))
    request = planning_request_from_user_definition(
        definition_id="user:my_outflow", primary_objective=EXEMPLAR.primary_objective,
        output=EXEMPLAR.output, operands=(hinted_operand, *base.operands[1:]),
        source_grain=EXEMPLAR.source_grain, output_grain=EXEMPLAR.output_grain,
        temporal=EXEMPLAR.temporal, content_hash="userhash")
    assert request.origin == "user_definition"
    assert request.computation_kind == "conceptual_pattern"      # no reviewed formula yet
    assert request.operands[0].binding_hint_refs == ("public.txns.txn_amt",)


def test_deterministic_requires_formula_and_conceptual_requires_reason():
    base = planning_request_from_recipe(EXEMPLAR)
    with pytest.raises(PlanningContractError, match="formula reference"):
        replace(base, formula=None)
    with pytest.raises(PlanningContractError, match="WHY it is conceptual-only"):
        replace(base, computation_kind="conceptual_pattern", formula=None)


def test_duplicate_roles_and_bad_origin_are_refused():
    base = planning_request_from_recipe(EXEMPLAR)
    with pytest.raises(PlanningContractError, match="duplicate operand roles"):
        replace(base, operands=(base.operands[0], base.operands[0]))
    with pytest.raises(PlanningContractError, match="origin"):
        replace(base, origin="wishful_thinking")


def test_intent_adapter_converges_on_the_recipe_shape():
    """A recipe and an intent describing the same atomic output produce structurally
    comparable requests — no downstream caller needs the origin to evaluate eligibility."""
    from featuregen.overlay.upload.feature_intent import (
        FeatureIntentV1,
        GenerationProvenanceV1,
    )

    base = planning_request_from_recipe(EXEMPLAR)
    intent = FeatureIntentV1(
        display_name="Posted debit amount (model)",
        business_definition="Total posted debit amount over the window.",
        primary_objective=EXEMPLAR.primary_objective,
        computation_kind="deterministic_formula",
        operation_class=EXEMPLAR.formula.result_class,
        output=EXEMPLAR.output,
        output_grain_entity=EXEMPLAR.output_grain,
        source_grain=EXEMPLAR.source_grain,
        operands=base.operands,
        temporal=EXEMPLAR.temporal,
        eligibility=EXEMPLAR.eligibility,
        leakage=EXEMPLAR.leakage,
        generation_provenance=GenerationProvenanceV1(
            prompt_ref="p", output_schema_version="feature-intent-1", model="m",
            call_ref="c", confirmed_scope_hash="s"))
    request = planning_request_from_feature_intent(intent)
    assert request.origin == "llm_intent"
    assert request.output == base.output
    assert request.operands == base.operands
    assert request.temporal == base.temporal
    # The readiness ceiling is structural: no reviewed expectation → conceptual, formula pending.
    assert request.computation_kind == "conceptual_pattern"
    assert "formula pending" in request.conceptual_reason
    assert request.formula is None

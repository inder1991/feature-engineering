"""Task A2 — the v2 expectation blueprint DERIVED from the recipe definition (D-1).

The measurement this task owes: how many of the 317 banking recipes are executable-shaped, and
by what NAME each of the rest is not. The counts below are pinned so a registry edit that
silently loses derivability fails CI.
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter
from dataclasses import replace
from inspect import signature

from featuregen.formula.schema_leaves import EmptyWindowResult, NullInput, WindowUnit
from featuregen.formula.schema_v2 import (
    AggregateFunctionV2,
    FinalOperationV2,
    WindowBasisV2,
)
from featuregen.overlay.upload.recipe_formula_blueprint_derivation import (
    AGGREGATION_UNDECLARED,
    AUTHORITY_REFS_AMBIGUOUS,
    DERIVATION_REFUSAL_CODES,
    DERIVED_TIMEZONE,
    GRAIN_KEY_UNRESOLVED,
    MULTIPLE_MEASURE_OPERANDS_UNRESOLVED,
    NO_MEASURE_OPERAND,
    NOT_A_DETERMINISTIC_FORMULA,
    OUTPUT_POLICY_UNDERIVABLE,
    PARAMETER_PROJECTION_UNDERIVABLE,
    TEMPORAL_BLOCKED,
    WINDOW_NOT_EVENT_ANCHORED,
    WINDOW_UNIT_UNSUPPORTED,
    BlueprintDerivationRefusal,
    derive_blueprint_v2,
    derive_registry_blueprints,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    bind_formula_expectation_v2,
    validate_blueprint_v2,
)
from featuregen.overlay.upload.recipe_formula_expectations import RECIPE_FORMULA_EXPECTATIONS
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    content_hash as grounding_content_hash,
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_recipe_by_id
from featuregen.overlay.upload.templates import (
    BindingResolution,
    GroundedNeedBinding,
    SourceEntityRoleResolution,
)

GOLD_V2 = (pathlib.Path(__file__).parents[2] / "formula" / "gold_v2"
           / "30_posted_debit_amount_exemplar.json")

#: The A2 measurement, pinned. 90 of 317 banking recipes are executable-shaped today; every
#: other recipe carries ONE named blocker, which is a concrete piece of registry work.
EXPECTED_OUTCOMES = {
    "DERIVED": 90,
    WINDOW_NOT_EVENT_ANCHORED: 102,
    MULTIPLE_MEASURE_OPERANDS_UNRESOLVED: 65,
    NOT_A_DETERMINISTIC_FORMULA: 19,
    AGGREGATION_UNDECLARED: 19,
    OUTPUT_POLICY_UNDERIVABLE: 6,
    WINDOW_UNIT_UNSUPPORTED: 6,
    NO_MEASURE_OPERAND: 4,
    TEMPORAL_BLOCKED: 3,
    PARAMETER_PROJECTION_UNDERIVABLE: 2,
    GRAIN_KEY_UNRESOLVED: 1,
}


def _outcome(value) -> str:
    return value.code if isinstance(value, BlueprintDerivationRefusal) else "DERIVED"


def test_derivation_reads_nothing_but_the_definition():
    """Pure by signature: one positional parameter, and no connection anywhere near it."""
    parameters = signature(derive_blueprint_v2).parameters
    assert list(parameters) == ["definition"]
    assert "conn" not in parameters


def test_derivation_is_total_over_the_registry_or_refuses_by_name():
    outcomes = Counter(_outcome(v) for v in derive_registry_blueprints().values())
    assert sum(outcomes.values()) == len(V2_RECIPES) == 317
    assert dict(outcomes) == EXPECTED_OUTCOMES
    assert set(outcomes) - {"DERIVED"} <= set(DERIVATION_REFUSAL_CODES)


def test_every_refusal_code_but_one_is_reachable_on_the_real_registry():
    """A code no definition can produce is a code nobody has to act on. Ten of the eleven fire
    on the shipped registry; `AUTHORITY_REFS_AMBIGUOUS` does not, because no recipe today
    declares two governed policy refs of the SAME kind — the seven recipes with a repeated
    prefix repeat a non-authority one (`threshold:`, `risk_corridor:`). It is reachable by
    construction (its own test below) and is kept, because the derivation must not silently
    pick one of two governed policies the day a pack does declare both."""
    seen = {_outcome(v) for v in derive_registry_blueprints().values()}
    assert seen - {"DERIVED"} == set(DERIVATION_REFUSAL_CODES) - {AUTHORITY_REFS_AMBIGUOUS}


def test_every_derived_blueprint_validates_and_is_deterministic():
    for recipe_id, value in derive_registry_blueprints().items():
        if isinstance(value, BlueprintDerivationRefusal):
            continue
        validate_blueprint_v2(value)
        assert value == derive_blueprint_v2(v2_recipe_by_id(recipe_id))
        assert value.recipe_id == recipe_id
        assert value.expectation_ref == v2_recipe_by_id(recipe_id).formula.expectation_ref


def test_the_exemplar_derives_to_the_reviewed_gold_fixture_shape():
    """The fixture is the oracle: this is what proves derivation is not invention.

    Every field the blueprint and the reviewed proposal both carry must agree. Three do NOT,
    and each is recorded rather than fudged — see the acceptance row:
      * timezone — the registry declares none for any recipe, so the derivation states UTC;
      * window inclusivity — the fixture is [start, end), the recipe's own compiled PIT text is
        (start, end];
      * the policy-ref NAMESPACE — the gold corpus writes `policy:eligible-posted-status`, the
        registry writes `eligible_status:foundation-posted-events`.
    """
    fixture = json.loads(GOLD_V2.read_text())["proposal"]
    expression = fixture["body"]["expr"]
    blueprint = derive_blueprint_v2(v2_recipe_by_id("posted_debit_amount"))
    assert not isinstance(blueprint, BlueprintDerivationRefusal)
    derived = blueprint.expressions[0]

    assert blueprint.final_operation.value == fixture["body"]["final_operation"]
    assert blueprint.body_shape == "unary"
    assert derived.expression_path == "body.expr"
    assert derived.aggregation.value == expression["aggregation"]
    assert derived.window.basis.value == expression["window"]["basis"]
    assert derived.window.unit.value == expression["window"]["unit"]
    assert derived.window.empty_window.value == expression["window"]["empty_window"]
    assert derived.window.null_input.value == expression["window"]["null_input"]
    assert derived.window.offset_periods == expression["window"].get("offset_periods", 0)
    assert blueprint.grain.entity == fixture["grain"]["entity"]
    assert len(blueprint.grain.key_roles) == len(fixture["grain"]["keys"])
    assert blueprint.decimal.precision == fixture["decimal"]["precision"]
    assert blueprint.decimal.scale == fixture["decimal"]["scale"]
    assert blueprint.decimal.rounding.value == fixture["decimal"]["rounding"]
    # Every authority the reviewed proposal declares is declared by the derivation too — the
    # NAMES differ (two namespaces), the SET of governed policies does not.
    for field in ("status_policy_ref", "direction_policy_ref", "reversal_policy_ref",
                  "currency_conversion_ref"):
        assert getattr(derived.authority_refs, field), field
        assert expression["authority_refs"][field], field

    # The three recorded disagreements, pinned so they cannot drift unnoticed.
    assert derived.window.timezone == DERIVED_TIMEZONE != expression["window"]["timezone"]
    assert derived.window.start_inclusive.value != expression["window"]["start_inclusive"]
    assert (derived.authority_refs.status_policy_ref
            != expression["authority_refs"]["status_policy_ref"])


def test_a_derived_blueprint_takes_its_grain_from_the_definition():
    """D-7 as STRUCTURE. The reviewed v1 blueprint for `merchant_mcc_diversity` is
    merchant-grain and the V2 recipe computes per customer; the derived blueprint cannot
    reproduce that mismatch, because it reads the same definition the candidate came from.
    The v1 registry entry is left exactly as it is — the re-key is a governance act."""
    blueprint = derive_blueprint_v2(v2_recipe_by_id("merchant_mcc_diversity"))
    assert blueprint.grain.entity == "customer"
    assert blueprint.grain.key_roles == ("customer",)
    assert blueprint.expressions[0].aggregation is AggregateFunctionV2.COUNT_DISTINCT
    assert RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"].grain.entity == "merchant"


def test_a_conceptual_pattern_is_not_a_deterministic_formula():
    refusal = derive_blueprint_v2(v2_recipe_by_id("salary_confidence"))
    assert refusal.code == NOT_A_DETERMINISTIC_FORMULA


def test_a_ratio_result_class_refuses_rather_than_inventing_a_second_expression():
    definition = v2_recipe_by_id("posted_debit_amount")
    ratio = replace(definition, formula=replace(definition.formula, result_class="ratio"),
                    output=replace(definition.output, additivity="non_additive"))
    refusal = derive_blueprint_v2(ratio)
    assert refusal.code == MULTIPLE_MEASURE_OPERANDS_UNRESOLVED
    assert "two expressions" in refusal.detail


def test_an_undeclared_aggregation_refuses_by_name():
    definition = v2_recipe_by_id("posted_debit_amount")
    extremum = replace(definition, formula=replace(definition.formula, result_class="extremum"),
                       output=replace(definition.output, additivity="non_additive"))
    refusal = derive_blueprint_v2(extremum)
    assert refusal.code == AGGREGATION_UNDECLARED
    assert "extremum" in refusal.detail


def test_prose_that_names_two_readings_is_underivable():
    definition = v2_recipe_by_id("posted_debit_amount")
    muddled = replace(definition, output=replace(
        definition.output,
        empty_population_policy="an empty window returns zero, or null when unconvertible"))
    refusal = derive_blueprint_v2(muddled)
    assert refusal.code == OUTPUT_POLICY_UNDERIVABLE


def test_two_policy_refs_of_one_kind_are_a_governance_answer_not_a_derivation():
    definition = v2_recipe_by_id("posted_debit_amount")
    doubled = replace(definition, eligibility=replace(
        definition.eligibility,
        policy_refs=(*definition.eligibility.policy_refs, "eligible_status:second-opinion")))
    refusal = derive_blueprint_v2(doubled)
    assert refusal.code == AUTHORITY_REFS_AMBIGUOUS


def test_a_derived_blueprint_binds_against_a_grounded_context():
    """The seam A4 will use: a blueprint that came from a definition, bound by A1's binder to
    the exact columns the shared binder chose. Derivation and binding are one path."""
    blueprint = derive_blueprint_v2(v2_recipe_by_id("posted_debit_amount"))
    parameters = (("window", 90),)

    def _binding(role: str, ref: str) -> GroundedNeedBinding:
        return GroundedNeedBinding(
            role=role, catalog_source="bank", logical_ref=ref,
            graph_object_ref=ref.replace("bank::", "public."), expected_concept=role,
            optional=False, join_role=None, temporal_role=None, distinct_binding_group=None,
            binding_resolution=BindingResolution.UNIQUE, tied_candidate_logical_refs=(ref,),
            tied_candidate_set_hash="set-hash")

    definition_json = {"version": "derived-probe"}
    context = RecipeGroundingContextV1(
        recipe_candidate_key="candidate", recipe_id="posted_debit_amount",
        source_entity_need_role="account",
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=(_binding("account", "bank::public.txns.acct_id"),
                       _binding("amount", "bank::public.txns.txn_amt"),
                       _binding("event_ts", "bank::public.txns.booking_ts")),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(
            "posted_debit_amount", parameters),
        template_definition=definition_json,
        template_content_hash=grounding_content_hash(definition_json))

    bound = bind_formula_expectation_v2(context, blueprint)
    assert bound.expectation_ref == "posted_debit_amount"
    assert bound.grain_entity == "account"
    assert bound.grain_key_refs == ("bank::public.txns.acct_id",)
    assert bound.expressions[0].operand_ref == "bank::public.txns.txn_amt"
    assert bound.expressions[0].event_time_ref == "bank::public.txns.booking_ts"
    assert bound.expressions[0].source_relation_ref == "bank::public.txns"
    assert bound.expressions[0].window_length == 90
    assert bound.expressions[0].aggregation is AggregateFunctionV2.SUM
    assert bound.final_operation is FinalOperationV2.IDENTITY
    assert bound.expressions[0].window.basis is WindowBasisV2.TRAILING
    assert bound.expressions[0].window.unit is WindowUnit.DAY
    assert bound.expressions[0].window.empty_window is EmptyWindowResult.ZERO
    assert bound.expressions[0].window.null_input is NullInput.IGNORE

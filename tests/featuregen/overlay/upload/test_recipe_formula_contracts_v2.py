"""Task A1 — the Formula-v2 expectation contract and its binder.

The v1 binder is the oracle for behaviour: the v2 binder must raise the SAME closed preflight
codes for the same situations, so the formula shadow classifies both generations with one
vocabulary. What is new is what v2 can express — every v2 aggregate, four body shapes, window
offsets, aggregate arguments, second operands and governed authority refs.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.formula.schema_leaves import (
    EmptyWindowResult,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    WindowUnit,
)
from featuregen.formula.schema_v2 import (
    AggregateFunctionV2,
    AuthorityRefsV2,
    FinalOperationV2,
    WindowBasisV2,
)
from featuregen.overlay.upload.recipe_formula_contracts import (
    DecimalPolicyExpectationV1,
    RecipeFormulaPreflightError,
    SemanticParameterProjectionKind,
    SemanticParameterProjectionV1,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    ExpressionRoleExpectationV2,
    GrainExpectationV2,
    RecipeFormulaExpectationBlueprintV2,
    WindowPolicyExpectationV2,
    bind_formula_expectation_v2,
    expectation_content_hash_v2,
    validate_blueprint_v2,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    content_hash as grounding_content_hash,
)
from featuregen.overlay.upload.templates import (
    BindingResolution,
    GroundedNeedBinding,
    SourceEntityRoleResolution,
)

RECIPE_ID = "posted_debit_amount"
EXPECTATION_REF = "posted_debit_amount"
DEFINITION = {"version": "test-v2"}
PARAMETERS = (("window", 90),)

WINDOW = WindowPolicyExpectationV2(
    event_time_role="event_ts", basis=WindowBasisV2.TRAILING, length_parameter="window",
    unit=WindowUnit.DAY, start_inclusive=Inclusivity.INCLUSIVE,
    end_inclusive=Inclusivity.EXCLUSIVE, timezone="Asia/Dubai",
    empty_window=EmptyWindowResult.ZERO, null_input=NullInput.IGNORE)

AUTHORITY = AuthorityRefsV2(status_policy_ref="policy:eligible-posted-status",
                            direction_policy_ref="policy:dc-sign-convention")

PROJECTIONS = (SemanticParameterProjectionV1(
    recipe_parameter="window",
    projection_kind=SemanticParameterProjectionKind.AST_PATH,
    canonical_formula_paths=("body.expr.window.length",)),)

DECIMAL = DecimalPolicyExpectationV1(precision=38, scale=6, rounding=RoundingMode.HALF_EVEN,
                                     overflow=OverflowBehavior.ERROR)


def _blueprint(**overrides) -> RecipeFormulaExpectationBlueprintV2:
    base = dict(
        recipe_id=RECIPE_ID, expectation_ref=EXPECTATION_REF,
        final_operation=FinalOperationV2.IDENTITY,
        expressions=(ExpressionRoleExpectationV2(
            expression_path="body.expr", aggregation=AggregateFunctionV2.SUM,
            operand_role="amount", source_relation_role="amount", window=WINDOW,
            authority_refs=AUTHORITY),),
        grain=GrainExpectationV2(entity="account", key_roles=("account",)),
        semantic_parameter_projections=PROJECTIONS, decimal=DECIMAL)
    base.update(overrides)
    return RecipeFormulaExpectationBlueprintV2(**base)


def _binding(role: str, ref: str, concept: str,
             resolution: BindingResolution = BindingResolution.UNIQUE) -> GroundedNeedBinding:
    return GroundedNeedBinding(
        role=role, catalog_source="bank", logical_ref=ref,
        graph_object_ref=ref.replace("bank::", "public."), expected_concept=concept,
        optional=False, join_role=None, temporal_role=None, distinct_binding_group=None,
        binding_resolution=resolution, tied_candidate_logical_refs=(ref,),
        tied_candidate_set_hash="set-hash")


BINDINGS = (
    _binding("account", "bank::public.txns.acct_id", "account_id"),
    _binding("amount", "bank::public.txns.txn_amt", "monetary_flow"),
    _binding("event_ts", "bank::public.txns.booking_ts", "event_timestamp"),
)


def _context(*, bindings=BINDINGS, recipe_id=RECIPE_ID,
             source_role="account", **overrides) -> RecipeGroundingContextV1:
    base = dict(
        recipe_candidate_key="candidate", recipe_id=recipe_id,
        source_entity_need_role=source_role,
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=tuple(bindings), semantic_parameters=PARAMETERS,
        semantic_parameter_binding_hash=semantic_parameter_hash(recipe_id, PARAMETERS),
        template_definition=DEFINITION,
        template_content_hash=grounding_content_hash(DEFINITION))
    base.update(overrides)
    return RecipeGroundingContextV1(**base)


# ───────────────────────────── the blueprint's own law ─────────────────────────────


def test_the_exemplar_blueprint_validates():
    validate_blueprint_v2(_blueprint())


def test_the_body_shape_is_derived_from_the_final_operation():
    assert _blueprint().body_shape == "unary"
    assert _blueprint(
        final_operation=FinalOperationV2.RATIO,
        expressions=(
            replace(_blueprint().expressions[0], expression_path="body.numerator"),
            replace(_blueprint().expressions[0], expression_path="body.denominator"),
        )).body_shape == "ratio"


def test_a_ratio_blueprint_must_carry_both_canonical_paths():
    with pytest.raises(ValueError, match="body.numerator"):
        validate_blueprint_v2(_blueprint(final_operation=FinalOperationV2.RATIO))


def test_count_rows_may_not_declare_an_operand_role():
    with pytest.raises(ValueError, match="carries no operand"):
        validate_blueprint_v2(_blueprint(expressions=(
            replace(_blueprint().expressions[0],
                    aggregation=AggregateFunctionV2.COUNT_ROWS),)))


def test_a_percentile_without_its_argument_is_refused():
    with pytest.raises(ValueError, match="percentile requires"):
        validate_blueprint_v2(_blueprint(expressions=(
            replace(_blueprint().expressions[0],
                    aggregation=AggregateFunctionV2.PERCENTILE),)))


def test_an_order_sensitive_aggregate_may_not_read_a_future_horizon():
    """The schema's own law, restated on the blueprint: a future window has no observed history."""
    with pytest.raises(ValueError, match="order-sensitive"):
        validate_blueprint_v2(_blueprint(expressions=(
            replace(_blueprint().expressions[0],
                    aggregation=AggregateFunctionV2.LAST_KNOWN,
                    window=replace(WINDOW, basis=WindowBasisV2.FUTURE_HORIZON)),)))


def test_an_offset_beyond_the_cap_is_refused():
    with pytest.raises(ValueError, match="offset_periods"):
        validate_blueprint_v2(_blueprint(expressions=(
            replace(_blueprint().expressions[0],
                    window=replace(WINDOW, offset_periods=13)),)))


def test_only_a_signed_sum_names_and_signs_its_terms():
    with pytest.raises(ValueError, match="only a signed sum"):
        validate_blueprint_v2(_blueprint(expressions=(
            replace(_blueprint().expressions[0], term_name="inflow", term_sign=1),)))


# ───────────────────────────── the binder ─────────────────────────────


def test_binding_a_v2_expectation_produces_exact_refs_for_every_role():
    bound = bind_formula_expectation_v2(_context(), _blueprint())
    expression = bound.expressions[0]
    assert expression.source_relation_ref == "bank::public.txns"
    assert expression.operand_ref == "bank::public.txns.txn_amt"
    assert expression.event_time_ref == "bank::public.txns.booking_ts"
    assert expression.second_operand_ref is None
    assert expression.window_length == 90
    assert expression.authority_refs == AUTHORITY
    assert bound.grain_entity == "account"
    assert bound.grain_key_refs == ("bank::public.txns.acct_id",)
    assert bound.expectation_ref == EXPECTATION_REF
    assert len(bound.blueprint_content_hash) == 64


def test_a_recipe_the_context_does_not_name_is_a_mismatch():
    with pytest.raises(RecipeFormulaPreflightError, match="RECIPE_EXPECTATION_MISMATCH"):
        bind_formula_expectation_v2(_context(recipe_id="other_recipe"), _blueprint())


def test_a_tampered_definition_is_a_hash_mismatch():
    with pytest.raises(RecipeFormulaPreflightError, match="RECIPE_DEFINITION_HASH_MISMATCH"):
        bind_formula_expectation_v2(
            _context(template_definition={"version": "tampered"}), _blueprint())


def test_a_tampered_parameter_binding_is_a_hash_mismatch():
    with pytest.raises(RecipeFormulaPreflightError, match="SEMANTIC_PARAMETER_HASH_MISMATCH"):
        bind_formula_expectation_v2(
            _context(semantic_parameter_binding_hash="0" * 64), _blueprint())


def test_a_v2_blueprint_whose_grain_role_is_not_bound_is_refused():
    """D-7 in the v2 binder: a blueprint keyed to a different grain entity than the context
    resolved cannot bind. The refusal is structural, not a warning."""
    with pytest.raises(RecipeFormulaPreflightError,
                       match="FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED"):
        bind_formula_expectation_v2(
            _context(), _blueprint(grain=GrainExpectationV2(entity="merchant",
                                                            key_roles=("merchant",))))


def test_a_duplicated_role_is_ambiguous():
    duplicated = (*BINDINGS, _binding("amount", "bank::public.txns.other_amt", "monetary_flow"))
    with pytest.raises(RecipeFormulaPreflightError, match="FORMULA_BINDING_AMBIGUOUS"):
        bind_formula_expectation_v2(_context(bindings=duplicated), _blueprint())


def test_a_non_unique_resolution_is_ambiguous():
    tied = (BINDINGS[0],
            _binding("amount", "bank::public.txns.txn_amt", "monetary_flow",
                     BindingResolution.AMBIGUOUS),
            BINDINGS[2])
    with pytest.raises(RecipeFormulaPreflightError, match="FORMULA_BINDING_AMBIGUOUS"):
        bind_formula_expectation_v2(_context(bindings=tied), _blueprint())


def test_a_role_the_context_never_bound_is_missing():
    with pytest.raises(RecipeFormulaPreflightError, match="FORMULA_BINDING_MISSING"):
        bind_formula_expectation_v2(_context(bindings=BINDINGS[:2]), _blueprint())


def test_a_cross_table_expression_is_unsupported():
    across = (BINDINGS[0],
              _binding("amount", "bank::public.other.txn_amt", "monetary_flow"),
              BINDINGS[2])
    with pytest.raises(RecipeFormulaPreflightError, match="FORMULA_AUTHORING_UNSUPPORTED"):
        bind_formula_expectation_v2(_context(bindings=across), _blueprint())


def test_a_window_parameter_the_variant_never_chose_is_invalid():
    with pytest.raises(RecipeFormulaPreflightError,
                       match="SEMANTIC_PARAMETER_PROJECTION_INCOMPLETE"):
        bind_formula_expectation_v2(
            _context(), _blueprint(semantic_parameter_projections=()))


def test_a_non_positive_window_length_is_invalid():
    parameters = (("window", 0),)
    context = _context(semantic_parameters=parameters,
                       semantic_parameter_binding_hash=semantic_parameter_hash(
                           RECIPE_ID, parameters))
    with pytest.raises(RecipeFormulaPreflightError, match="SEMANTIC_WINDOW_INVALID"):
        bind_formula_expectation_v2(context, _blueprint())


def test_the_bound_expectation_is_hash_stable_over_role_order():
    """Role ORDER in the grounding context is an accident of the binder's iteration; the bound
    expectation's identity must not move with it."""
    forward = bind_formula_expectation_v2(_context(), _blueprint())
    reversed_context = _context(bindings=tuple(reversed(BINDINGS)))
    backward = bind_formula_expectation_v2(reversed_context, _blueprint())
    assert forward == backward
    assert (expectation_content_hash_v2(_blueprint())
            == forward.blueprint_content_hash == backward.blueprint_content_hash)


def test_a_second_operand_binds_to_its_own_ref():
    """The v2 fork v1 has no vocabulary for: a row-level binary operation's second column."""
    blueprint = _blueprint(expressions=(ExpressionRoleExpectationV2(
        expression_path="body.expr", aggregation=AggregateFunctionV2.DATE_DIFF_AVG,
        operand_role="event_ts", source_relation_role="amount",
        second_operand_role="amount", window=WINDOW),))
    validate_blueprint_v2(blueprint)
    bound = bind_formula_expectation_v2(_context(), blueprint)
    assert bound.expressions[0].second_operand_ref == "bank::public.txns.txn_amt"

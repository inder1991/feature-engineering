"""Task 6 — operation map + corrected additivity + C1 output-authority resolver.

The authoritative output policy is resolved ONLY from C1 governed facts (the ``per_expr_facts``
``OperationalValue`` bundles seeded through the REAL governed path by the Task-5 fixtures) — NEVER
from the proposal's advisory ``expected_output``. See ``docs/superpowers/specs/...child1...`` §B
(operation map), §C (the operation-specific required-field matrix over C1) and §D (corrected
additivity).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.formula.c1_fixtures import (
    seed_fork,
    seed_hash_mismatch,
    seed_not_operational,
    seed_projection_unavailable,
    seed_resolved,
)
from tests.featuregen.formula.factories import (
    AMOUNT_REF,
    TABLE_REF,
    customer_grain,
    default_decimal,
    ratio_of_sums,
    sum_expression,
    trailing_90d_window,
)

from featuregen.formula.operations import to_path_aggregation
from featuregen.formula.output_authority import (
    ExprFacts,
    ExternalRequirement,
    InvalidOutput,
    NeedsAuthority,
    PartitionProof,
    formula_additivity,
    resolve_formula_output_policy,
)
from featuregen.formula.schema import (
    AdditivityClass,
    AggregateExpression,
    AggregateFunction,
    DiffBody,
    ExpectedOutput,
    FormulaOutputPolicyV1,
    SourceRelation,
    TypedFormulaProposalV1,
    UnaryBody,
)
from featuregen.overlay.field_authority import InfluenceTier
from featuregen.overlay.upload.operational_facts import (
    OperationalValue,
    read_operational_value,
)
from featuregen.overlay.upload.planner.multisource_contracts import PathAggregation

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _proposal(body, *, expected_output: ExpectedOutput | None = None) -> TypedFormulaProposalV1:
    return TypedFormulaProposalV1(
        formula_schema_version=1, operation_grammar_version=1, canonicalization_version=1,
        grain=customer_grain(), body=body, parameters=(), decimal=default_decimal(),
        expected_output=expected_output)


def _seed_unit_hint(db, source: str, unit_value: str) -> OperationalValue:
    """A REAL C1 HINT read of ``unit`` = ``unit_value`` (status ``not_operational``, value carried):
    the Task-5 ``seed_not_operational`` gives the live-hint decision; the flat ``unit`` column is then
    populated the way ingest writes hint metadata (the C1 read surfaces the flat value)."""
    col = seed_not_operational(db, source=source)
    db.execute(
        "UPDATE graph_node SET unit = %s WHERE catalog_source = %s AND object_ref = %s",
        [unit_value, col.source, col.object_ref])
    return read_operational_value(db, col.logical_ref, "unit")


def _read(db, col, field: str) -> OperationalValue:
    return read_operational_value(db, col.logical_ref, field)


# ── small builders ────────────────────────────────────────────────────────────────────────────────
def _ov(status: str, value: object | None) -> OperationalValue:
    """A hand-built ``OperationalValue`` for a pure-logic (no-DB) additivity test — the same shape
    C1 returns. The DB-backed resolver tests below use the REAL Task-5 fixtures instead."""
    return OperationalValue(
        value=value, influence=InfluenceTier.DISPLAY, producer=None, strength=None, status=status,
        conflict_status=None, selected_evidence_ids=(), decision_event_id="d1", fact_key=None,
        fact_event_id=None, policy_version="v", resolver_version="v")


def _unary(aggregation: AggregateFunction, *, operand: str | None = AMOUNT_REF) -> UnaryBody:
    return UnaryBody(expr=AggregateExpression(
        aggregation=aggregation, operand=operand,
        source_relation=SourceRelation(table_ref=TABLE_REF), filter=None,
        window=trailing_90d_window()))


def _facts(**fields: OperationalValue) -> ExprFacts:
    return ExprFacts(**fields)


# ── §D corrected additivity ───────────────────────────────────────────────────────────────────────
def test_count_distinct_is_non_additive():
    """§D — the BUG fix: a distinct count does NOT sum across partitions (``b_output_policy`` wrongly
    marks it ``additive``). Default (no disjointness proof) → NON_ADDITIVE."""
    body = _unary(AggregateFunction.COUNT_DISTINCT)
    result = formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof())
    assert result is AdditivityClass.NON_ADDITIVE


def test_count_distinct_additive_only_with_proven_disjoint_values():
    body = _unary(AggregateFunction.COUNT_DISTINCT)
    proof = PartitionProof(disjoint_distinct_values=True)
    assert formula_additivity(body, per_expr_facts={}, partition_proof=proof) \
        is AdditivityClass.ADDITIVE


def test_count_rows_additive_only_across_disjoint_partitions():
    body = _unary(AggregateFunction.COUNT_ROWS, operand=None)
    assert formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE
    proof = PartitionProof(disjoint_row_partitions=True)
    assert formula_additivity(body, per_expr_facts={}, partition_proof=proof) \
        is AdditivityClass.ADDITIVE


def test_ratio_is_non_additive():
    body = ratio_of_sums()
    assert formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE


def test_difference_is_non_additive():
    body = DiffBody(minuend=sum_expression(), subtrahend=sum_expression())
    assert formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE


def test_sum_carries_governed_additive_input_with_a_path_proof():
    body = _unary(AggregateFunction.SUM)
    facts = {"body.expr": _facts(additivity=_ov("resolved", "additive"))}
    proof = PartitionProof(path_additive=True)
    assert formula_additivity(body, per_expr_facts=facts, partition_proof=proof) \
        is AdditivityClass.ADDITIVE
    # …but without the path proof an additive input degrades conservatively.
    assert formula_additivity(body, per_expr_facts=facts, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE


# ── §B operation → path-aggregation compatibility map ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        (AggregateFunction.SUM, PathAggregation.sum),
        (AggregateFunction.COUNT_ROWS, PathAggregation.count),
        (AggregateFunction.COUNT_NON_NULL, PathAggregation.count),
        (AggregateFunction.COUNT_DISTINCT, PathAggregation.count_distinct),
    ],
)
def test_to_path_aggregation_maps_the_supported_vocabulary(fn, expected):
    assert to_path_aggregation(fn) is expected


# ── §C — SUM resolves from governed facts; a HINT-only unit never forces NEEDS_AUTHORITY ───────────
def test_sum_resolves_without_demanding_hint_only_unit(db):
    """SUM(amount): unit is a C1 HINT (``not_operational``), so the resolver must NOT block on it;
    output_type comes from the operand's governed facts, and the advisory ``expected_output`` (which
    here deliberately CONTRADICTS the governed facts) is never consulted."""
    add_col = seed_resolved(db, source="t6_sum_add")            # governed additivity = non_additive
    facts = {"body.expr": ExprFacts(
        additivity=_read(db, add_col, "additivity"),           # resolved / governed
        output_type=_read(db, add_col, "logical_representation"),  # value "numeric"
        unit=_seed_unit_hint(db, "t6_sum_unit", "dollars"),    # HINT, value carried
    )}
    advisory = ExpectedOutput(output_type="percentage", unit="euros", currency="EUR")
    proposal = _proposal(UnaryBody(expr=sum_expression()), expected_output=advisory)

    result = resolve_formula_output_policy(
        proposal, per_expr_facts=facts, grain_facts={}, now=_NOW)

    assert isinstance(result, FormulaOutputPolicyV1)
    assert result.output_type == "numeric"                     # governed fact — NOT "percentage"
    assert result.unit == "dollars"                            # HINT carried — NOT advisory "euros"
    assert result.currency is None                             # no currency fact — NOT advisory "EUR"
    assert result.output_additivity is AdditivityClass.NON_ADDITIVE


# ── §C — a REQUIRED field failing closed in C1 (fork/hash_mismatch/projection_unavailable) ─────────
@pytest.mark.parametrize(
    "seeder", [seed_hash_mismatch, seed_fork, seed_projection_unavailable])
def test_sum_needs_authority_when_required_additivity_fails_closed(db, seeder):
    """SUM's ``additivity`` is a REQUIRED field (§C). When C1 fails it closed — a forked head, a
    tampered value hash, or a degraded projection — the resolver fails closed to NEEDS_AUTHORITY
    rather than fabricating a policy. (``seed_projection_unavailable`` degrades globally; it is the
    only seed here and is read immediately.)"""
    col = seeder(db, source="t6_hardfail")
    add_ov = _read(db, col, "additivity")
    assert add_ov.status in {"hash_mismatch", "fork", "projection_unavailable"}

    facts = {"body.expr": ExprFacts(additivity=add_ov)}
    result = resolve_formula_output_policy(
        _proposal(UnaryBody(expr=sum_expression())),
        per_expr_facts=facts, grain_facts={}, now=_NOW)

    assert isinstance(result, NeedsAuthority)
    assert result.reason                                   # a machine reason is carried


# ── §C — DIFFERENCE demands EXACTLY compatible unit/currency ───────────────────────────────────────
def test_difference_incompatible_units_is_invalid_output(db):
    """DIFFERENCE of two operands with INCOMPATIBLE units (§C: subtracting dollars from euros is not a
    meaningful value) → INVALID_FORMULA, not merely unsupported."""
    minu_unit = _seed_unit_hint(db, "t6_diff_min", "dollars")
    subt_unit = _seed_unit_hint(db, "t6_diff_sub", "euros")
    facts = {
        "body.minuend": ExprFacts(unit=minu_unit),
        "body.subtrahend": ExprFacts(unit=subt_unit),
    }
    result = resolve_formula_output_policy(
        _proposal(DiffBody(minuend=sum_expression(), subtrahend=sum_expression())),
        per_expr_facts=facts, grain_facts={}, now=_NOW)

    assert isinstance(result, InvalidOutput)


def test_difference_compatible_units_resolves_to_that_unit(db):
    """DIFFERENCE of two SAME-unit operands → resolves, carrying THAT unit (§C)."""
    minu_unit = _seed_unit_hint(db, "t6_diff2_min", "dollars")
    subt_unit = _seed_unit_hint(db, "t6_diff2_sub", "dollars")
    type_col = seed_resolved(db, source="t6_diff2_type")
    facts = {
        "body.minuend": ExprFacts(unit=minu_unit, output_type=_read(db, type_col, "logical_representation")),
        "body.subtrahend": ExprFacts(unit=subt_unit),
    }
    result = resolve_formula_output_policy(
        _proposal(DiffBody(minuend=sum_expression(), subtrahend=sum_expression())),
        per_expr_facts=facts, grain_facts={}, now=_NOW)

    assert isinstance(result, FormulaOutputPolicyV1)
    assert result.unit == "dollars"
    assert result.output_additivity is AdditivityClass.NON_ADDITIVE


# ── §C — RATIO requires units/currency to CANCEL ───────────────────────────────────────────────────
def test_ratio_non_cancelling_units_needs_external_provisioning(db):
    """RATIO whose numerator/denominator units do NOT cancel (dollars per count) cannot be resolved to
    a dimensionless value from governed facts alone (§C) → a TYPED external requirement, not an
    indiscriminate NEEDS_AUTHORITY."""
    num_unit = _seed_unit_hint(db, "t6_ratio_num", "dollars")
    den_unit = _seed_unit_hint(db, "t6_ratio_den", "count")
    facts = {
        "body.numerator": ExprFacts(unit=num_unit),
        "body.denominator": ExprFacts(unit=den_unit),
    }
    result = resolve_formula_output_policy(
        _proposal(ratio_of_sums()), per_expr_facts=facts, grain_facts={}, now=_NOW)

    assert result == ExternalRequirement("UNIT_PROVISIONING_REQUIRED")


def test_ratio_cancelling_units_resolves_dimensionless(db):
    """RATIO of two SAME-unit operands cancels to a DIMENSIONLESS value (§C)."""
    num_unit = _seed_unit_hint(db, "t6_ratio2_num", "dollars")
    den_unit = _seed_unit_hint(db, "t6_ratio2_den", "dollars")
    facts = {
        "body.numerator": ExprFacts(unit=num_unit),
        "body.denominator": ExprFacts(unit=den_unit),
    }
    result = resolve_formula_output_policy(
        _proposal(ratio_of_sums()), per_expr_facts=facts, grain_facts={}, now=_NOW)

    assert isinstance(result, FormulaOutputPolicyV1)
    assert result.unit is None                             # cancelled → dimensionless
    assert result.output_additivity is AdditivityClass.NON_ADDITIVE

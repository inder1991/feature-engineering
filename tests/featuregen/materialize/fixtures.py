"""Spec A — the three worked features, hand-authored, and the governed catalog they author over.

Two halves that MUST agree, and a test that proves they do
(``test_fixtures.py::test_every_fixture_is_what_child1_really_resolves``):

* :func:`authored_formula` — the hand-authored :class:`TypedFormulaV1` later tasks compile;
* :func:`raw_proposal` — the raw JSON a scripted author "emits" for the SAME feature, so a test can
  drive the REAL Child-1 orchestrator and get a real authoring run, a real terminal trace event and
  a real ``candidate_formula_hash``.

WHY BOTH. Gate 1 (spec §1.2) verifies a supplied result against the immutable terminal event, so a
fixture is only useful if it is the formula an authoring run genuinely produced. Hand-authoring the
proposal alone would leave the ``TypedFormulaV1`` unverified; hand-authoring the formula alone would
leave it unprovable. Writing both and pinning ``authored_formula(name) == run(raw_proposal(name))``
makes a wrong fixture a FAILING TEST rather than a silently-forged input.

FIELD NAMES ARE VERIFIED, not remembered (interfaces reference §6, re-read at implementation):
``TypedLiteral(type=…)`` — NOT ``literal_type``; ``FilterPredicate(op, left, right_literal,
right_param, right_set)`` with ``kind`` ``init=False`` and NO ``right_ref`` at all (a predicate's
only column reference is ``left``); ``FilterBool(op, children)`` — NOT ``operands``;
``WindowPolicy.unit`` is a ``WindowUnit``.

OUTPUT POLICIES ARE VERIFIED TOO (reference §7 / ``formula/output_authority.py``), and this is where
a plausible-looking fixture becomes a FORGERY:

* a plain ``SUM`` resolves ``NON_ADDITIVE`` — ``ADDITIVE`` needs the operand's governed additivity
  to be ADDITIVE *and* ``partition_proof.path_additive``, and the resolver has no proof at resolve
  time (``output_authority._NO_PROOF``). Its ``output_type`` is the operand's GOVERNED
  ``logical_representation`` (here ``"numeric"``), and ``external_type_required`` is False only
  because that decision is governed;
* ``COUNT_DISTINCT`` resolves ``NON_ADDITIVE`` with the dimensionless logical type ``"integer"``;
* ``RATIO`` resolves ``NON_ADDITIVE`` with logical type ``"decimal"`` (``_RATIO_OUTPUT_TYPE``), and
  ``unit``/``currency`` ``None`` because they cancel.

THE CATALOG IS SEEDED THROUGH THE REAL MACHINERY — ``build_graph`` for the physical columns,
``record_field_evidence`` + ``resolve_and_project`` for the governed type decision (the only route
to ``status="resolved"``), exactly as ``tests/featuregen/formula/authoring_fixtures.py`` does. A
flat ``graph_node`` insert would skip the decision lifecycle ``read_operational_value`` derives its
statuses from, and the resolved output policy would then be a fiction.

⚠️ ``build_graph`` writes object refs under the DEFAULT schema — every ref here is
``hdfc::public.transactions.*``, not ``hdfc::banking.transactions.*``. That is the catalog's real
identity model (``object_ref._DEFAULT_SCHEMA``), not a shortcut: mapping a logical ref onto the
cluster's ``banking`` schema is physical resolution, which is Task 3/5's job, not the formula's.
"""
from __future__ import annotations

import copy
from typing import Any

from featuregen.formula.schema import (
    CANONICALIZATION_VERSION,
    FORMULA_SCHEMA_VERSION,
    OPERATION_GRAMMAR_VERSION,
    OUTPUT_POLICY_VERSION,
    AdditivityClass,
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    EmptyWindowResult,
    FilterBool,
    FilterBoolOp,
    FilterNode,
    FilterPredicate,
    FilterPredicateOp,
    FormulaOutputPolicyV1,
    Grain,
    Inclusivity,
    LiteralType,
    NullInput,
    OverflowBehavior,
    RatioBody,
    RoundingMode,
    SourceRelation,
    TypedFormulaV1,
    TypedLiteral,
    UnaryBody,
    WindowBasis,
    WindowPolicy,
    WindowUnit,
    ZeroDenominator,
)
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize.inventory import EngineVersions
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph

SOURCE = "hdfc"
TABLE = "transactions"
TABLE_REF = f"{SOURCE}::public.{TABLE}"
REF_AMT = f"{TABLE_REF}.txn_amt"
REF_DT = f"{TABLE_REF}.txn_dt"
REF_CIF = f"{TABLE_REF}.cif_id"
REF_MERCHANT = f"{TABLE_REF}.merchant_id"
REF_DR_CR = f"{TABLE_REF}.dr_cr_flag"
REF_STATUS = f"{TABLE_REF}.status_cd"
REF_CROSS_BORDER = f"{TABLE_REF}.cross_border_flag"

#: The one column whose ``logical_representation`` is seeded as a GOVERNED decision — the reason a
#: SUM over it reaches ``external_type_required=False`` and the resolved type ``"numeric"``.
GOVERNED_TYPE_REFS: tuple[str, ...] = (REF_AMT,)

#: What the `hdfc-local` environment is DECLARED to run (spec §0). Real values are Task 0's to
#: capture from the cluster; what every test below needs is that an inventory carries a complete,
#: non-blank set, because §7's generated project pins its dependencies from exactly these.
ENGINE_VERSIONS = EngineVersions(
    hive="3.1.3", spark="3.5.1", metastore="3.1.3", python="3.11.9", java="11.0.22",
    pyspark="3.5.1", kedro="0.19.9", kedro_datasets="4.1.0")

#: The three worked features of the slice (spec "First-slice bounds").
FEATURE_NAMES: tuple[str, ...] = (
    "total_debit_amount_30d",
    "distinct_merchant_count_90d",
    "cross_border_value_ratio_90d",
)

_TIMEZONE = "Asia/Kolkata"
_DECIMAL = DecimalPolicy(precision=38, scale=6, rounding=RoundingMode.HALF_EVEN,
                         overflow=OverflowBehavior.ERROR)
#: The ratio's own policy. ``half_up``, not ``half_even``: Spark's decimal division rounds HALF_UP
#: at the result scale before any explicit rounding call runs, so a declared ``half_even`` on a
#: ratio is unenforceable and ``resolve_physical_type`` REFUSES it (DEFERRED-WORK A.28). The worked
#: feature declares the mode the engine actually applies.
_RATIO_DECIMAL = DecimalPolicy(precision=38, scale=6, rounding=RoundingMode.HALF_UP,
                               overflow=OverflowBehavior.ERROR)
_GRAIN = Grain(entity="customer", keys=(REF_CIF,))


# ── the governed catalog ─────────────────────────────────────────────────────────────────────────

def seed_materialize_catalog(db, *, source: str = SOURCE) -> None:
    """Build the governed catalog the three worked features author over (module docstring)."""
    rows = [
        CanonicalRow(source, TABLE, "txn_amt", "numeric"),
        CanonicalRow(source, TABLE, "txn_dt", "date", as_of=True),
        CanonicalRow(source, TABLE, "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(source, TABLE, "merchant_id", "text"),
        CanonicalRow(source, TABLE, "dr_cr_flag", "text"),
        CanonicalRow(source, TABLE, "status_cd", "text"),
        CanonicalRow(source, TABLE, "cross_border_flag", "boolean"),
    ]
    build_graph(db, source, rows)

    # The governed FACT links (grain / availability): the provenance read_column_facts derives
    # "governed" authority from for the two SPECIALIZED_FACT fields.
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id = 'ovf_evt_grain' "
        "WHERE catalog_source = %s AND object_ref = %s",
        (source, f"public.{TABLE}.cif_id"))
    db.execute(
        "UPDATE graph_node SET availability_fact_event_id = 'ovf_evt_asof' "
        "WHERE catalog_source = %s AND object_ref = %s",
        (source, f"public.{TABLE}.txn_dt"))

    governed = [f"{source}::public.{TABLE}.txn_amt"]
    for ref in governed:
        _attest(db, ref, "logical_representation", "numeric")
    resolve_and_project(db, source=source, logical_refs=governed)

    # read_operational_value fails CLOSED on a lagged/degraded projection (GATE 3), so the drift
    # watermark must say the projection is healthy or EVERY governed read degrades.
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', 0) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now()", (source,))


def _attest(db, logical_ref: str, field_name: str, value: str) -> None:
    record_field_evidence(
        db, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="materialize-fixture", source_snapshot_id="materialize-fixture-snap",
        input_hash=field_input_hash(
            logical_ref=logical_ref, field_name=field_name, material=value))


# ── the hand-authored formulas ───────────────────────────────────────────────────────────────────

def _window(length: int) -> WindowPolicy:
    """A trailing [start, end) window on the governed as-of column. ``unit`` is a ``WindowUnit``."""
    return WindowPolicy(
        event_time_ref=REF_DT, basis=WindowBasis.TRAILING, length=length, unit=WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE, end_inclusive=Inclusivity.EXCLUSIVE,
        timezone=_TIMEZONE, empty_window=EmptyWindowResult.NULL, null_input=NullInput.IGNORE)


def _posted_debit_filter() -> FilterNode:
    """``dr_cr_flag = 'D' AND status_cd = 'posted'``.

    ``FilterBool`` carries ``children`` (not ``operands``) and ``FilterPredicate``'s ONLY column
    reference is ``left`` — there is no ``right_ref`` field, so the right-hand side is a
    ``TypedLiteral`` (``type=``, not ``literal_type=``). ``kind`` is ``init=False`` on both."""
    return FilterBool(op=FilterBoolOp.AND, children=(
        FilterPredicate(op=FilterPredicateOp.EQUAL, left=REF_DR_CR,
                        right_literal=TypedLiteral(type=LiteralType.STRING, value="D")),
        FilterPredicate(op=FilterPredicateOp.EQUAL, left=REF_STATUS,
                        right_literal=TypedLiteral(type=LiteralType.STRING, value="posted")),
    ))


def _cross_border_filter() -> FilterNode:
    return FilterPredicate(op=FilterPredicateOp.EQUAL, left=REF_CROSS_BORDER,
                           right_literal=TypedLiteral(type=LiteralType.BOOLEAN, value="true"))


def _formula(body, output: FormulaOutputPolicyV1,
             decimal: DecimalPolicy = _DECIMAL) -> TypedFormulaV1:
    return TypedFormulaV1(
        formula_schema_version=FORMULA_SCHEMA_VERSION,
        operation_grammar_version=OPERATION_GRAMMAR_VERSION,
        output_policy_version=OUTPUT_POLICY_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
        grain=_GRAIN, body=body, parameters=(), decimal=decimal, output=output)


def _total_debit_amount_30d() -> TypedFormulaV1:
    """SUM of debit amounts over a trailing 30 days.

    VERIFIED output: ``NON_ADDITIVE``. A plain SUM is additive only with a governed-ADDITIVE operand
    AND ``path_additive``; neither is proven, and ``resolve_formula_output_policy`` supplies no
    proof at all. Claiming ADDITIVE here would make the fixture a forgery Gate 1 rejects."""
    return _formula(
        UnaryBody(expr=AggregateExpression(
            aggregation=AggregateFunction.SUM, operand=REF_AMT,
            source_relation=SourceRelation(table_ref=TABLE_REF),
            filter=_posted_debit_filter(), window=_window(30))),
        FormulaOutputPolicyV1(
            output_type="numeric", unit=None, currency=None,
            output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False))


def _distinct_merchant_count_90d() -> TypedFormulaV1:
    """COUNT DISTINCT merchants over a trailing 90 days.

    VERIFIED output: dimensionless ``"integer"``, ``NON_ADDITIVE`` (additive only across proven
    ``disjoint_distinct_values`` — the §D correction to ``b_output_policy``'s additive claim)."""
    return _formula(
        UnaryBody(expr=AggregateExpression(
            aggregation=AggregateFunction.COUNT_DISTINCT, operand=REF_MERCHANT,
            source_relation=SourceRelation(table_ref=TABLE_REF),
            filter=None, window=_window(90))),
        FormulaOutputPolicyV1(
            output_type="integer", unit=None, currency=None,
            output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False))


def _cross_border_value_ratio_90d() -> TypedFormulaV1:
    """Cross-border value as a share of total value over a trailing 90 days.

    ``zero_denominator=ZeroDenominator.NULL`` — a customer with no transactions gets NULL, never a
    fabricated 0. VERIFIED output: ``"decimal"``, ``NON_ADDITIVE``, unit/currency cancel to None."""
    return _formula(
        RatioBody(
            numerator=AggregateExpression(
                aggregation=AggregateFunction.SUM, operand=REF_AMT,
                source_relation=SourceRelation(table_ref=TABLE_REF),
                filter=_cross_border_filter(), window=_window(90)),
            denominator=AggregateExpression(
                aggregation=AggregateFunction.SUM, operand=REF_AMT,
                source_relation=SourceRelation(table_ref=TABLE_REF),
                filter=None, window=_window(90)),
            zero_denominator=ZeroDenominator.NULL),
        FormulaOutputPolicyV1(
            output_type="decimal", unit=None, currency=None,
            output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False),
        decimal=_RATIO_DECIMAL)


_FORMULAS = {
    "total_debit_amount_30d": _total_debit_amount_30d,
    "distinct_merchant_count_90d": _distinct_merchant_count_90d,
    "cross_border_value_ratio_90d": _cross_border_value_ratio_90d,
}


def authored_formula(name: str) -> TypedFormulaV1:
    """The hand-authored ``TypedFormulaV1`` for one worked feature."""
    return _FORMULAS[name]()


# ── the raw proposals that produce them through the REAL authoring path ──────────────────────────

def _raw_window(length: int) -> dict[str, Any]:
    return {"event_time_ref": REF_DT, "basis": "trailing", "length": length, "unit": "day",
            "start_inclusive": "inclusive", "end_inclusive": "exclusive",
            "timezone": _TIMEZONE, "empty_window": "null", "null_input": "ignore"}


def _raw_expr(aggregation: str, operand: str | None, *, length: int,
              filter_node: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"aggregation": aggregation, "operand": operand,
            "source_relation": {"table_ref": TABLE_REF}, "filter": filter_node,
            "window": _raw_window(length)}


def _raw_equal(left: str, literal_type: str, value: str) -> dict[str, Any]:
    return {"kind": "predicate", "op": "equal", "left": left,
            "right_literal": {"type": literal_type, "value": value}}


def _raw_proposal(body: dict[str, Any], *, rounding: str = "half_even") -> dict[str, Any]:
    return {"formula_schema_version": FORMULA_SCHEMA_VERSION,
            "operation_grammar_version": OPERATION_GRAMMAR_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "grain": {"entity": "customer", "keys": [REF_CIF]},
            "body": body,
            "parameters": [],
            "decimal": {"precision": 38, "scale": 6, "rounding": rounding,
                        "overflow": "error"},
            # The ADVISORY expected_output is deliberately absent: it is never identity-bearing, and
            # asserting one would put the run on the §F expectation axis for no fixture value.
            "expected_output": None}


_RAW_PROPOSALS: dict[str, dict[str, Any]] = {
    "total_debit_amount_30d": _raw_proposal({
        "final_operation": "identity",
        "expr": _raw_expr("sum", REF_AMT, length=30, filter_node={
            "kind": "bool", "op": "and", "children": [
                _raw_equal(REF_DR_CR, "string", "D"),
                _raw_equal(REF_STATUS, "string", "posted"),
            ]})}),
    "distinct_merchant_count_90d": _raw_proposal({
        "final_operation": "identity",
        "expr": _raw_expr("count_distinct", REF_MERCHANT, length=90)}),
    "cross_border_value_ratio_90d": _raw_proposal({
        "final_operation": "ratio",
        "numerator": _raw_expr("sum", REF_AMT, length=90,
                               filter_node=_raw_equal(REF_CROSS_BORDER, "boolean", "true")),
        "denominator": _raw_expr("sum", REF_AMT, length=90),
        "zero_denominator": "null"}, rounding="half_up"),
}


def raw_proposal(name: str) -> dict[str, Any]:
    """The raw JSON proposal a scripted author emits to produce :func:`authored_formula`."""
    return copy.deepcopy(_RAW_PROPOSALS[name])


#: A body naming an operation the v1 grammar covers but whose OPERAND is not a column ref — the
#: shape gate rejects it, so the run folds to REJECTED and still writes a COMPLETED terminal event.
def rejected_raw_proposal() -> dict[str, Any]:
    """A proposal that is genuinely INVALID (a table ref where a column ref is required), so the
    real orchestrator folds it to ``REJECTED`` — which STILL writes a ``COMPLETED`` terminal."""
    return _raw_proposal({"final_operation": "identity",
                          "expr": _raw_expr("sum", TABLE_REF, length=30)})


# ── intents ──────────────────────────────────────────────────────────────────────────────────────

_HYPOTHESES = {
    "total_debit_amount_30d":
        "Debit turnover over the last 30 days predicts short-term credit risk.",
    "distinct_merchant_count_90d":
        "Merchant breadth over 90 days separates transactors from revolvers.",
    "cross_border_value_ratio_90d":
        "A rising cross-border value share flags remittance-driven accounts.",
}


def intent_for(name: str, *, hypothesis: str | None = None,
               recipe_authoring_context: dict[str, Any] | None = None) -> AuthoringIntent:
    """The :class:`AuthoringIntent` a worked feature is authored under.

    ``name`` IS the feature name — ``AuthoringResult`` carries none (reference §6). ``hypothesis``
    is overridable so a test can build a LOOK-ALIKE intent that hashes differently, which is the
    only thing separating ``INTENT_HASH_MISMATCH`` from a pass."""
    return AuthoringIntent(
        name=name,
        hypothesis=hypothesis if hypothesis is not None else _HYPOTHESES.get(name, name),
        target_entity="customer",
        target_grain_keys=(REF_CIF,),
        recipe_authoring_context=recipe_authoring_context)

"""Factory constructors for TypedFormulaV1 canonicalization tests.

The schema dataclasses are frozen AND slotted, so variants are built through
these constructors and ``dataclasses.replace`` — never ``__dict__``.

Base fixture: a customer-grain RATIO of two SUMs (numerator filtered to debit
transactions) over a trailing-90-day window on
``ftr::public.comp_financial_tran_repos_dly.tran_amt_aed``.
"""
from __future__ import annotations

from featuregen.formula.schema import (
    AdditivityClass,
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    EmptyWindowResult,
    FilterNode,
    FilterPredicate,
    FilterPredicateOp,
    FormulaBody,
    FormulaOutputPolicyV1,
    Grain,
    Inclusivity,
    LiteralType,
    NullInput,
    OverflowBehavior,
    ParamClass,
    ParameterDecl,
    RatioBody,
    RoundingMode,
    SourceRelation,
    TypedFormulaV1,
    TypedLiteral,
    WindowBasis,
    WindowPolicy,
    WindowUnit,
    ZeroDenominator,
)

TABLE_REF = "ftr::public.comp_financial_tran_repos_dly"
AMOUNT_REF = f"{TABLE_REF}.tran_amt_aed"
EVENT_TIME_REF = f"{TABLE_REF}.tran_dt"
CIF_KEY_REF = f"{TABLE_REF}.cif_id"
ACCOUNT_KEY_REF = f"{TABLE_REF}.acct_id"
TRAN_TYPE_REF = f"{TABLE_REF}.tran_type_cd"
CHANNEL_REF = f"{TABLE_REF}.channel_cd"
STATUS_REF = f"{TABLE_REF}.status_cd"


def trailing_90d_window(event_time_ref: str = EVENT_TIME_REF) -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=event_time_ref,
        basis=WindowBasis.TRAILING,
        length=90,
        unit=WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE,
        end_inclusive=Inclusivity.EXCLUSIVE,
        timezone="Asia/Dubai",
        empty_window=EmptyWindowResult.NULL,
        null_input=NullInput.IGNORE,
    )


def equal_predicate(left: str = TRAN_TYPE_REF, value: str = "debit") -> FilterPredicate:
    return FilterPredicate(
        op=FilterPredicateOp.EQUAL,
        left=left,
        right_literal=TypedLiteral(type=LiteralType.STRING, value=value),
    )


def in_predicate(
    left: str = CHANNEL_REF, values: tuple[str, ...] = ("atm", "branch")
) -> FilterPredicate:
    return FilterPredicate(
        op=FilterPredicateOp.IN,
        left=left,
        right_set=tuple(TypedLiteral(type=LiteralType.STRING, value=v) for v in values),
    )


def not_null_predicate(left: str = STATUS_REF) -> FilterPredicate:
    return FilterPredicate(op=FilterPredicateOp.IS_NOT_NULL, left=left)


def sum_expression(
    operand: str = AMOUNT_REF,
    *,
    table_ref: str = TABLE_REF,
    filter_node: FilterNode | None = None,
    window: WindowPolicy | None = None,
) -> AggregateExpression:
    return AggregateExpression(
        aggregation=AggregateFunction.SUM,
        operand=operand,
        source_relation=SourceRelation(table_ref=table_ref),
        filter=filter_node,
        window=window if window is not None else trailing_90d_window(),
    )


def customer_grain(
    keys: tuple[str, ...] = (CIF_KEY_REF, ACCOUNT_KEY_REF), entity: str = "customer"
) -> Grain:
    return Grain(entity=entity, keys=keys)


def ratio_of_sums(
    numerator: AggregateExpression | None = None,
    denominator: AggregateExpression | None = None,
    zero_denominator: ZeroDenominator = ZeroDenominator.NULL,
) -> RatioBody:
    return RatioBody(
        numerator=numerator if numerator is not None else sum_expression(filter_node=equal_predicate()),
        denominator=denominator if denominator is not None else sum_expression(),
        zero_denominator=zero_denominator,
    )


def string_parameter(
    name: str,
    *,
    allowed_set: tuple[str, ...] | None = None,
    classification: str = "internal",
) -> ParameterDecl:
    return ParameterDecl(
        name=name,
        type=LiteralType.STRING,
        param_class=ParamClass.SEMANTIC,
        classification=classification,
        nullable=False,
        allowed_set=allowed_set,
        allowed_min=None,
        allowed_max=None,
    )


def default_decimal() -> DecimalPolicy:
    return DecimalPolicy(
        precision=38,
        scale=6,
        rounding=RoundingMode.HALF_EVEN,
        overflow=OverflowBehavior.ERROR,
    )


def default_output() -> FormulaOutputPolicyV1:
    return FormulaOutputPolicyV1(
        output_type="decimal",
        unit=None,
        currency=None,
        output_additivity=AdditivityClass.NON_ADDITIVE,
        external_type_required=False,
    )


def base_formula(
    *,
    formula_schema_version: int = 1,
    operation_grammar_version: int = 1,
    output_policy_version: int = 1,
    canonicalization_version: int = 1,
    grain: Grain | None = None,
    body: FormulaBody | None = None,
    parameters: tuple[ParameterDecl, ...] = (),
    decimal: DecimalPolicy | None = None,
    output: FormulaOutputPolicyV1 | None = None,
) -> TypedFormulaV1:
    return TypedFormulaV1(
        formula_schema_version=formula_schema_version,
        operation_grammar_version=operation_grammar_version,
        output_policy_version=output_policy_version,
        canonicalization_version=canonicalization_version,
        grain=grain if grain is not None else customer_grain(),
        body=body if body is not None else ratio_of_sums(),
        parameters=parameters,
        decimal=decimal if decimal is not None else default_decimal(),
        output=output if output is not None else default_output(),
    )

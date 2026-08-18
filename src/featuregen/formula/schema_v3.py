"""C-A2/C-A3 — Formula wire schema **v3**: the semantic row selection, and nothing else.

**V3 is not a new LANGUAGE.** It is the Formula-V2 language at wire version 3. The one addition is
:class:`SemanticRowSelectionV1`, which lets a formula say WHAT it wants (``debit``) while the policy
realization says HOW this source spells it (``D``). Before it, ``posted_debit_amount`` could only
express direction as a physical ``filter`` predicate — which the recipe expectation validator
rejects as ``UNAUTHORED_FILTER`` — so a deterministic author had to infer ``debit`` from the recipe
NAME or its prose. That is the gap this schema closes.

**Why a whole new type family rather than a field on V2.** ``canonical_v2._plain_v2`` serializes
EVERY dataclass field (``for f in sorted(fields(value))``), and ``test_canonical_v2`` pins that
behaviour: *"a field added later is hash-bearing automatically"*. Adding ``row_selections`` to
``AggregateExpressionV2`` would therefore have re-hashed every stored V2 artifact, even set to
``None``. V2's dataclasses are untouched here; ``test_canonical_v2`` staying green is the proof.

**What V3 REUSES, deliberately.** ``WindowPolicyV2``, ``AuthorityRefsV2`` and the shared v1 leaves
(``Grain``, ``DecimalPolicy``, ``ParameterDecl``, ``SourceRelation``, ``FilterNode``) are imported
unchanged. Duplicating a type whose shape is identical would create two definitions of one concept
and a second place for them to drift. A V3 sibling arrives only when a shape genuinely differs —
which is exactly why ``AggregateExpressionV3`` exists and ``WindowPolicyV3`` does not.

**Policy references keep ONE owner** (``AuthorityRefsV2``). A selection carries no ``policy_ref``:
two places naming a direction policy is two places to disagree about which one executes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from featuregen.formula.schema_leaves import (
    DecimalPolicy,
    FilterNode,
    Grain,
    LogicalRef,
    ParameterDecl,
    SchemaError,
    SourceRelation,
)
from featuregen.formula.schema_v2 import (
    OPERATION_GRAMMAR_VERSION_V2,
    AggregateFunctionV2,
    AuthorityRefsV2,
    FinalOperationV2,
    WindowPolicyV2,
    _check_expression_v2,
)

__all__ = [
    "CANONICALIZATION_VERSION_V3",
    "FORMULA_SCHEMA_VERSION_V3",
    "OPERATION_GRAMMAR_VERSION_V3",
    "SELECTION_TOKENS",
    "AggregateExpressionV3",
    "CompositeBodyV3",
    "DiffBodyV3",
    "FormulaBodyV3",
    "RatioBodyV3",
    "SelectionKind",
    "SemanticRowSelectionV1",
    "SignedTermV3",
    "TypedFormulaProposalV3",
    "UnaryBodyV3",
    "body_expressions_v3",
    "validate_semantics_v3",
]

#: The wire pin. The LANGUAGE is still V2; only the schema version moves.
FORMULA_SCHEMA_VERSION_V3 = 3
#: Unchanged — v3 adds no operation. Bound to v2's constant so a grammar increment moves both.
OPERATION_GRAMMAR_VERSION_V3 = OPERATION_GRAMMAR_VERSION_V2
#: v3's canonical projection has its OWN lineage: it starts at 1 and moves when v3's bytes move,
#: independently of v2's, which is frozen.
CANONICALIZATION_VERSION_V3 = 1

#: Refusal code: a row selection and a filter both govern row inclusion on one expression.
SELECTION_FILTER_CONFLICT = "SELECTION_FILTER_CONFLICT"


class SelectionKind(StrEnum):
    """What a selection selects ON. Closed: an unknown kind has no realization to resolve it."""

    TRANSACTION_DIRECTION = "transaction_direction"
    ELIGIBILITY = "eligibility"


#: The CLOSED token vocabulary per kind. This is what makes "a physical literal refuses"
#: mechanically decidable: nothing at the schema layer can look at ``D`` and know it is physical,
#: but it can know ``D`` is not a member of ``{debit, credit}``. The banking meaning of each token
#: is the realization's job (C-C8); the schema only owns membership.
SELECTION_TOKENS: dict[SelectionKind, frozenset[str]] = {
    SelectionKind.TRANSACTION_DIRECTION: frozenset({"debit", "credit"}),
    SelectionKind.ELIGIBILITY: frozenset({"eligible"}),
}

#: Which ``AuthorityRefsV2`` field must be present for a selection of each kind. A selection
#: declares INTENT; without the matching policy reference nothing can resolve it to columns and
#: values, so the pair is required together or the formula is refused.
_REQUIRED_POLICY_REF: dict[SelectionKind, str] = {
    SelectionKind.TRANSACTION_DIRECTION: "direction_policy_ref",
    SelectionKind.ELIGIBILITY: "status_policy_ref",
}


@dataclass(frozen=True, slots=True)
class SemanticRowSelectionV1:
    """One semantic row selection: *this expression wants DEBIT rows*.

    ``semantic_value`` is a SEMANTIC token from :data:`SELECTION_TOKENS`, never the source's
    physical literal. ``D`` belongs in the policy realization and the IR operator it renders — the
    formula must stay portable across a source that spells debit ``D``, ``DR`` or ``-1``.

    Carries no ``policy_ref``: ``AuthorityRefsV2`` is the single owner of policy references.
    """

    kind: SelectionKind
    role: str
    semantic_value: str


@dataclass(frozen=True, slots=True)
class AggregateExpressionV3:
    """V2's expression plus :attr:`row_selections`.

    A TUPLE, unique by ``(kind, role)`` — one selection cannot be assumed to cover every future
    expression, and a ratio's numerator and denominator may legitimately select differently.
    """

    aggregation: AggregateFunctionV2
    operand: LogicalRef | None
    source_relation: SourceRelation
    filter: FilterNode | None
    window: WindowPolicyV2
    aggregation_argument: float | None = None
    second_operand: LogicalRef | None = None
    authority_refs: AuthorityRefsV2 | None = None
    row_selections: tuple[SemanticRowSelectionV1, ...] = ()


@dataclass(frozen=True, slots=True)
class UnaryBodyV3:
    expr: AggregateExpressionV3
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.IDENTITY, init=False)


@dataclass(frozen=True, slots=True)
class RatioBodyV3:
    numerator: AggregateExpressionV3
    denominator: AggregateExpressionV3
    zero_denominator: str
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.RATIO, init=False)


@dataclass(frozen=True, slots=True)
class DiffBodyV3:
    minuend: AggregateExpressionV3
    subtrahend: AggregateExpressionV3
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.DIFFERENCE, init=False)


@dataclass(frozen=True, slots=True)
class SignedTermV3:
    name: str
    sign: int
    expr: AggregateExpressionV3


@dataclass(frozen=True, slots=True)
class CompositeBodyV3:
    terms: tuple[SignedTermV3, ...]
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.SIGNED_SUM, init=False)


FormulaBodyV3 = UnaryBodyV3 | RatioBodyV3 | DiffBodyV3 | CompositeBodyV3


@dataclass(frozen=True, slots=True)
class TypedFormulaProposalV3:
    """The v3 proposal. Version fields are DATA, validated against the v3 pins — never inferred."""

    formula_schema_version: int
    operation_grammar_version: int
    canonicalization_version: int
    grain: Grain
    body: FormulaBodyV3
    parameters: tuple[ParameterDecl, ...]
    decimal: DecimalPolicy
    expected_output: object | None
    allocation_policy_ref: str = ""


def body_expressions_v3(body: FormulaBodyV3) -> tuple[AggregateExpressionV3, ...]:
    if isinstance(body, UnaryBodyV3):
        return (body.expr,)
    if isinstance(body, RatioBodyV3):
        return (body.numerator, body.denominator)
    if isinstance(body, DiffBodyV3):
        return (body.minuend, body.subtrahend)
    return tuple(term.expr for term in body.terms)


def _as_v2_expression(expr: AggregateExpressionV3):
    """The v2 view of a v3 expression, for reusing ``_check_expression_v2`` verbatim.

    Every v2 rule (operand/aggregation pairing, containment, parameter binding, window shape) is
    IDENTICAL in v3 — re-implementing them here would be a second copy free to drift from the one
    the v2 suite exercises. Only the selection rules below are v3's own.
    """
    from featuregen.formula.schema_v2 import AggregateExpressionV2
    return AggregateExpressionV2(
        aggregation=expr.aggregation, operand=expr.operand,
        source_relation=expr.source_relation, filter=expr.filter, window=expr.window,
        aggregation_argument=expr.aggregation_argument, second_operand=expr.second_operand,
        authority_refs=expr.authority_refs)


def _check_selections(expr: AggregateExpressionV3, path: str) -> None:
    seen: set[tuple[SelectionKind, str]] = set()
    for index, sel in enumerate(expr.row_selections):
        where = f"{path}.row_selections[{index}]"
        if not isinstance(sel.kind, SelectionKind):
            raise SchemaError(f"{where}: unknown selection kind {sel.kind!r}")
        if not sel.role or sel.role != sel.role.strip():
            raise SchemaError(f"{where}: role must be non-empty and unpadded")
        tokens = SELECTION_TOKENS[sel.kind]
        if sel.semantic_value not in tokens:
            raise SchemaError(
                f"{where}: semantic_value {sel.semantic_value!r} is not one of "
                f"{sorted(tokens)} — a selection carries a SEMANTIC token, never the source's "
                f"physical literal (that belongs to the policy realization)")
        key = (sel.kind, sel.role)
        if key in seen:
            raise SchemaError(
                f"{where}: duplicate selection for (kind={sel.kind.value}, role={sel.role!r}) — "
                f"one role is governed once")
        seen.add(key)

        required = _REQUIRED_POLICY_REF[sel.kind]
        ref = getattr(expr.authority_refs, required, "") if expr.authority_refs else ""
        if not ref.strip():
            raise SchemaError(
                f"{where}: a {sel.kind.value} selection requires authority_refs.{required} — the "
                f"selection declares intent and the policy reference is what resolves it")

    if expr.row_selections and expr.filter is not None:
        raise SchemaError(
            f"{path}: {SELECTION_FILTER_CONFLICT} — this expression carries both a row selection "
            f"and a filter. The schema cannot prove WHICH column a filter touches, so a filter "
            f"beside a selection is refused rather than risk applying direction twice or "
            f"contradicting it")


def validate_semantics_v3(p: TypedFormulaProposalV3) -> None:
    """The v3 semantic gate: v2's rules verbatim, plus the selection rules."""
    if p.formula_schema_version != FORMULA_SCHEMA_VERSION_V3:
        raise SchemaError(
            f"formula_schema_version: expected {FORMULA_SCHEMA_VERSION_V3}, "
            f"got {p.formula_schema_version}")
    if p.operation_grammar_version != OPERATION_GRAMMAR_VERSION_V3:
        raise SchemaError(
            f"operation_grammar_version: expected {OPERATION_GRAMMAR_VERSION_V3}, "
            f"got {p.operation_grammar_version}")
    if p.canonicalization_version != CANONICALIZATION_VERSION_V3:
        raise SchemaError(
            f"canonicalization_version: expected {CANONICALIZATION_VERSION_V3}, "
            f"got {p.canonicalization_version}")

    params = {decl.name: decl for decl in p.parameters}
    for index, expr in enumerate(body_expressions_v3(p.body)):
        path = f"body.expr[{index}]"
        _check_expression_v2(_as_v2_expression(expr), path, params)
        _check_selections(expr, path)


def is_v3_body(body: object) -> bool:
    """Whether ``body`` is one of v3's four body shapes.

    Exists so ``body_expressions_v2`` can dispatch without importing four names, and so the
    "is this v3" question has ONE answer rather than an isinstance tuple repeated at call sites.
    """
    return isinstance(body, UnaryBodyV3 | RatioBodyV3 | DiffBodyV3 | CompositeBodyV3)

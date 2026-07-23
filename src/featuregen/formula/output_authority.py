"""Child-1 §C/§D — the CORE authority logic: corrected additivity + the operation-specific
output-authority resolver over C1 governed facts.

TWO responsibilities, both offline (no execution, no Spark, no durable artifact):

* :func:`formula_additivity` (§D) — the CORRECTED additivity rule. ``b_output_policy`` wrongly marks
  ``count_distinct`` additive (``b_output_policy.py:140``); that module is out of scope and is NOT
  edited. This computes additivity from the body's operation + a ``PartitionProof`` (disjointness /
  path additivity CANNOT be proven from the body alone), returning **NON_ADDITIVE for
  ``COUNT_DISTINCT``** by default.
* :func:`resolve_formula_output_policy` (§C) — resolves the AUTHORITATIVE
  :class:`~featuregen.formula.schema.FormulaOutputPolicyV1` from the C1 governed facts carried in
  ``per_expr_facts`` (already-read :class:`OperationalValue` bundles). Only the fields the operation
  actually needs are REQUIRED; ``unit``/``currency`` are C1 HINTS and never force ``NEEDS_AUTHORITY``.

**The proposal's advisory ``expected_output`` is NEVER consulted here.** The authoritative policy is a
pure function of the governed C1 facts + the body's operation; ``expected_output`` is the LLM's
advisory guess and comparing/logging it is a SEPARATE concern (§F expectation axis), out of scope for
this resolver.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.formula.schema import (
    AdditivityClass,
    AggregateFunction,
    DiffBody,
    FormulaBody,
    FormulaOutputPolicyV1,
    RatioBody,
    TypedFormulaProposalV1,
    UnaryBody,
)
from featuregen.overlay.upload.operational_facts import OperationalValue

__all__ = [
    "ExprFacts",
    "ExternalRequirement",
    "InvalidOutput",
    "NeedsAuthority",
    "PartitionProof",
    "formula_additivity",
    "resolve_formula_output_policy",
]

# ── C1 statuses that FAIL CLOSED on a REQUIRED field (§C: fork/hash_mismatch/projection_unavailable
# → NEEDS_AUTHORITY). A HINT-only field (``not_operational``) is never in this set. ──────────────────
_HARD_FAIL_STATUSES: frozenset[str] = frozenset(
    {"fork", "hash_mismatch", "projection_unavailable"}
)
# The C1 status that means "a governed decision field is load-bearing and verified".
_RESOLVED = "resolved"

# Base logical types B treats as numeric (base type only, ignoring any ``(p,s)``). Kept LOCAL +
# auditable — mirrors ``b_output_policy._NUMERIC_LOGICAL_TYPES`` (never imported from a module this
# one must not couple to).
_NUMERIC_LOGICAL_TYPES: frozenset[str] = frozenset({
    "numeric", "integer", "bigint", "int", "int4", "int8", "smallint",
    "float", "double", "double precision", "decimal", "real", "money",
})

# The dimensionless output type of a count (§C: COUNT_* → dimensionless).
_COUNT_OUTPUT_TYPE = "integer"
_RATIO_OUTPUT_TYPE = "decimal"
_UNKNOWN_TYPE = "unknown"


@dataclass(frozen=True, slots=True)
class ExprFacts:
    """The ALREADY-READ C1 :class:`OperationalValue` bundle for ONE aggregate expression's operand.

    Each axis is the operational read of a single ``(operand_logical_ref, field_name)``; ``None`` when
    the caller did not read that field. ``output_type`` is the operand's governed numeric type
    (C1 ``logical_representation``); ``additivity`` its governed additivity (C1 ``additivity``);
    ``unit`` / ``currency`` are C1 HINTS (``not_operational``), carried but never authority-forcing."""

    output_type: OperationalValue | None = None
    additivity: OperationalValue | None = None
    unit: OperationalValue | None = None
    currency: OperationalValue | None = None


@dataclass(frozen=True, slots=True)
class PartitionProof:
    """The disjointness / path-additivity evidence additivity CANNOT derive from the body alone (§D).

    * ``disjoint_row_partitions`` — the grain partitions rows disjointly (COUNT_ROWS/COUNT_NON_NULL are
      additive ONLY then);
    * ``disjoint_distinct_values`` — the distinct-value sets across partitions are proven disjoint
      (the ONLY case a COUNT_DISTINCT is additive);
    * ``path_additive`` — the governed input's aggregation path is itself additive (gates a SUM's
      additive claim)."""

    disjoint_row_partitions: bool = False
    disjoint_distinct_values: bool = False
    path_additive: bool = False


@dataclass(frozen=True, slots=True)
class NeedsAuthority:
    """Fail-closed: a REQUIRED C1 field was fork/hash_mismatch/projection_unavailable (§C)."""

    reason: str


@dataclass(frozen=True, slots=True)
class ExternalRequirement:
    """A REQUIRED field is unavailable and must be provisioned externally (e.g. a non-cancelling
    RATIO unit → ``ExternalRequirement("UNIT_PROVISIONING_REQUIRED")``)."""

    requirement: str


@dataclass(frozen=True, slots=True)
class InvalidOutput:
    """The output is structurally impossible from the governed facts (e.g. a DIFFERENCE of exactly
    INCOMPATIBLE units) — an ``INVALID_FORMULA`` disposition, not merely unsupported."""

    reason: str


# ── §D corrected additivity ───────────────────────────────────────────────────────────────────────

def _governed_additivity(ov: OperationalValue | None) -> AdditivityClass | None:
    """The operand's GOVERNED additivity, or ``None`` if it was not resolved as a governed C1 fact.

    Only a ``resolved`` (governed, hash-verified) additivity decision confers an input additivity;
    any other status (hint / no_value / fork / …) yields ``None`` → the caller degrades conservatively.
    """
    if ov is None or ov.status != _RESOLVED:
        return None
    try:
        return AdditivityClass(str(ov.value))
    except ValueError:
        return None


def _expr_facts(per_expr_facts: dict[str, ExprFacts] | None, path: str) -> ExprFacts:
    facts = (per_expr_facts or {}).get(path)
    return facts if facts is not None else ExprFacts()


def formula_additivity(
    body: FormulaBody,
    *,
    per_expr_facts,
    partition_proof: PartitionProof,
) -> AdditivityClass:
    """The corrected output additivity for a formula body (§D). Additivity CANNOT be proven from the
    body alone, so a ``PartitionProof`` supplies the disjointness / path evidence.

    * ``RATIO`` → NON_ADDITIVE; ``DIFFERENCE`` → NON_ADDITIVE (no proven rule applies here);
    * ``COUNT_DISTINCT`` → NON_ADDITIVE unless ``disjoint_distinct_values`` is proven (the BUG fix —
      ``b_output_policy`` wrongly marks it additive);
    * ``COUNT_ROWS`` / ``COUNT_NON_NULL`` → ADDITIVE only across ``disjoint_row_partitions``;
    * ``SUM`` → its GOVERNED input additivity, and ``ADDITIVE`` only when the input is additive AND the
      path is proven additive; a ``SEMI_ADDITIVE`` input stays semi-additive; anything else degrades
      to NON_ADDITIVE (never a silent additive claim)."""
    if isinstance(body, (RatioBody, DiffBody)):
        return AdditivityClass.NON_ADDITIVE

    if not isinstance(body, UnaryBody):  # pragma: no cover - discriminated union is closed
        return AdditivityClass.NON_ADDITIVE

    agg: AggregateFunction = body.expr.aggregation
    if agg is AggregateFunction.COUNT_DISTINCT:
        return (
            AdditivityClass.ADDITIVE
            if partition_proof.disjoint_distinct_values
            else AdditivityClass.NON_ADDITIVE
        )
    if agg in (AggregateFunction.COUNT_ROWS, AggregateFunction.COUNT_NON_NULL):
        return (
            AdditivityClass.ADDITIVE
            if partition_proof.disjoint_row_partitions
            else AdditivityClass.NON_ADDITIVE
        )
    if agg is AggregateFunction.SUM:
        operand_additivity = _governed_additivity(_expr_facts(per_expr_facts, "body.expr").additivity)
        if operand_additivity is AdditivityClass.ADDITIVE and partition_proof.path_additive:
            return AdditivityClass.ADDITIVE
        if operand_additivity is AdditivityClass.SEMI_ADDITIVE:
            return AdditivityClass.SEMI_ADDITIVE
        return AdditivityClass.NON_ADDITIVE
    return AdditivityClass.NON_ADDITIVE  # pragma: no cover - AggregateFunction is closed


# ── §C output-authority resolver over C1 governed facts ─────────────────────────────────────────────

# No partition proof is available at resolve time, so the resolved additivity is the CONSERVATIVE
# (unproven) class; a caller with a real proof calls :func:`formula_additivity` directly.
_NO_PROOF = PartitionProof()

_COUNT_FUNCTIONS = frozenset(
    {AggregateFunction.COUNT_ROWS, AggregateFunction.COUNT_NON_NULL, AggregateFunction.COUNT_DISTINCT}
)


def _is_numeric_logical_type(value: object | None) -> bool:
    base = (str(value) if value is not None else "").lower().split("(")[0].strip()
    return base in _NUMERIC_LOGICAL_TYPES


def _numeric_output_type(ov: OperationalValue | None) -> tuple[str, bool]:
    """``(output_type, external_type_required)`` for an operand's C1 type read (§C SUM/DIFFERENCE).

    A GOVERNED numeric type (``status="resolved"``) clears external validation; an
    ungoverned-but-numeric value still yields the type but must be externally type-validated (matching
    ``b_output_policy``'s conservative degrade); a non-numeric / absent type → ``"unknown"`` +
    ``external_type_required=True`` (never a silent numeric claim)."""
    value = ov.value if ov is not None else None
    governed = ov is not None and ov.status == _RESOLVED
    if value is not None and _is_numeric_logical_type(value):
        return str(value), not governed
    return _UNKNOWN_TYPE, True


def _needs_authority(required: list[OperationalValue | None]) -> NeedsAuthority | None:
    """§C fail-closed: if ANY REQUIRED field's C1 read is fork / hash_mismatch /
    projection_unavailable, no policy can be trusted → :class:`NeedsAuthority` carrying the machine
    reason. ``None`` entries (a field the operation does not require, or was not read) are skipped;
    HINT statuses (``not_operational``) are never in :data:`_HARD_FAIL_STATUSES`, so a hint never
    forces authority."""
    for ov in required:
        if ov is not None and ov.status in _HARD_FAIL_STATUSES:
            return NeedsAuthority(ov.conflict_status or ov.status)
    return None


def _hint_value(ov: OperationalValue | None) -> str | None:
    """The carried HINT value (unit/currency). HINTS never force NEEDS_AUTHORITY (§C); a fail-closed
    read simply carries ``None``."""
    if ov is None or ov.value is None:
        return None
    return str(ov.value)


def resolve_formula_output_policy(
    proposal: TypedFormulaProposalV1,
    *,
    per_expr_facts,
    grain_facts,
    now: datetime,
) -> FormulaOutputPolicyV1 | NeedsAuthority | ExternalRequirement | InvalidOutput:
    """Resolve the AUTHORITATIVE output policy from the C1 governed facts in ``per_expr_facts`` (§C).

    ``per_expr_facts`` maps each expression PATH (``body.expr`` / ``body.numerator`` / … ) to its
    already-read :class:`ExprFacts`; ``grain_facts`` maps each grain-key ``logical_ref`` to its C1
    read. Returns a :class:`FormulaOutputPolicyV1` on success, else the typed non-success arm
    (:class:`NeedsAuthority` / :class:`ExternalRequirement` / :class:`InvalidOutput`). The proposal's
    advisory ``expected_output`` is NEVER read — the policy is a pure function of the governed facts
    and the body's operation."""
    del now  # no freshness read here (the C1 facts were already read under their own gates)
    body: FormulaBody = proposal.body
    if isinstance(body, UnaryBody):
        return _resolve_unary(body, per_expr_facts, grain_facts)
    if isinstance(body, DiffBody):
        return _resolve_difference(body, per_expr_facts)
    if isinstance(body, RatioBody):
        return _resolve_ratio(body, per_expr_facts)
    return InvalidOutput("unknown_body")  # pragma: no cover - FormulaBody is a closed union


def _resolve_unary(
    body: UnaryBody, per_expr_facts, grain_facts
) -> FormulaOutputPolicyV1 | NeedsAuthority | ExternalRequirement | InvalidOutput:
    agg = body.expr.aggregation
    facts = _expr_facts(per_expr_facts, "body.expr")
    additivity = formula_additivity(body, per_expr_facts=per_expr_facts, partition_proof=_NO_PROOF)

    if agg in _COUNT_FUNCTIONS:
        # §C — COUNT_* are DIMENSIONLESS. COUNT_ROWS also needs the grain to be readable; the operand
        # existence/type read is required for COUNT_NON_NULL/COUNT_DISTINCT.
        required = [facts.output_type]
        if agg is AggregateFunction.COUNT_ROWS:
            required = list((grain_facts or {}).values())
        needs = _needs_authority(required)
        if needs is not None:
            return needs
        return FormulaOutputPolicyV1(
            output_type=_COUNT_OUTPUT_TYPE, unit=None, currency=None,
            output_additivity=additivity, external_type_required=False)

    if agg is AggregateFunction.SUM:
        # §C — SUM: numeric type + additivity; unit/currency are HINTS (carried, never blocking).
        needs = _needs_authority([facts.additivity, facts.output_type])
        if needs is not None:
            return needs
        output_type, external_type_required = _numeric_output_type(facts.output_type)
        return FormulaOutputPolicyV1(
            output_type=output_type, unit=_hint_value(facts.unit),
            currency=_hint_value(facts.currency), output_additivity=additivity,
            external_type_required=external_type_required)

    return InvalidOutput("unsupported_aggregation")  # pragma: no cover - AggregateFunction is closed


class _Incompatible:
    """Sentinel: two operand dimensions (unit or currency) that are NOT the same and cannot combine."""


_INCOMPATIBLE = _Incompatible()


def _same_dimension(
    left: OperationalValue | None, right: OperationalValue | None
) -> str | None | _Incompatible:
    """The common unit/currency of two operands, or :data:`_INCOMPATIBLE` if they differ.

    Both HINTS. Equal (incl. both absent → ``None``) → the shared dimension; any difference → the
    sentinel. Dimension HINTS never trigger NEEDS_AUTHORITY; the difference is a structural output
    fact the caller turns into INVALID (DIFFERENCE) or an external requirement (RATIO)."""
    a, b = _hint_value(left), _hint_value(right)
    if a == b:
        return a
    return _INCOMPATIBLE


def _resolve_difference(
    body: DiffBody, per_expr_facts
) -> FormulaOutputPolicyV1 | NeedsAuthority | ExternalRequirement | InvalidOutput:
    """§C DIFFERENCE — numeric both operands + EXACTLY compatible unit/currency; the output carries
    that unit/currency, and INCOMPATIBLE units/currency → INVALID_FORMULA."""
    minu = _expr_facts(per_expr_facts, "body.minuend")
    subt = _expr_facts(per_expr_facts, "body.subtrahend")
    needs = _needs_authority(
        [minu.additivity, minu.output_type, subt.additivity, subt.output_type])
    if needs is not None:
        return needs

    unit = _same_dimension(minu.unit, subt.unit)
    if isinstance(unit, _Incompatible):
        return InvalidOutput("incompatible_unit")
    currency = _same_dimension(minu.currency, subt.currency)
    if isinstance(currency, _Incompatible):
        return InvalidOutput("incompatible_currency")

    output_type, external_type_required = _numeric_output_type(minu.output_type)
    additivity = formula_additivity(body, per_expr_facts=per_expr_facts, partition_proof=_NO_PROOF)
    return FormulaOutputPolicyV1(
        output_type=output_type, unit=unit, currency=currency, output_additivity=additivity,
        external_type_required=external_type_required)


def _resolve_ratio(
    body: RatioBody, per_expr_facts
) -> FormulaOutputPolicyV1 | NeedsAuthority | ExternalRequirement | InvalidOutput:
    """§C RATIO — numeric both operands + units/currency CANCEL → dimensionless. A mismatch that
    cannot cancel is a TYPED external requirement (``UNIT_PROVISIONING_REQUIRED`` /
    ``CURRENCY_PROVISIONING_REQUIRED``), not an indiscriminate NEEDS_AUTHORITY."""
    num = _expr_facts(per_expr_facts, "body.numerator")
    den = _expr_facts(per_expr_facts, "body.denominator")
    needs = _needs_authority(
        [num.additivity, num.output_type, den.additivity, den.output_type])
    if needs is not None:
        return needs

    if isinstance(_same_dimension(num.unit, den.unit), _Incompatible):
        return ExternalRequirement("UNIT_PROVISIONING_REQUIRED")
    if isinstance(_same_dimension(num.currency, den.currency), _Incompatible):
        return ExternalRequirement("CURRENCY_PROVISIONING_REQUIRED")

    additivity = formula_additivity(body, per_expr_facts=per_expr_facts, partition_proof=_NO_PROOF)
    return FormulaOutputPolicyV1(
        output_type=_RATIO_OUTPUT_TYPE, unit=None, currency=None, output_additivity=additivity,
        external_type_required=False)

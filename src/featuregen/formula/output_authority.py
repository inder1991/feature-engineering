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
    AggregateExpression,
    AggregateFunction,
    DiffBody,
    FormulaBody,
    RatioBody,
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

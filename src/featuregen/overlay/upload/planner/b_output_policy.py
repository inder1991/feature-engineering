"""Phase 3C.2b-i-B · Task 8 — governed output-field derivation (fail-closed).

Derives an operand's ``output_type`` / ``external_type_required`` / ``output_additivity`` from
GOVERNED reads — never from the flat display column alone. This module NEVER hard-rejects (the
op-level rejects live in ``b_operation``): "fail-closed" here means CONSERVATIVE field values — an
ungoverned/unreadable type degrades to ``output_type="unknown"`` + ``external_type_required=True``
(the honest NEEDS_EXTERNAL_VALIDATION the spike hand-set), and an ambiguous additivity degrades to
``unknown``. It never claims authority it did not read.

Governed operational-type read (the same authority gate the single-source gauntlet uses):

* ``read_column_facts(conn, logical_ref, "logical_representation")`` — the OperationalColumnFacts
  adapter, whose ``authority == "governed"`` already encodes ``is_feature_eligible`` for the
  decision-governed ``logical_representation`` field; its ``value`` is the flat OPERATIONAL
  ``graph_node.data_type`` (never the decision's load-bearing value, which is a hash);
* ``is_feature_eligible(conn, logical_ref, "logical_representation")`` — the decision-log +
  ``load_bearing_value_hash`` gate, consulted explicitly (belt-and-suspenders);
* ``active_disqualifiers_for(...)`` returning ``CONFIRMATION_PENDING_REVALIDATION`` — a human-confirmed
  value invalidated by a later material change; treated as NOT-governed (the projection-lag analog),
  which ``read_column_facts`` does NOT itself consult.

``declared_type`` is ALWAYS hint-only (``column_authority`` §4) and is never read as governed here —
so a real FTR operand (the glossary has no PARSER/SUPPORTED or SOURCE/ATTESTED type) lands at
``external_type_required=True``, matching the spike's honest disposition.

Output additivity is DERIVED per aggregation so the value stays coherent with A's compile checker
(``multisource_compile._output_additivity_coherent``): an ``identity`` single-path plan may claim
``additive`` ONLY when the path is itself additive, so a governed/concept ``additive`` operand under
SUM yields ``additive``, but a stock (semi/non-additive) summed over rows/time yields ``non_additive``,
and an unknown operand yields ``unknown`` (never a silent additive claim).

Reuses (does NOT redefine) A's ``PathAggregation``/``AdditivityClass``/``to_additivity_class``. Reads
only; edits nothing in A or the engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.contracts import DbConn
from featuregen.overlay.field_authority import Disqualifier
from featuregen.overlay.upload.column_authority import OperationalColumnFacts, read_column_facts
from featuregen.overlay.upload.field_resolution import is_feature_eligible
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.contracts import AdditivityClass, to_additivity_class
from featuregen.overlay.upload.planner.multisource_contracts import PathAggregation

__all__ = [
    "OUTPUT_POLICY_VERSION",
    "OutputPolicyV1",
    "derive_output_additivity",
    "resolve_output_policy",
]

OUTPUT_POLICY_VERSION = "3c2bib.outpol.1.0.0"

_SCHEMA_DEFAULT = "public"
_LOGICAL_REPRESENTATION = "logical_representation"
_ADDITIVITY = "additivity"

# The OPERATIONAL logical types B treats as numeric (base type, ignoring any ``(p,s)`` precision).
# Mirrors ``feature_assist._NUMERIC_TYPES`` — kept LOCAL + auditable so this module owns its own
# closed vocabulary and never imports a private symbol from a module it must not edit.
_NUMERIC_LOGICAL_TYPES: frozenset[str] = frozenset({
    "numeric", "integer", "bigint", "int", "int4", "int8", "smallint",
    "float", "double", "double precision", "decimal", "real", "money",
})


@dataclass(frozen=True, slots=True)
class OutputPolicyV1:
    """The derived output fields for one operand (fill A's ``PathStrategyV1`` /
    ``FinalExpressionV1``). ``external_type_required`` is True whenever the operational type could not
    be read as governed-and-numeric."""
    output_type: str
    output_additivity: AdditivityClass
    external_type_required: bool


def _is_numeric_logical_type(value: str | None) -> bool:
    """Is the governed operational type numeric? Base type only (``numeric(10,2)`` -> ``numeric``)."""
    base = (value or "").lower().split("(")[0].strip()
    return base in _NUMERIC_LOGICAL_TYPES


def _declared_schema(conn: DbConn, catalog_source: str, object_ref: str) -> str:
    """The REAL declared schema for the column from ``graph_node.schema_name`` (the same T7 seam),
    with ``NULL``/blank falling back to ``"public"`` — matching ``normalize_ref``'s own default. The
    concept/type field-evidence is keyed on the schema-preserving ref, so it must be recovered here."""
    row = conn.execute(
        "SELECT schema_name FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (catalog_source, object_ref)).fetchone()
    schema = row[0] if row is not None else None
    return schema or _SCHEMA_DEFAULT


def _logical_ref(conn: DbConn, catalog_source: str, object_ref: str) -> str:
    """Build the SCHEMA-PRESERVING ``logical_ref`` for the operand's public-flattened graph_node
    ``object_ref`` (``[public.]table.column``), recovering the real schema from ``graph_node``."""
    parts = object_ref.split(".")
    if len(parts) >= 2:
        table, column = parts[-2], parts[-1]
    else:
        table, column = object_ref, None
    schema = _declared_schema(conn, catalog_source, object_ref)
    return normalize_ref(catalog_source, schema, table, column)


def _field_governed(conn: DbConn, logical_ref: str, field_name: str,
                    facts: OperationalColumnFacts) -> bool:
    """Is ``field_name`` GOVERNED-and-readable for this operand? Fail-closed AND over: the adapter
    reports ``authority == "governed"`` with a non-None value, ``is_feature_eligible`` agrees, and no
    ``CONFIRMATION_PENDING_REVALIDATION`` disqualifier is active (the revalidation-lag analog)."""
    if facts.authority != "governed" or facts.value is None:
        return False
    if not is_feature_eligible(conn, logical_ref, field_name):
        return False
    if Disqualifier.CONFIRMATION_PENDING_REVALIDATION in active_disqualifiers_for(
            conn, logical_ref, field_name):
        return False
    return True


def derive_output_additivity(operand_additivity: AdditivityClass,
                             aggregation: PathAggregation) -> AdditivityClass:
    """Derive the operand's OUTPUT additivity from its input additivity + its path aggregation, so the
    result satisfies A's ``_output_additivity_coherent`` (never claim ``additive`` you can't back):

    * ``count`` / ``count_distinct`` -> ``additive`` (a count is additive across partitions; the
      operand's own additivity is irrelevant);
    * ``min`` / ``max`` -> ``non_additive`` (conservative — an extremum is never additive);
    * ``sum`` -> passes ``additive`` through; a ``semi_additive`` stock summed over rows/time is
      ``non_additive``; ``non_additive`` stays ``non_additive``; ``unknown``/``not_applicable`` ->
      ``unknown`` (honest — never silently additive).

    ``take_latest`` / ``avg`` / ``stddev`` never reach here (``b_operation`` defers/rejects them); the
    fail-closed default is ``unknown``."""
    if aggregation in (PathAggregation.count, PathAggregation.count_distinct):
        return AdditivityClass.additive
    if aggregation in (PathAggregation.min, PathAggregation.max):
        return AdditivityClass.non_additive
    if aggregation is PathAggregation.sum:
        if operand_additivity is AdditivityClass.additive:
            return AdditivityClass.additive
        if operand_additivity in (AdditivityClass.semi_additive, AdditivityClass.non_additive):
            return AdditivityClass.non_additive
        return AdditivityClass.unknown
    return AdditivityClass.unknown


def resolve_output_policy(conn: DbConn, *, catalog_source: str, object_ref: str,
                          aggregation: PathAggregation, concept_additivity: AdditivityClass,
                          now: datetime) -> OutputPolicyV1:
    """Derive the operand's output fields from governed reads, fail-closed. ``concept_additivity`` is
    the already-governed T5/T6 fallback used when the operand carries no governed ``additivity`` field.
    ``now`` is accepted for seam parity with the other governed resolvers (no freshness read here)."""
    del now  # no freshness read in this derivation; kept for resolver-seam parity.
    logical_ref = _logical_ref(conn, catalog_source, object_ref)

    # output_type + external_type_required — governed AND numeric clears external validation.
    type_facts = read_column_facts(conn, logical_ref, _LOGICAL_REPRESENTATION)
    if (_field_governed(conn, logical_ref, _LOGICAL_REPRESENTATION, type_facts)
            and _is_numeric_logical_type(type_facts.value)):
        output_type = type_facts.value or "numeric"
        external_type_required = False
    else:
        output_type = "unknown"
        external_type_required = True

    # output_additivity — governed additivity overrides the passed concept additivity, then derive.
    add_facts = read_column_facts(conn, logical_ref, _ADDITIVITY)
    if _field_governed(conn, logical_ref, _ADDITIVITY, add_facts):
        operand_additivity = to_additivity_class(add_facts.value)
    else:
        operand_additivity = concept_additivity
    output_additivity = derive_output_additivity(operand_additivity, aggregation)

    return OutputPolicyV1(output_type=output_type, output_additivity=output_additivity,
                          external_type_required=external_type_required)

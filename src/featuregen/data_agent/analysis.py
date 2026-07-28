"""`AnalysisExecutionIRV1` — a typed analysis plan, and its compiler.

Distinct from `FormulaExecutionIRV1` (roadmap §3a). That contract carries one feature, one final
operation, one grain and one spine, and its `FinalOperation` vocabulary is
identity/ratio/difference — so `current_count < previous_count` cannot be expressed in it at all.
Two IRs, sharing lower-level primitives and the same executors.

**The population spine is the whole design.** A period-over-period question is asked ABOUT a
population, not about the events. Aggregating the events and comparing the two aggregates loses
every entity that has no events in one period — and for "whose transactions decreased", the entity
that fell to zero is the most affected one in the answer. So the spine is the left side of the query
and both period aggregates hang off it, with missing counts coalesced to zero.

This compiles to SQL for the sandbox slice. The IR remains the artifact of record and can compile to
a generated Kedro project later without the ontology or the audit trail noticing.
"""
from __future__ import annotations

import hashlib

from dataclasses import dataclass, field
from enum import StrEnum

from featuregen.data_agent.dimensions import (
    UNKNOWN,
    AttributionError,
    DimensionAttributionPolicyV1,
    MissingValueBehavior,
)
from featuregen.data_agent.eligibility import TransactionEligibilityPolicyV1
from featuregen.data_agent.observation import ObservationPlanError, require_identifier
from featuregen.data_agent.physical import PhysicalDatasetBindingV1


class AnalysisIRError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Comparison(StrEnum):
    DECREASED = "decreased"
    INCREASED = "increased"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class Period:
    """One side of a period-over-period comparison, as the exact partition values it covers."""

    label: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PopulationSpine:
    """WHO the question is about. Always the left side of the query."""

    binding: PhysicalDatasetBindingV1
    key_column: str


@dataclass(frozen=True, slots=True)
class Dimension:
    """A group-by attribute, read from the spine's table."""

    column: str


@dataclass(frozen=True, slots=True)
class AnalysisRow:
    """One entity's answer."""

    key: str
    previous_count: int
    current_count: int
    dimensions: dict[str, str] = field(default_factory=dict)

    @property
    def decreased(self) -> bool:
        return self.current_count < self.previous_count

    @property
    def increased(self) -> bool:
        return self.current_count > self.previous_count


@dataclass(frozen=True, slots=True)
class AnalysisExecutionIRV1:
    """A period-over-period count per entity, with dimensions."""

    question: str
    spine: PopulationSpine
    event_binding: PhysicalDatasetBindingV1
    event_key_column: str
    period_column: str
    current: Period
    previous: Period
    measure: str = "count"
    comparison: Comparison = Comparison.DECREASED
    dimensions: tuple[Dimension, ...] = ()
    #: Which transactions count. REQUIRED — an analysis with no eligibility policy would count
    #: pending, failed and reversed transactions as activity, which changes the answer rather than
    #: merely widening it.
    eligibility: TransactionEligibilityPolicyV1 | None = None
    #: WHERE the dimension history lives, and HOW to pick the row that classifies each customer.
    #: Separate from the spine: an SCD2 table has several rows per customer and cannot BE the
    #: population without duplicating it.
    dimension_binding: PhysicalDatasetBindingV1 | None = None
    attribution: DimensionAttributionPolicyV1 | None = None

    def __post_init__(self) -> None:
        # The identifier rule is genuinely shared with observation, so it is reused rather than
        # duplicated — but its error type is re-raised as this module's, because a caller of the
        # analysis API should not have to catch an ObservationPlanError to handle a bad dimension.
        try:
            require_identifier(self.spine.key_column, what="spine key")
            require_identifier(self.event_key_column, what="event key")
            require_identifier(self.period_column, what="period column")
            for dimension in self.dimensions:
                require_identifier(dimension.column, what="dimension")
        except ObservationPlanError as exc:
            raise AnalysisIRError(exc.code, str(exc).split(": ", 1)[-1]) from None
        if self.measure != "count":
            raise AnalysisIRError("ANALYSIS_UNSUPPORTED_MEASURE",
                                  f"measure {self.measure!r} is not supported in this slice")
        if self.eligibility is None:
            raise AnalysisIRError(
                "ANALYSIS_NO_ELIGIBILITY_POLICY",
                "no transaction eligibility policy: without one, pending, failed and REVERSED "
                "transactions count as activity")
        if self.dimensions and (self.dimension_binding is None or self.attribution is None):
            raise AnalysisIRError(
                "ANALYSIS_NO_ATTRIBUTION_POLICY",
                "dimensions were requested with no attribution policy: the renderer must not choose "
                "between today's segment, the segment at the cutoff, and the segment per period")
        for period in (self.current, self.previous):
            if not period.values:
                raise AnalysisIRError(
                    "ANALYSIS_EMPTY_PERIOD",
                    f"period {period.label!r} selects no values; an empty period is either a "
                    "mistake or an unbounded scan awaiting a fallback")


    @property
    def plan_hash(self) -> str:
        """Stable identity of WHAT this plan computes.

        `question` is deliberately EXCLUDED: it is provenance, and two differently-worded questions
        that resolve to the same computation are the same plan — mirroring `authoring_run_id` being
        outside `FormulaExecutionIRV1.identity_payload`.

        The eligibility policy IS included: changing which transactions count changes the answer, so
        it must change the identity, or a cached result computed under a different definition of
        "counts" could be reused.
        """
        e = self.eligibility
        material = "|".join([
            self.spine.binding.identity.table_id, self.spine.key_column,
            self.event_binding.identity.table_id, self.event_key_column, self.period_column,
            ",".join(self.current.values), ",".join(self.previous.values),
            self.measure, str(self.comparison),
            ",".join(d.column for d in self.dimensions),
            e.status_column, ",".join(e.included_status_values), str(e.reversal_mode),
            e.reversal_column, ",".join(e.non_reversed_values), str(e.null_behavior),
        ] + ([] if self.attribution is None else [
            str(self.attribution.attribution_basis), self.attribution.report_cutoff,
            self.attribution.effective_from_column, self.attribution.effective_to_column,
            str(self.attribution.missing_value_behavior),
            self.dimension_binding.identity.table_id if self.dimension_binding else "",
        ]))
        return hashlib.sha256(material.encode()).hexdigest()[:32]


def _q(name: str) -> str:
    return '"' + name.strip() + '"'


def _literals(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def compile_analysis(ir: AnalysisExecutionIRV1, *, dialect) -> str:
    """Compile the IR to one statement.

    Shape: one CTE per period aggregating events by entity, then the SPINE **left joined** to both,
    with counts coalesced to zero. The left join is the correctness property — an inner join would
    pass on any fixture where every entity happens to appear in both periods, and drop exactly the
    entities the question is about on a real one.
    """
    class _Shim:
        def __init__(self, b): self.binding = b

    spine_ref = dialect.table_ref(_Shim(ir.spine.binding))
    event_ref = dialect.table_ref(_Shim(ir.event_binding))
    key, event_key, period = _q(ir.spine.key_column), _q(ir.event_key_column), _q(ir.period_column)

    # CRITICAL PLACEMENT: eligibility predicates belong INSIDE each period aggregation. In the outer
    # query, after the spine LEFT JOIN, a `WHERE tran_status = 'POSTED'` would drop every customer
    # whose joined columns are NULL — i.e. exactly the customers with no eligible transactions — and
    # silently destroy the population-spine guarantee the LEFT JOIN exists to provide.
    eligibility = " AND ".join(ir.eligibility.predicates(_q))

    def _period_cte(name: str, values: tuple[str, ...]) -> str:
        return (f"{name} AS (SELECT {event_key} AS k, COUNT(*) AS n FROM {event_ref} "
                f"WHERE {period} IN ({_literals(values)}) AND {event_key} IS NOT NULL "
                f"AND {eligibility} "
                f"GROUP BY {event_key})")

    ctes = [_period_cte("prev", ir.previous.values), _period_cte("curr", ir.current.values)]
    dimension_select = ""
    dimension_join = ""

    if ir.dimensions:
        # The dimension SNAPSHOT is built first, then LEFT JOINed — the same rule as the period
        # aggregates. Effective-date predicates in the outer WHERE would drop every customer with no
        # dimension history, repeating the population-spine mistake one layer up.
        dim_ref = dialect.table_ref(_Shim(ir.dimension_binding))
        validity = ir.attribution.validity_predicate(_q)
        dim_cols = ", ".join(_q(d.column) for d in ir.dimensions)
        ctes.append(
            f"dim AS (SELECT {_q(ir.spine.key_column)} AS k, {dim_cols} "
            f"FROM {dim_ref} WHERE {validity})")
        dimension_join = f"LEFT JOIN dim ON dim.k = s.{key}\n"
        if ir.attribution.missing_value_behavior is MissingValueBehavior.UNKNOWN_BUCKET:
            # Totals still reconcile: an unclassified customer is a bucket, not a disappearance.
            dimension_select = "".join(
                f", COALESCE(dim.{_q(d.column)}, '{UNKNOWN}') AS {_q(d.column)}"
                for d in ir.dimensions)
        else:
            dimension_select = "".join(f", dim.{_q(d.column)}" for d in ir.dimensions)

    joined = ",\n     ".join(ctes)
    return (
        f"WITH {joined}\n"
        f"SELECT s.{key} AS entity_key,\n"
        f"       COALESCE(prev.n, 0) AS previous_count,\n"
        f"       COALESCE(curr.n, 0) AS current_count"
        f"{dimension_select}\n"
        f"FROM {spine_ref} s\n"
        f"LEFT JOIN prev ON prev.k = s.{key}\n"
        f"LEFT JOIN curr ON curr.k = s.{key}\n"
        f"{dimension_join}"
        f"ORDER BY s.{key}")


def assert_no_dimension_overlap(conn, ir: AnalysisExecutionIRV1, *, dialect) -> None:
    """Refuse when a customer has more than one dimension row valid at the cutoff.

    Silently taking `MAX(effective_from)` would pick one classification and hide a data-quality
    problem in the dimension table — and two identical active records would duplicate the customer
    in the result, inflating every group total.
    """
    if not ir.dimensions:
        return
    class _Shim:
        def __init__(self, b): self.binding = b
    dim_ref = dialect.table_ref(_Shim(ir.dimension_binding))
    key = _q(ir.spine.key_column)
    sql = (f"SELECT COUNT(*) FROM (SELECT {key} FROM {dim_ref} "
           f"WHERE {ir.attribution.validity_predicate(_q)} "
           f"GROUP BY {key} HAVING COUNT(*) > 1) overlapping")
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        overlapping = int(cursor.fetchone()[0])
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    if overlapping:
        raise AttributionError(
            "ATTRIBUTION_OVERLAPPING_RECORDS",
            f"{overlapping} entity(ies) have more than one dimension record valid at "
            f"{ir.attribution.report_cutoff!r}; choosing one would hide a dimension-table defect")


def run_analysis(conn, ir: AnalysisExecutionIRV1, *, dialect) -> tuple[AnalysisRow, ...]:
    """Compile and execute, returning one row per entity in the population."""
    assert_no_dimension_overlap(conn, ir, dialect=dialect)
    cursor = conn.cursor()
    try:
        cursor.execute(compile_analysis(ir, dialect=dialect))
        rows = cursor.fetchall()
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()

    out: list[AnalysisRow] = []
    for row in rows:
        key, previous, current = row[0], int(row[1]), int(row[2])
        dimensions = {d.column: row[3 + i] for i, d in enumerate(ir.dimensions)}
        out.append(AnalysisRow(key=key, previous_count=previous, current_count=current,
                               dimensions=dimensions))
    return tuple(out)

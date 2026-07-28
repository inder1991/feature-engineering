"""Spec §4.2 / §8 — the COMPUTE nodes of the generated pipeline: the spine, and the PIT projection.

**This module emits text.** Like the rest of ``render``, it never imports ``pyspark`` and never
imports ``kedro``; both appear only inside the strings below. The compiler runs wherever the
compiler runs, and the artifact it emits runs on the cluster.

**What the spine is, and why it gets its own node.** It is the declared entity population (§4) — the
one row per ``(keys…, business_dt)`` that every feature is later LEFT JOINed onto. Everything
downstream inherits its membership: an entity the spine drops has no row to land on, and an entity
it duplicates duplicates every feature value in the published table. So the selection is rendered
from the DECLARED source under the DECLARED :class:`~featuregen.materialize.spine.SnapshotPolicy`,
never from a fact table and never from "the whole customer table" — which is precisely what §4.2
was written after an earlier revision left possible.

**Each policy renders its OWN selection.** Four variants, four different bodies:

* ``CURRENT_SNAPSHOT`` — the table IS the current population and holds no history, so it can only
  answer the business date it was OBSERVED at. Any other one refuses. A present-day table quietly
  answering an arbitrary historical date is the failure this variant exists to make impossible.
* ``LATEST_AVAILABLE_AS_OF`` — SCD history, and the one with real teeth. Rules 1–3 are three
  separate obligations, and a single ``row_number()`` appears to satisfy all three while failing at
  least one: the effective-time filter, the availability filter at the SAME cutoff, and a tie-break
  that REFUSES rather than picking. See :func:`_latest_available_as_of`.
* ``PARTITION_MAPPED`` — a business date selects partition values through §3.4's DECLARED mapping,
  which is read off the physical requirement rather than re-declared or inferred here.
* ``ACTIVE_POPULATION`` — a CLOSED set of declared status values. No implicit notion of "active",
  and no free-text predicate anywhere.

**Duplicate spine keys are a blocking gate, not a de-duplication step (rule 5).** Nothing here emits
``distinct`` or ``dropDuplicates``. Fan-out is already refused upstream — ``joins.py`` refuses
unknown cardinality *first*, then fan-out — so a duplicate reaching the spine means a declaration
that does not hold, and collapsing it in the renderer is how row inflation becomes invisible.

**The cutoff is stated, never inherited.** The rendered session timezone is pinned to UTC (§7), so
the cadence's governed zone is written into the node explicitly. A window or a gate computed in the
wrong zone shifts its boundaries silently, and 18:30 UTC and midnight UTC are the same business date
in two different banks.

**What raises.** Nothing here is a governed verdict about a feature — Gate 2 already decided that.
What is left is a caller whose plan, contract, spine and physical requirement do not describe one
population, and §14's closed vocabularies have no member for any of those, so they raise. The
refusals the RENDERED code performs are a different thing entirely: they are §9 gates on real rows,
and they name :class:`~featuregen.materialize.codes.ValidationGateCode` members.

──────────────────────────────────────────────────────────────────────────────────────────────────

**The PIT projection (§8 rules 1 and 2)** decides which rows one expression is allowed to SEE. It is
the node whose failure mode is invisible: a gate that keeps a row the bank did not yet know about
raises nothing, computes a number, validates beautifully in backtest and is wrong in production. So
both rules render from the expression's own :class:`~featuregen.materialize.expression_ir.PitSpec`,
and neither has a default.

* **Rule 1, the availability gate.** ``availability_ref <= cutoff``, where the basis says what that
  column MEANS. ``posted_at`` and ``ingested_at`` name the arrival instant itself; under
  ``event_time_plus_lag`` the column holds the EVENT time and the declared ``lag_hours`` is when it
  could first have been read, so the lag is added — a rendering that dropped it would admit rows for
  a lag the declaration exists to state.
* **Rule 2, window boundaries.** ``basis``, ``length``, ``unit``, ``start_inclusive`` and
  ``end_inclusive``, each honoured on its own. The two inclusivity flags are carried PER EXPRESSION
  precisely because they vary, so they are read as data rather than assumed to be ``[start, end)``.

**Calendar periods are calendar periods.** Boundaries are computed as DATES with date arithmetic —
``add_months`` for month, quarter and year — and only then converted to instants. A month is not
thirty days: three months before 2026-07-06 is 2026-04-06, and ninety days before it is 2026-04-07,
so a day-count conversion moves the boundary and silently changes which rows the feature sums. The
date-first order matters for a second reason: a boundary computed as ``instant - N*24h`` crosses a
DST change wrong, while a local midnight converted to UTC does not.

**Two zones, both stated.** The cutoff's zone is the cadence's; the window's is
``PitSpec.window_timezone``; they are separate governed fields and are separately written into the
rendered source. The rendered session is pinned to UTC (§7), so a zone left to be inherited is a
boundary quietly moved by hours.

**Only the read set is selected — never a star.** Gate 2 authorized a SET of columns, and ``*``
reads whatever the table happens to hold today, including a column added after the authorization.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from featuregen.formula.schema import (
    AggregateFunction,
    EmptyWindowResult,
    FilterBoolOp,
    FilterKind,
    FilterPredicateOp,
    FinalOperation,
    LiteralType,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    ZeroDenominator,
)
from featuregen.materialize.admission import hive_identifier
from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import MaterializationContractV1
from featuregen.materialize.expression_ir import (
    AvailabilityBasis,
    ExpressionExecutionIR,
    PitSpec,
    RefRole,
)
from featuregen.materialize.group_plan import (
    FeatureGroupPlanV1,
    PlannedFeature,
    StagingStatus,
)
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import (
    EventTimePartition,
    PartitionTransform,
    StaticSnapshot,
)
from featuregen.materialize.ir import FormulaExecutionIRV1
from featuregen.materialize.physical_types import PhysicalType
from featuregen.materialize.render.project import (
    RenderedNode,
    feature_staging_path,
    slug,
)
from featuregen.materialize.spine import (
    ActivePopulation,
    CurrentSnapshot,
    LatestAvailableAsOf,
    PartitionMappedSnapshot,
    PopulationSemantics,
    SpineSpec,
)
from featuregen.overlay.upload.object_ref import parse_ref

__all__ = [
    "SPINE_FUNC_NAME",
    "SPINE_NODE_NAME",
    "calculation_func_name",
    "calculation_node_name",
    "projection_func_name",
    "projection_node_name",
    "render_calculation_node",
    "render_projection_node",
    "render_spine_node",
]

#: One declared per-expression policy, whatever its vocabulary — ``_per_path`` polices the KEYS of a
#: mapping and never reads its values, so it must not narrow what the caller is allowed to declare.
_Declared = TypeVar("_Declared")

#: The Kedro node's name — how a failure, a resume and a ``--node`` selection all address it.
SPINE_NODE_NAME = "spine"

#: The function ``nodes.py`` defines and ``pipeline.py`` wires BY NAME.
SPINE_FUNC_NAME = "build_spine"

#: The run parameter the node reads its business date from (§11.1). It is a parameter and not a
#: rendered literal for the same reason ``generation_id`` is: a date baked into the source would
#: make two renders of one compilation differ, and would let an artifact answer exactly one day.
_BUSINESS_DT_PARAMETER = "params:business_dt"

#: The rank column the ``LATEST_AVAILABLE_AS_OF`` body adds and drops. Double-underscored so it
#: cannot collide with a governed column of the source, and dropped before the select so it can
#: never reach the published table.
_RANK_COLUMN = "__spine_rank"

#: Import statements are declared ON the node, never written into ``source``: the rendered
#: ``nodes.py`` merges and sorts them, and one hidden in a body would be emitted once per node.
_DATAFRAME_IMPORT = "from pyspark.sql import DataFrame"
_FUNCTIONS_IMPORT = "from pyspark.sql import functions as F"
_WINDOW_IMPORT = "from pyspark.sql import Window"

#: How many MONTHS one window unit is, for the units that are calendar months. ``add_months`` is
#: the calendar operation; there is deliberately no entry mapping any of these to a day count,
#: because the day count IS the defect §8 rule 2 names.
_MONTHS_PER_UNIT: dict[str, int] = {"month": 1, "quarter": 3, "year": 12}

#: How many DAYS one window unit is, for the two units that ARE a whole number of days in every
#: calendar. A day is one day and a week is seven; neither depends on where in the year it lands,
#: which is exactly what is untrue of a month.
_DAYS_PER_UNIT: dict[str, int] = {"day": 1, "week": 7}

#: ``F.trunc``'s format for the calendar period a unit names — the first instant of the period the
#: business date falls in. ``day`` is absent because a date already IS one, and Spark's ``trunc``
#: has no ``day`` format: it returns NULL for an unrecognized one, and a NULL boundary silently
#: keeps or drops every row.
_PERIOD_START: dict[str, str] = {"week": "week", "month": "month", "quarter": "quarter",
                                 "year": "year"}

#: The two window bases (``formula.schema.WindowBasis``). Closed, and rendered as two different
#: anchors rather than one anchor with a flag.
_TRAILING = "trailing"
_CALENDAR_PERIOD = "calendar_period"

#: ``formula.schema.Inclusivity`` — which comparison each boundary flag renders as.
_START_COMPARISON: dict[str, str] = {"inclusive": ">=", "exclusive": ">"}
_END_COMPARISON: dict[str, str] = {"inclusive": "<=", "exclusive": "<"}

#: How ``PartitionTransform`` renders a business DATE as a partition value. The transform is
#: DECLARED (§3.4) and this mapping only applies it; a member missing from here is refused rather
#: than defaulted, because the default a missing transform falls into is "the date as written",
#: which reads an empty partition and returns a smaller number with no error.
_TRANSFORMS: dict[PartitionTransform, str] = {
    PartitionTransform.DATE_ISO: "business_date",
    PartitionTransform.DATE_COMPACT: "business_date.replace('-', '')",
}

#: The marker the calculation adds to its grain-level aggregate and drops before staging. It is
#: what tells an entity with NO source rows apart from one whose aggregate evaluated to NULL — both
#: are null after a left join, and §8 rule 4 gives them different declared answers. Rendered only
#: when the declared ``empty_window`` needs the distinction; double-underscored so it cannot collide
#: with a governed column, and dropped so it can never reach the published table.
_SOURCE_ROWS = "__source_rows"

#: The body paths each ``FinalOperation`` compiles to, **in slot order**. The order is semantic and
#: not a convenience: a numerator rendered where the denominator belongs inverts every value in the
#: column, and nothing downstream would say so. Keying the caller's projections and policies by
#: these paths — rather than pairing them positionally — is what makes that swap unrepresentable.
#: The paths themselves are Child-1's (``expression_ir.BODY_PATHS``), mirrored here as an ORDERED
#: mapping because that module's set says which paths exist and not which operation owns which.
_BODY_SLOTS: Mapping[FinalOperation, tuple[str, ...]] = {
    FinalOperation.IDENTITY: ("body.expr",),
    FinalOperation.RATIO: ("body.numerator", "body.denominator"),
    FinalOperation.DIFFERENCE: ("body.minuend", "body.subtrahend"),
}

#: Spark's aggregate for each member of Child-1's closed vocabulary. ``COUNT_ROWS`` is absent
#: because it has no operand to substitute into (``schema.py`` [c9]) and is rendered on its own.
_AGGREGATE_CALLS: dict[AggregateFunction, str] = {
    AggregateFunction.SUM: "F.sum",
    AggregateFunction.COUNT_NON_NULL: "F.count",
    AggregateFunction.COUNT_DISTINCT: "F.countDistinct",
}

#: The two ``RoundingMode`` members a Spark function implements EXACTLY. The other four would have
#: to be composed from floor/ceil over a scaled value, and §6 requires the DECLARED mode rather than
#: one this renderer assembled — so they refuse rather than approximate.
_ROUNDING_CALLS: dict[RoundingMode, str] = {
    RoundingMode.HALF_UP: "F.round",
    RoundingMode.HALF_EVEN: "F.bround",
}

#: What each rendered rounding mode DOES, in the generated project's own terms. The renderer's menu
#: of implementable modes is not the bank's business: a comment saying "the two available modes"
#: describes this compiler's limits rather than this feature's declared behaviour.
_ROUNDING_NOTES: dict[RoundingMode, str] = {
    RoundingMode.HALF_UP: "a tie rounds AWAY from zero, so 2.5 becomes 3.",
    RoundingMode.HALF_EVEN: "a tie rounds to the EVEN neighbour, so 2.5 becomes 2 and 3.5 becomes 4.",
}

#: ``FilterPredicateOp`` → the Python operator its rendered comparison uses.
_COMPARISONS: dict[FilterPredicateOp, str] = {
    FilterPredicateOp.EQUAL: "==",
    FilterPredicateOp.NOT_EQUAL: "!=",
    FilterPredicateOp.GREATER_THAN: ">",
    FilterPredicateOp.GREATER_OR_EQUAL: ">=",
    FilterPredicateOp.LESS_THAN: "<",
    FilterPredicateOp.LESS_OR_EQUAL: "<=",
}

#: The two predicates that take no right-hand side at all.
_NULL_TESTS: dict[FilterPredicateOp, str] = {
    FilterPredicateOp.IS_NULL: "isNull",
    FilterPredicateOp.IS_NOT_NULL: "isNotNull",
}

#: The two that take a SET.
_SET_TESTS = frozenset({FilterPredicateOp.IN, FilterPredicateOp.NOT_IN})

#: ``DECIMAL(p,s)`` as ``physical_types`` publishes it.
_DECIMAL_SQL_TYPE = re.compile(r"DECIMAL\((\d+),(\d+)\)", re.IGNORECASE)

#: The width a rendered CALL is allowed to reach before it is split one argument per line. It is
#: this repository's own ``line-length``, because the generated project is Python a bank reads and
#: there is no reason to hold it to a narrower rule than the code that emits it. Fixed, so the
#: choice depends on nothing but the rendered names: a call split where it would have fitted makes
#: a reader look for the significance of the split, and significance is what they will assume.
_RENDERED_WIDTH = 100

#: How many parents up from the rendered ``nodes.py`` the project root is:
#: ``<root>/src/<package>/pipelines/<pipeline>/nodes.py``. Stated once, and proved against a really
#: rendered project rather than counted by eye — a wrong depth resolves to a directory that may well
#: exist, and the node would then read somebody else's lock or fail on the cluster.
_LOCK_DEPTH = 4


# ── the public entry point ───────────────────────────────────────────────────────────────────────


def render_spine_node(
    spine: SpineSpec,
    plan: FeatureGroupPlanV1,
    contract: MaterializationContractV1,
    *,
    spine_input: PhysicalInputRequirement,
    source_dataset: str,
    spine_dataset: str,
) -> RenderedNode:
    """Render the node that produces the declared entity population for one business date (§4.2).

    Args:
        spine: what §4's declaration established — the population, its keys, its policy.
        plan: the packing list. Its ``entity_key_columns`` are the names the published table uses,
            read off the plan rather than re-derived: a second derivation is a second chance to
            disagree about what a governed ref names.
        contract: the group's materialization contract, for the cadence-derived cutoff (§8) and for
            the landing keys the plan's columns were derived from.
        spine_input: the resolved physical requirement for the population's table (§3.5). Its
            ``partition_mapping`` is the DECLARED mapping ``PARTITION_MAPPED`` renders from.
        source_dataset: the catalog name of the governed source. A NAME — the location behind it is
            catalog configuration, which is the only place a location may be written.
        spine_dataset: the catalog name this node writes.

    Returns:
        A :class:`~featuregen.materialize.render.project.RenderedNode` whose wiring Task 12 checks.

    Raises:
        TypeError: an argument is not the type it must be.
        ValueError: the plan, contract, spine and requirement do not describe one population — the
            landing keys disagree, the policy is outside §4.2's closed union, a
            ``CURRENT_ACTIVE_ONLY`` claim rests on a policy that cannot support it, or a
            ``PARTITION_MAPPED`` policy has no declared mapping to render.
    """
    if not isinstance(spine, SpineSpec):
        raise TypeError(
            f"rendering the spine needs the SpineSpec §4 validated, got {type(spine).__name__}: it "
            f"is the record that the governed facts did not refute the declaration, and a bare "
            f"declaration would render a population nothing checked")
    if not isinstance(plan, FeatureGroupPlanV1):
        raise TypeError(
            f"rendering the spine needs the FeatureGroupPlanV1, got {type(plan).__name__}: the plan "
            f"is what states the COLUMN names the published table carries, and a spine that named "
            f"its own would land its keys under names the group never planned")
    if not isinstance(contract, MaterializationContractV1):
        raise TypeError(
            f"rendering the spine needs the MaterializationContractV1, got "
            f"{type(contract).__name__}: the §8 cutoff is derived from ITS cadence, and a cutoff "
            f"taken from anywhere else is a second answer to which clock the group runs on")
    if not isinstance(spine_input, PhysicalInputRequirement):
        raise TypeError(
            f"rendering the spine needs the population's PhysicalInputRequirement, got "
            f"{type(spine_input).__name__}: §3.4's partition mapping lives on it, and a mapping "
            f"taken from anywhere else is the second copy nobody governs")
    _dataset(source_dataset, "the spine's source dataset")
    _dataset(spine_dataset, "the spine's output dataset")

    keys = _key_columns(spine, plan, contract)
    _check_claim(spine)

    availability = (None if spine.availability_ref is None
                    else _column(spine.availability_ref, "the spine's availability_ref"))
    cutoff_zone = contract.pit_semantics.cutoff_timezone
    cutoff_time = contract.pit_semantics.cutoff_time

    policy = spine.snapshot_policy
    if isinstance(policy, CurrentSnapshot):
        body, needs_window = _current_snapshot(policy), False
    elif isinstance(policy, LatestAvailableAsOf):
        body, needs_window = _latest_available_as_of(policy, keys), True
    elif isinstance(policy, PartitionMappedSnapshot):
        body, needs_window = _partition_mapped(policy, spine_input), False
    elif isinstance(policy, ActivePopulation):
        body, needs_window = _active_population(policy), False
    else:
        raise ValueError(
            f"the declaration carries a snapshot policy this renderer has no body for "
            f"({type(policy).__name__}): §4.2's union is CLOSED, and the default a missing variant "
            f"falls into is to read the whole table — the exact behaviour the section exists to "
            f"eliminate")

    imports = [_DATAFRAME_IMPORT, _FUNCTIONS_IMPORT] + ([_WINDOW_IMPORT] if needs_window else [])
    return RenderedNode(
        name=SPINE_NODE_NAME,
        func_name=SPINE_FUNC_NAME,
        source=_render_source(
            spine, keys, body,
            availability=availability, cutoff_zone=cutoff_zone, cutoff_time=cutoff_time,
            business_dt_column=plan.business_dt_column),
        inputs=(source_dataset, _BUSINESS_DT_PARAMETER),
        outputs=(spine_dataset,),
        imports=tuple(imports),
        tags=("primary", "spine"),
    )


# ── what the caller must have got right ──────────────────────────────────────────────────────────


def _dataset(name: str, what: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"{what} has no name ({name!r}): a node carries dataset NAMES and the catalog carries "
            f"the locations behind them, so a blank name is a read or a write nobody declared")
    return name


def _column(ref: str, what: str) -> str:
    """One governed ref's COLUMN name — the same reduction ``group_plan._key_columns`` performs."""
    try:
        _source, _schema, _table, column = parse_ref(ref)
    except ValueError as exc:
        raise ValueError(
            f"{what} is {ref!r}, which is not an addressable column ref ({exc}): the rendered node "
            f"selects columns, and a ref nothing can name is not one") from exc
    if column is None:
        raise ValueError(
            f"{what} is {ref!r}, which names a TABLE and not a column: a relation cannot be read as "
            f"a key, a status or a clock")
    return column


def _key_columns(
    spine: SpineSpec, plan: FeatureGroupPlanV1, contract: MaterializationContractV1,
) -> tuple[tuple[str, str], ...]:
    """``(source column, published column)`` per key, positionally — never re-derived.

    The published names come off the PLAN (which normalized them once, through the one Hive
    normalizer) and the source names off the spine's own refs. The contract sits between the two and
    is what proves they are the same keys in the same order, so a disagreement is caught here rather
    than surfacing as a spine whose columns are aliased into another population's names.
    """
    if tuple(contract.ordered_keys) != tuple(spine.ordered_key_refs):
        raise ValueError(
            f"the contract's landing key(s) {list(contract.ordered_keys)} are not the spine's "
            f"{list(spine.ordered_key_refs)}: the published row is keyed by the contract and its "
            f"rows come from the spine, so two different key lists would alias one population's "
            f"columns into another's names")
    if len(plan.entity_key_columns) != len(spine.ordered_key_refs):
        raise ValueError(
            f"the plan publishes {len(plan.entity_key_columns)} landing key column(s) "
            f"{list(plan.entity_key_columns)} and the spine declares {len(spine.ordered_key_refs)} "
            f"key ref(s): the spine's select is positional, so an arity that differs would land one "
            f"key's values under another key's name")
    if plan.business_dt_column != contract.pit_semantics.business_dt_column:
        raise ValueError(
            f"the plan's business-date column {plan.business_dt_column!r} is not the contract's "
            f"{contract.pit_semantics.business_dt_column!r}: the spine emits one row per "
            f"(keys…, business_dt) and the two names must be one name")
    return tuple(
        (_column(ref, "a spine landing key"), published)
        for ref, published in zip(spine.ordered_key_refs, plan.entity_key_columns))


def _check_claim(spine: SpineSpec) -> None:
    """§4.2 rule 4 — ``CURRENT_ACTIVE_ONLY`` REQUIRES ``ActivePopulation``.

    ``validate_spine_declaration`` enforces this at declaration time; it is re-checked here because
    the renderer is the last place that can, and a spine rendered around a claim its policy cannot
    support publishes an "active only" population whose body filters nothing.
    """
    active_claim = spine.population_semantics is PopulationSemantics.CURRENT_ACTIVE_ONLY
    if active_claim and not isinstance(spine.snapshot_policy, ActivePopulation):
        raise ValueError(
            f"the declaration claims CURRENT_ACTIVE_ONLY under a "
            f"{type(spine.snapshot_policy).__name__} policy: there is no implicit notion of "
            f"'active', so the claim can only rest on a declared status column and a declared set "
            f"of values")


@dataclass(frozen=True, slots=True)
class _Body:
    """One policy's contribution to the node: what it REFUSES up front, and what it SELECTS.

    The two are kept apart so a guard is rendered before anything else the node does. A refusal that
    ran after the run had begun deriving values would read as though the date were answerable right
    up to the point where it turned out not to be.
    """

    guard: tuple[str, ...]
    selection: tuple[str, ...]


# ── the four policy bodies ───────────────────────────────────────────────────────────────────────


def _current_snapshot(policy: CurrentSnapshot) -> _Body:
    """The vintage guard, and then the whole table — because the table IS the population.

    There is no time filter to render: a table that holds no history has nothing to filter BY. What
    it has instead is the vintage it was observed at, and any other business date refuses.

    The guard is a GUARD and is rendered before anything else the node does, including the cutoff.
    A refusal that came after the run had started deriving values would read as though the date were
    answerable right up to the point where it was not.
    """
    observed = _safe_text(policy.observed_snapshot_ref, "the declared observed_snapshot_ref")
    return _Body(
        guard=(
            "    # §4.2 — the declared population is a CURRENT snapshot: the table IS the population",
            "    # and holds no history, so the ONLY business date it can answer is the one it was",
            "    # observed at. Answering another one would be today's rows wearing a past date.",
            f"    if business_date != {policy.observed_snapshot_ref!r}:",
            *_refuse(
                ValidationGateCode.SPINE_INCOMPLETE,
                f"the declared population is a snapshot observed at {observed} and holds no "
                f"history, so it cannot honestly answer another business date. The spine refuses "
                f"rather than answering with the rows it holds today. Asked for:",
                tail="repr(business_date)"),
        ),
        selection=(
            "    # No time filter: a table that holds no history has nothing to filter BY, and the",
            "    # vintage it can answer was settled by the guard above.",
            "    rows = source",
        ))


def _latest_available_as_of(policy: LatestAvailableAsOf, keys: tuple[tuple[str, str], ...]) -> _Body:
    """SCD history: rules 1, 2 and 3 — three obligations, rendered as three separate things.

    Rule 1 is a CONJUNCTION and both halves are load-bearing. The effective-time half keeps out a
    version that had not taken effect; the availability half keeps out one that had taken effect and
    had not yet ARRIVED. An implementation with only the first picks the later, unavailable version
    and returns a population nobody could have known about at the cutoff.

    Rule 3 is why this uses ``rank()`` and not ``row_number()``. ``row_number()`` invents an order
    where the declaration provides none: every tie resolves, the run succeeds, and the population
    changes between runs with every downstream number moving with it. ``rank()`` gives EVERY row
    tied on the complete declared ordering the same rank, so an unresolved tie survives to be seen —
    and it is then refused, not repaired.
    """
    effective = _column(policy.effective_time_ref, "the policy's effective_time_ref")
    available = _column(policy.availability_ref, "the policy's availability_ref")
    breaks = [_column(ref, "a deterministic_tie_break_ref")
              for ref in policy.deterministic_tie_break_refs]
    ordering = ", ".join(f"F.col({name!r}).desc()" for name in (effective, *breaks))
    partition = ", ".join(f"F.col({name!r})" for name, _published in keys)
    declared = ", ".join(_safe_text(name, "a tie-break column") for name in breaks) or "none declared"
    return _Body(guard=(), selection=(
        "    # §4.2 rule 1 — a CONJUNCTION, and BOTH halves matter. The first keeps out a version",
        "    # that had not taken effect by the cutoff; the second keeps out one that had taken",
        "    # effect and had not yet ARRIVED. The same cutoff for both, and they are written as two",
        "    # filters because they are two obligations.",
        f"    eligible = source.where(F.col({effective!r}) <= cutoff)",
        f"    eligible = eligible.where(F.col({available!r}) <= cutoff)",
        "",
        "    # §4.2 rules 2 and 3 — the greatest effective time per key wins, and the declared",
        "    # tie-break refs order rows that share one. F.rank() deliberately, NOT a row-numbering",
        "    # window: numbering the rows would invent an order the declaration does not provide, so",
        "    # every tie would resolve and none would be seen. Under rank() a tie SURVIVES, below.",
        f"    latest_first = Window.partitionBy({partition}).orderBy(",
        f"        {ordering})",
        f"    ranked = eligible.withColumn({_RANK_COLUMN!r}, F.rank().over(latest_first))",
        f"    winners = ranked.where(F.col({_RANK_COLUMN!r}) == 1).drop({_RANK_COLUMN!r})",
        "",
        "    # A key with more than one rank-1 row is a tie NO declared ordering separates. It",
        "    # depends on the actual rows, so it is a gate here and not a compilation refusal.",
        f"    unresolved = winners.groupBy({partition}).count().where(F.col('count') > 1)",
        "    if unresolved.limit(1).count() > 0:",
        *_refuse(
            ValidationGateCode.SPINE_NON_DETERMINISTIC,
            f"two eligible rows share an effective time and every declared tie-break "
            f"({declared}), so no declared ordering separates them. Picking one would change the "
            f"population between runs and move every feature value with it, so the spine refuses."),
        "    rows = winners",
    ))


def _partition_mapped(policy: PartitionMappedSnapshot,
                      spine_input: PhysicalInputRequirement) -> _Body:
    """A business date selects partition values — through §3.4's DECLARED mapping.

    The mapping is READ off the physical requirement, never re-declared here and never inferred: two
    copies of one mapping is two mappings, and the second one is the one nobody governs. Only the
    two variants that actually SELECT a partition set from a declaration are renderable. ``FullScan``
    reads every partition (the opposite of what this policy claims), ``AvailabilityPartition``
    widens the set for late arrivals (which cannot produce one row per key without a rule nobody
    declared), and ``VerifiedUnpartitioned`` says there are no partitions at all.
    """
    declared = [_column(ref, "an ordered_partition_ref") for ref in policy.ordered_partition_refs]
    mapping = spine_input.partition_mapping
    if isinstance(mapping, EventTimePartition):
        named = [mapping.partition_column]
        transform = _TRANSFORMS.get(mapping.transform)
        if transform is None:
            raise ValueError(
                f"the declared partition mapping renders values with {mapping.transform!r}, which "
                f"this renderer has no form for: PartitionTransform is CLOSED, and applying an "
                f"unmodelled one would read an empty partition and return a smaller population "
                f"with no error")
        values = [(mapping.partition_column, transform)]
    elif isinstance(mapping, StaticSnapshot):
        named = [column for column, _value in mapping.partition_values]
        values = [(column, repr(value)) for column, value in mapping.partition_values]
    else:
        raise ValueError(
            f"the population's table declares the partition mapping "
            f"{type(mapping).__name__ if mapping is not None else None}, which does not resolve a "
            f"business date to partition VALUES: PARTITION_MAPPED says a business date selects the "
            f"partitions, and rendering it over a mapping that reads every partition — or none, or "
            f"a widened set for late arrivals — would read a population the declaration did not "
            f"describe")
    if [name.strip().lower() for name in declared] != [name.strip().lower() for name in named]:
        raise ValueError(
            f"the policy names partition column(s) {declared} and the declared mapping partitions "
            f"on {named}: the mapping is governed in one place (§3.4), and a policy that names "
            f"other columns is the second copy of it")

    lines = [
        "    # §4.2 / §3.4 — the business date selects partition values through the DECLARED",
        "    # mapping. The mapping is governed in the environment inventory; this applies it.",
    ]
    for index, (column, value) in enumerate(values):
        read_from = "source" if index == 0 else "rows"
        lines.append(f"    rows = {read_from}.where(F.col({column!r}) == F.lit({value}))")
    return _Body(guard=(), selection=tuple(lines))


def _active_population(policy: ActivePopulation) -> _Body:
    """A CLOSED set of declared status values — no implicit "active", no free-text predicate."""
    if not policy.allowed_status_values:
        raise ValueError(
            "the ACTIVE_POPULATION policy declares no allowed status values: an empty closed set "
            "selects nothing, so the spine would be empty and every landing key would disappear "
            "with no error at all")
    status = _column(policy.status_ref, "the policy's status_ref")
    allowed = list(policy.allowed_status_values)
    return _Body(guard=(), selection=(
        "    # §4.2 — current rows restricted to a CLOSED set of DECLARED status values. A reviewer",
        "    # can read the population rule here without reading any SQL, which is the point.",
        f"    rows = source.where(F.col({status!r}).isin({allowed!r}))",
    ))


# ── the shared frame around every policy ─────────────────────────────────────────────────────────


def _wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap. Deterministic by construction — it observes nothing but ``text``.

    Rendered prose is wrapped rather than hand-broken because the values spliced into it vary in
    length: a message hand-wrapped around one bank's column names is a ragged message for the next.
    """
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and len(candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    return lines


def _comment(text: str, *, indent: str = "    ", width: int = 92) -> list[str]:
    """A rendered comment block, WRAPPED rather than hand-broken.

    Declared column names and timezones are spliced into these, and they vary in length: a comment
    hand-wrapped around one bank's names is a ragged comment for the next, and a long one is a line
    a reviewer scrolls past.
    """
    return [f"{indent}# {line}" for line in _wrap(text, width)]


def _safe_text(value: str, what: str) -> str:
    """A declared value spliced INTO a rendered prose message.

    Column names and status values reach the rendered source through ``repr``, which quotes them.
    A value written into a message has no such protection, so a quote or a backslash in one would
    end the literal early — and the node would either fail to parse or parse into something else.
    """
    if any(character in value for character in '"\\\n\r'):
        raise ValueError(
            f"{what} is {value!r}, which carries a quote, a backslash or a newline: it is written "
            f"into a rendered message, where any of the three would end the string literal early")
    return value


def _refuse(code: ValidationGateCode, text: str, *, tail: str | None = None,
            indent: str = "        ") -> list[str]:
    """``raise RuntimeError("CODE: …")`` — one §9 gate, word-wrapped.

    The generated project cannot import ``featuregen``, so the code travels as TEXT; taking it from
    the enum rather than spelling it means a code that leaves §14's vocabulary stops rendering.

    ``tail`` is a rendered EXPRESSION appended to the message, and it is the only way a run-time
    value enters one. Always at the end: a value spliced into the middle forces the prose around it
    to be hand-wrapped, which is how a message ends up saying something its author did not check.
    """
    return _raise_lines(f"{code.value}: {text}", tail=tail, indent=indent)


def _raise_lines(message: str, *, tail: str | None = None,
                 indent: str = "        ") -> list[str]:
    """``raise RuntimeError(…)`` with ``message`` word-wrapped across string literals.

    Separate from :func:`_refuse` because not every rendered abort is a §9 gate. A formula's own
    declared ``empty_window: error`` stops the run on the FORMULA's policy (§8 rule 4), and §14's
    closed vocabulary has no member for it — prefixing one of its codes would put a word into the
    gate table's mouth, and inventing a new one would put it into an operator's.
    """
    wrapped = _wrap(message, 84)
    inner = indent + "    "
    parts = [f'{inner}"{part} "' for part in wrapped[:-1]] + [f'{inner}"{wrapped[-1]}"']
    if tail is not None:
        # The space belongs INSIDE the literal: `"…for:" + repr(x)` renders `for:'2020-01-01'`.
        joined = f'{inner}"{wrapped[-1]} " + {tail}'
        # A long tail on an already-full last line is the one place this wrapper can still emit a
        # line nobody sized. It goes on its own continuation rather than trailing off the screen.
        parts[-1] = joined if len(joined) + 1 <= _RENDERED_WIDTH else (
            f'{inner}"{wrapped[-1]} "\n{inner}+ {tail}')
    return [f"{indent}raise RuntimeError(", *parts[:-1], f"{parts[-1]})"]


def _cutoff_lines(cutoff_time: str, cutoff_zone: str) -> list[str]:
    """§8's ``cutoff``, in the CADENCE's governed zone — the one shape both nodes must use.

    Shared rather than spelled twice: the spine's rule 6 and an expression's rule 1 are the same
    comparison against the same instant, and two renderings of one instant is one of them being
    wrong later.
    """
    return [
        f"    # §8's cutoff: the business date at {cutoff_time} in {cutoff_zone}. The rendered",
        "    # session timezone is pinned to UTC, so the governed zone is STATED here rather",
        "    # than inherited — a cutoff computed in the wrong zone moves silently.",
        "    cutoff = F.to_utc_timestamp(",
        f"        F.to_timestamp(F.lit(business_date + {' ' + cutoff_time!r})), {cutoff_zone!r})",
    ]


def _render_source(
    spine: SpineSpec,
    keys: tuple[tuple[str, str], ...],
    body: _Body,
    *,
    availability: str | None,
    cutoff_zone: str,
    cutoff_time: str,
    business_dt_column: str,
) -> str:
    """The whole node, in the order it must be read: the policy's guard, the cutoff, the policy's
    own selection, rule 6, the landing shape, rule 5."""
    policy = spine.snapshot_policy
    key_columns = ", ".join(_safe_text(published, "a landing key column")
                            for _source, published in keys)
    landing = f"({key_columns}, {_safe_text(business_dt_column, 'the business-date column')})"

    summary = (
        f"Exactly one row per {landing}. Every feature is LEFT JOINed onto this, so an entity "
        f"missing here has no row to land on, and an entity duplicated here duplicates every "
        f"feature value in the published table.")
    lines = [
        f"def {SPINE_FUNC_NAME}(source: DataFrame, business_dt: str) -> DataFrame:",
        f'    """The declared entity population for one business date — {policy.kind.value} (§4).',
        "",
        *(f"    {part}" for part in _wrap(summary, 92)),
        '    """',
        "    business_date = str(business_dt)",
    ]
    if body.guard:
        lines += [*body.guard, ""]

    # The policy's own availability filter, when it has one, already applies the spine's own column
    # at this same cutoff. Filtering twice on one column is noise a reviewer has to rule out.
    policy_filters_availability = (
        isinstance(policy, LatestAvailableAsOf)
        and availability is not None
        and _column(policy.availability_ref, "the policy's availability_ref") == availability)

    if availability is not None or isinstance(policy, LatestAvailableAsOf):
        lines += [*_cutoff_lines(cutoff_time, cutoff_zone), ""]
    lines += [*body.selection]

    if availability is not None and not policy_filters_availability:
        lines += [
            "",
            "    # §4.2 rule 6 — the spine's OWN availability_ref participates in PIT filtering",
            "    # exactly as an expression's does: a member that had not yet arrived at the cutoff",
            "    # was not knowable then, so it is not in the population.",
            f"    rows = rows.where(F.col({availability!r}) <= cutoff)",
        ]
    elif policy_filters_availability:
        lines += [
            "",
            f"    # §4.2 rule 6 — the spine's OWN availability_ref is {availability!r}, which the",
            "    # eligibility filter above already applied at this same cutoff. Filtering on it a",
            "    # second time would change nothing and would read as though it were a second rule.",
        ]

    selected = ", ".join(f"F.col({source!r}).alias({published!r})" for source, published in keys)
    grouped = ", ".join(
        f"F.col({name!r})" for name in (*(published for _s, published in keys), business_dt_column))
    lines += [
        "",
        "    # The landing shape (§8 rule 3): the planned key columns, plus the business date the",
        "    # run was given. Nothing else — the system columns are added once, at assembly.",
        f"    spine = rows.select({selected}).withColumn(",
        f"        {business_dt_column!r}, F.to_date(F.lit(business_date)))",
        "",
        "    # §4.2 rule 5 — duplicate spine keys are a BLOCKING GATE, never a de-duplication step.",
        "    # Collapsing the rows here would turn a declaration that does not hold into a smaller",
        "    # population that looks right, and every count downstream would move with it.",
        "    duplicated = spine.groupBy(",
        f"        {grouped}).count().where(F.col('count') > 1)",
        "    if duplicated.limit(1).count() > 0:",
        *_refuse(
            ValidationGateCode.SPINE_DUPLICATE_KEY,
            f"the declared population produced more than one row for a {landing}. §4.2 makes that "
            f"a blocking gate rather than a de-duplication step: collapsing the rows would hide a "
            f"population the declaration got wrong."),
        "    return spine",
    ]
    return "\n".join(lines) + "\n"


# ══ §8 rules 1 and 2 — the point-in-time projection ══════════════════════════════════════════════


def projection_node_name(feature_column: str, expr_path: str) -> str:
    """The Kedro node name for one expression's projection — how an operator addresses it."""
    return f"project_{feature_column}__{slug(expr_path)}"


def projection_func_name(feature_column: str, expr_path: str) -> str:
    """The function ``nodes.py`` defines and ``pipeline.py`` wires BY NAME. One per expression:
    §7 gives every expression its own projection, and no scan is shared in this slice."""
    return projection_node_name(feature_column, expr_path)


def render_projection_node(
    expression: ExpressionExecutionIR,
    contract: MaterializationContractV1,
    *,
    feature_column: str,
    source_dataset: str,
    projection_dataset: str,
) -> RenderedNode:
    """Render the node that decides which rows ONE expression may see (§8 rules 1 and 2).

    Args:
        expression: the compiled expression. Its ``pit`` is the whole of what is rendered here, and
            its ``physical_read_set`` is the only set of columns the node is allowed to name.
        contract: the group's materialization contract. Rule 1's cutoff is derived from ITS cadence
            — the window's zone is the expression's own and is a different field.
        feature_column: the published column this expression contributes to. Names the node.
        source_dataset: the catalog name of the governed source table. A NAME; the location behind
            it is catalog configuration, which is the only place a location may be written.
        projection_dataset: the catalog name this node writes.

    Returns:
        A :class:`~featuregen.materialize.render.project.RenderedNode` whose wiring Task 12 checks.

    Raises:
        TypeError: an argument is not the type it must be.
        ValueError: the expression cannot be projected as one relation — its read set spans tables,
            its clock or its availability column is outside the authorized read set, its window
            names a basis, unit or inclusivity outside the closed vocabulary, or its declared lag
            and its declared basis contradict each other.
    """
    if not isinstance(expression, ExpressionExecutionIR):
        raise TypeError(
            f"rendering a projection needs the compiled ExpressionExecutionIR, got "
            f"{type(expression).__name__}: the PIT spec, the read set and the join plan are what "
            f"decide which rows the feature may see, and none of them can be inferred from a name")
    if not isinstance(contract, MaterializationContractV1):
        raise TypeError(
            f"rendering a projection needs the MaterializationContractV1, got "
            f"{type(contract).__name__}: §8's cutoff is derived from ITS cadence, and a cutoff "
            f"taken from anywhere else is a second answer to which clock the group runs on")
    if not isinstance(feature_column, str) or not feature_column.isidentifier():
        raise ValueError(
            f"the published column this expression feeds is {feature_column!r}, which is not an "
            f"identifier: it names the node and the rendered function, and a name Python cannot "
            f"parse is a generated project that fails at import")
    _dataset(source_dataset, "the projection's source dataset")
    _dataset(projection_dataset, "the projection's output dataset")

    pit = expression.pit
    read_set = _read_set_columns(expression)
    clock = _read_column(pit.event_time_ref, read_set, "the window's event_time_ref")
    available = _read_column(pit.availability_ref, read_set, "the expression's availability_ref")

    source = "\n".join([
        f"def {projection_func_name(feature_column, expression.expr_path)}"
        f"(source: DataFrame, business_dt: str) -> DataFrame:",
        *_projection_docstring(expression, feature_column),
        "    business_date = str(business_dt)",
        "",
        *_read_set_lines(read_set),
        "",
        *_cutoff_lines(contract.pit_semantics.cutoff_time, contract.pit_semantics.cutoff_timezone),
        "",
        *_availability_gate(pit, available),
        "",
        *_window_boundaries(pit, clock, contract.pit_semantics.cutoff_timezone),
        "    return rows",
    ]) + "\n"
    return RenderedNode(
        name=projection_node_name(feature_column, expression.expr_path),
        func_name=projection_func_name(feature_column, expression.expr_path),
        source=source,
        inputs=(source_dataset, _BUSINESS_DT_PARAMETER),
        outputs=(projection_dataset,),
        imports=(_DATAFRAME_IMPORT, _FUNCTIONS_IMPORT),
        tags=("intermediate", "projection"),
    )


# ── what the expression must be for one relation to be projectable ───────────────────────────────


def _read_set_columns(expression: ExpressionExecutionIR) -> tuple[str, ...]:
    """The authorized COLUMN names of the one relation this projection reads, sorted.

    Sorted rather than left in read-set order so the rendered bytes cannot move with a change to
    how the compiler happened to walk the expression — ``generated_project_hash`` must identify
    what was built.

    A read set spanning two tables is refused rather than rendered. Reaching the second table needs
    the join plan, and this renderer emits no join: rendering one relation and silently dropping
    the traversal would compute the aggregate over the wrong rows, which is the failure mode that
    reports nothing.
    """
    if expression.join_plan.steps:
        raise ValueError(
            f"the expression's governed join plan has {len(expression.join_plan.steps)} hop(s) and "
            f"this renderer projects a single relation: rendering the source table alone would "
            f"drop the traversal that reaches the grain, and the aggregate would be computed over "
            f"rows that never belonged to the entity — with nothing to report it")
    tables = {(ref.schema.strip().lower(), ref.table.strip().lower())
              for ref in expression.physical_read_set}
    if len(tables) != 1:
        raise ValueError(
            f"the expression's read set spans {sorted(tables)}: a projection reads ONE governed "
            f"relation, and a node handed one dataset cannot name columns of another table")
    columns = sorted({ref.column for ref in expression.physical_read_set if ref.column is not None})
    if not columns:
        raise ValueError(
            "the expression's read set names no column at all, only the relation: a projection "
            "that selected nothing would hand the calculation an empty row shape, and `*` — the "
            "only other thing it could select — reads whatever the table happens to hold today")
    relation = {ref.logical_ref for ref in expression.physical_read_set
                if RefRole.SOURCE_TABLE in ref.roles}
    if len(relation) != 1:
        raise ValueError(
            f"the expression's read set names {len(relation)} source relation(s): §2.1 records the "
            f"relation itself as a read, and a projection with no relation — or two — is not one "
            f"table's rows")
    return tuple(columns)


def _read_column(ref: str, read_set: tuple[str, ...], what: str) -> str:
    """One PIT ref reduced to a column, REQUIRED to be inside the authorized read set.

    Gate 2 authorized a set of columns. A filter on a column outside it is a read nobody governed,
    and it would not appear in the projection's select either — so the node would fail on the
    cluster, at the point where the cheapest thing to have done was refuse here.
    """
    column = _column(ref, what)
    if column not in read_set:
        raise ValueError(
            f"{what} is {ref!r}, whose column {column!r} is not in the authorized read set "
            f"{list(read_set)}: §1.3 authorized a SET of columns, and filtering on one outside it "
            f"is a read this group was never granted")
    return column


# ── the rendered body ────────────────────────────────────────────────────────────────────────────


def _projection_docstring(expression: ExpressionExecutionIR, feature_column: str) -> list[str]:
    pit = expression.pit
    summary = (
        f"Which rows {_safe_text(feature_column, 'the feature column')} / "
        f"{_safe_text(expression.expr_path, 'the expression path')} may SEE — nothing is aggregated "
        f"here. A row survives only if it had ARRIVED by the cutoff (rule 1) and its event time "
        f"falls inside the declared {pit.window_length}-{_safe_text(pit.window_unit, 'the unit')} "
        f"{_safe_text(pit.window_basis, 'the basis')} window (rule 2).")
    return [
        f'    """Point-in-time rows for {_safe_text(feature_column, "the feature column")} (§8).',
        "",
        *(f"    {part}" for part in _wrap(summary, 92)),
        '    """',
    ]


def _read_set_lines(read_set: tuple[str, ...]) -> list[str]:
    """The select. Named column by column, and never ``*``."""
    named = [f"F.col({column!r})" for column in read_set]
    lines = [
        *_comment(
            "§1.3's authorized read set, named column by column. NEVER a star: a star reads "
            "whatever the table happens to hold today, including a column added AFTER the "
            "authorization — so the group would read more than it was granted, and nothing at all "
            "would say so."),
        "    rows = source.select(",
    ]
    line = "       "
    for index, part in enumerate(named):
        piece = part + ("," if index < len(named) - 1 else ")")
        if len(line) + 1 + len(piece) > 98:
            lines.append(line)
            line = "       "
        line = f"{line} {piece}"
    lines.append(line)
    return lines


def _lag_interval(lag_hours: str) -> tuple[str, str]:
    """``(rendered INTERVAL, prose)`` for a declared ``lag_hours`` — exact, never rounded.

    The lag is a canonical STRING because the governed fact types it as a JSON number and a float
    reaching an identity hash is a rounding decision nobody made. It is read here with ``Decimal``
    for the same reason, and it is rendered in the LARGEST unit that expresses it exactly: ``6``
    hours renders as ``INTERVAL 6 HOURS`` and reads as the declaration, while 21600 seconds reads as
    arithmetic somebody has to check.
    """
    try:
        hours = Decimal(lag_hours)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"the declared availability lag is {lag_hours!r}, which is not a number ({exc}): it is "
            f"rendered as an interval, and a lag nothing can read is a gate that would either not "
            f"render or render as something else") from exc
    if hours.is_nan() or hours.is_infinite() or hours < 0:
        raise ValueError(
            f"the declared availability lag is {lag_hours!r}: a lag says how long after the event a "
            f"row could first be READ, so a negative or non-finite one describes a row available "
            f"before it happened")
    for scale, unit, word in ((1, "HOURS", "hour"), (60, "MINUTES", "minute"),
                              (3600, "SECONDS", "second")):
        amount = hours * scale
        if amount == amount.to_integral_value():
            whole = int(amount)
            return f"INTERVAL {whole} {unit}", f"{whole} {word}{'' if whole == 1 else 's'}"
    raise ValueError(
        f"the declared availability lag {lag_hours!r} hours is not a whole number of seconds: it "
        f"would have to be rounded to render as an interval, and rounding a governed lag is a "
        f"decision this renderer has no authority to make")


def _availability_gate(pit: PitSpec, available: str) -> list[str]:
    """§8 rule 1 — the gate whose violation is INVISIBLE.

    A row kept here that the bank could not yet have read produces a feature that backtests
    beautifully and fails in production, and nothing raises anywhere along the way. So the basis
    decides the comparison explicitly and there is no default branch.
    """
    try:
        basis = AvailabilityBasis(pit.availability_basis)
    except ValueError as exc:
        raise ValueError(
            f"the expression declares the availability basis {pit.availability_basis!r}, which is "
            f"not one AVAILABILITY_TIME governs ({exc}): the vocabulary is CLOSED, and a basis "
            f"nothing recognizes would have to be gated on by guessing what the column means") from exc
    lag_declared = pit.availability_lag_hours is not None
    if (basis is AvailabilityBasis.EVENT_TIME_PLUS_LAG) != lag_declared:
        raise ValueError(
            f"the expression's availability basis is {basis.value!r} and its "
            f"declared lag is {pit.availability_lag_hours!r}: a lag is defined for "
            f"`event_time_plus_lag` and for nothing else, so one basis without it would render a "
            f"gate that admits rows too early and the other with it would apply a lag to an "
            f"arrival time that already includes one")

    head = [
        "    # §8 rule 1 — the availability gate, and the filter whose violation is INVISIBLE: a row",
        "    # kept here that could not yet have been READ at the cutoff raises nothing at all.",
    ]
    if basis is AvailabilityBasis.EVENT_TIME_PLUS_LAG:
        interval, prose = _lag_interval(str(pit.availability_lag_hours))
        return [
            *head,
            *_comment(
                f"Basis {basis.value}: {available!r} holds the EVENT time, and the "
                f"declared lag of {prose} is how long after it the row could first be read. The lag "
                f"is added rather than dropped — dropping it admits every row a whole {prose} early."),
            f"    available_at = F.col({available!r}) + F.expr({interval!r})",
            "    rows = rows.where(available_at <= cutoff)",
        ]
    if basis in (AvailabilityBasis.POSTED_AT, AvailabilityBasis.INGESTED_AT):
        return [
            *head,
            *_comment(
                f"Basis {basis.value}: {available!r} IS the instant the row became "
                f"readable, so it is compared to the cutoff as it stands. It is the GOVERNED "
                f"availability column and deliberately not the event-time column — a row can happen "
                f"long before anyone can see it, which is the entire point of the gate."),
            f"    rows = rows.where(F.col({available!r}) <= cutoff)",
        ]
    raise ValueError(  # pragma: no cover — every member above; here so a NEW one cannot default
        f"the expression's availability basis {basis.value!r} is governed and this renderer has no "
        f"gate for it: the default an unmodelled member falls into is no gate at all — every row "
        f"visible, on every business date")


def _window_boundaries(pit: PitSpec, clock: str, cutoff_zone: str) -> list[str]:
    """§8 rule 2 — ``basis``, ``length``, ``unit`` and BOTH inclusivity flags, each honoured.

    The boundaries are DATES first and instants second. That order is what makes a calendar period
    a calendar period: ``add_months`` steps whole months, so three months before 2026-07-06 is
    2026-04-06 — while ninety days before it is 2026-04-07, and 2026-01-31 plus a month is the 28th
    of February rather than the 3rd of March. It is also what makes the zone conversion right: a
    boundary computed as ``instant - N x 24h`` crosses a DST change wrong, and a local midnight
    converted to UTC does not.
    """
    if not isinstance(pit.window_length, int) or isinstance(pit.window_length, bool):
        raise ValueError(
            f"the declared window length is {pit.window_length!r}, which is not a whole number of "
            f"units: it is rendered into date arithmetic, where anything else is either a syntax "
            f"error or a boundary somebody rounded")
    if pit.window_length < 1:
        raise ValueError(
            f"the declared window length is {pit.window_length}: a window of no units spans no "
            f"time, so the projection would be empty for every entity and every feature would "
            f"evaluate to its empty-window policy with no error anywhere")
    start_op = _START_COMPARISON.get(pit.window_start_inclusive)
    end_op = _END_COMPARISON.get(pit.window_end_inclusive)
    if start_op is None or end_op is None:
        raise ValueError(
            f"the window's boundaries are declared {pit.window_start_inclusive!r} / "
            f"{pit.window_end_inclusive!r}: Inclusivity is CLOSED, and the two flags are carried "
            f"per expression precisely because they vary — assuming a half-open window here would "
            f"move a boundary the declaration states")
    if pit.window_basis not in (_TRAILING, _CALENDAR_PERIOD):
        raise ValueError(
            f"the window's basis is {pit.window_basis!r}: WindowBasis is CLOSED, and the two bases "
            f"anchor the window at DIFFERENT dates — a trailing window ends at the business date "
            f"and a calendar period at the start of the period containing it")

    same_zone = pit.window_timezone.strip() == cutoff_zone.strip()
    zone_note = (
        f"The window's zone is {pit.window_timezone} — the same string as the cutoff's, and stated "
        f"again rather than reused because they are two governed fields that are free to differ."
        if same_zone else
        f"The window's OWN governed zone is {pit.window_timezone}, which is NOT the cutoff's "
        f"({cutoff_zone}). They are separate governed fields and this is the window's.")
    return [
        *_comment(
            f"§8 rule 2 — the declared window: {pit.window_length} "
            f"{_plural(pit.window_length, pit.window_unit)}, {pit.window_basis}. Both boundaries "
            f"are computed as DATES with calendar arithmetic and turned into instants only at the "
            f"end — which is also what keeps a boundary from crossing a daylight-saving change "
            f"wrong, as `instant - N x 24h` would."),
        "    anchor = F.to_date(F.lit(business_date))",
        *_period_end(pit),
        *_period_start(pit),
        "",
        *_comment(
            f"{zone_note} The rendered session is pinned to UTC, so a zone left to be inherited "
            f"moves both boundaries by hours."),
        f"    starts_at = F.to_utc_timestamp(window_start.cast('timestamp'), "
        f"{pit.window_timezone!r})",
        f"    ends_at = F.to_utc_timestamp(window_end.cast('timestamp'), {pit.window_timezone!r})",
        "",
        *_comment(
            f"Both flags as DECLARED: start {pit.window_start_inclusive} ({start_op}), end "
            f"{pit.window_end_inclusive} ({end_op}). Two filters, because they are two boundaries "
            f"— and they are carried per expression precisely because they vary."),
        f"    rows = rows.where(F.col({clock!r}) {start_op} starts_at)",
        f"    rows = rows.where(F.col({clock!r}) {end_op} ends_at)",
    ]


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


def _period_end(pit: PitSpec) -> list[str]:
    """Where the window ENDS — the whole difference between the two bases."""
    if pit.window_basis == _TRAILING:
        return [
            "    # Trailing: the window ends at the business date itself.",
            "    window_end = anchor",
        ]
    period = _PERIOD_START.get(pit.window_unit)
    if period is None:
        if pit.window_unit == "day":
            return [
                *_comment("A calendar period of days: the period a date falls in IS that date, so "
                          "there is nothing to truncate to. The end is the business date."),
                "    window_end = anchor",
            ]
        raise ValueError(
            f"the window's unit is {pit.window_unit!r}: WindowUnit is CLOSED, and a calendar period "
            f"this renderer cannot truncate to has no first day — Spark's `trunc` returns NULL for "
            f"a format it does not know, and a NULL boundary keeps or drops every row silently")
    return [
        *_comment(
            f"A calendar period: the window ends where the CURRENT {pit.window_unit} begins, so the "
            f"incomplete {pit.window_unit} the business date sits in is outside it. That is the "
            f"whole difference from a trailing window, and it is a different set of rows."),
        f"    window_end = F.trunc(anchor, {period!r})",
    ]


def _period_start(pit: PitSpec) -> list[str]:
    """Where the window STARTS — ``length`` units back, in CALENDAR arithmetic."""
    days = _DAYS_PER_UNIT.get(pit.window_unit)
    if days is not None:
        # A day is a day and a week is seven days in every calendar. Counting these is exact; it is
        # a month that is not a fixed number of days, and there is deliberately no day count for one.
        return [
            *_comment(
                f"{pit.window_length} {_plural(pit.window_length, pit.window_unit)} back. A "
                f"{pit.window_unit} is a whole number of days in every calendar, so counting days "
                f"here IS the calendar operation rather than a conversion into one."),
            f"    window_start = F.date_sub(window_end, {pit.window_length * days})",
        ]
    months = _MONTHS_PER_UNIT.get(pit.window_unit)
    if months is None:
        raise ValueError(
            f"the window's unit is {pit.window_unit!r}, which this renderer has no calendar "
            f"arithmetic for: WindowUnit is CLOSED, and the default an unmodelled unit would fall "
            f"into is a day count — the exact conversion §8 rule 2 forbids")
    return [
        *_comment(
            f"{pit.window_length} {_plural(pit.window_length, pit.window_unit)} back, as CALENDAR "
            f"months. A month is NOT thirty days: `add_months` steps whole months and clamps to the "
            f"end of one, so 2026-01-31 back a month is 2026-02-28 where a 30-day count gives "
            f"2026-01-01, and 3 months back from 2026-07-06 is 2026-04-06 where 90 days gives "
            f"2026-04-07. Either conversion moves the boundary and changes which rows are summed."),
        f"    window_start = F.add_months(window_end, {-pit.window_length * months})",
    ]


# ══ §8 rules 3 and 4 — the per-feature calculation ═══════════════════════════════════════════════


def calculation_node_name(feature_column: str) -> str:
    """The Kedro node name for one feature's calculation — how an operator addresses it."""
    return f"calculate_{feature_column}"


def calculation_func_name(feature_column: str) -> str:
    """The function ``nodes.py`` defines and ``pipeline.py`` wires BY NAME. One per FEATURE: §9
    proves completeness per published column, and a column is staged by exactly one node."""
    return calculation_node_name(feature_column)


def render_calculation_node(
    ir: FormulaExecutionIRV1,
    feature: PlannedFeature,
    plan: FeatureGroupPlanV1,
    *,
    empty_window: Mapping[str, EmptyWindowResult],
    null_input: Mapping[str, NullInput],
    projection_datasets: Mapping[str, str],
    spine_dataset: str,
    staging_dataset: str,
    manifest_dataset: str,
) -> RenderedNode:
    """Render the node that computes ONE feature and writes its own §9 evidence.

    Every ``FinalOperation`` renders: an ``IDENTITY`` body is one aggregate staged as the feature, a
    ``RATIO`` is two aggregates divided under the formula's own ``zero_denominator`` policy, and a
    ``DIFFERENCE`` is two aggregates subtracted. The two operands of a final operation are FULL
    aggregates — each has its own governed filter, its own point-in-time projection and its own
    window — so they are supplied and policed per body path rather than as one thing.

    ``empty_window`` and ``null_input`` are REQUIRED keyword arguments with no defaults, and are
    keyed BY BODY PATH. They are §8 rule 4's formula-owned policies, declared on each expression's
    own ``WindowPolicy``, and :class:`~featuregen.materialize.expression_ir.PitSpec` excludes them
    deliberately — so they do not travel on the IR and must be supplied from the formula that
    declared them. A default would be this renderer answering a policy question the formula exists to
    answer: ``ignore`` and ``propagate`` produce different numbers from the same rows, and neither is
    more conservative than the other. One value shared across both operands would be a second guess
    of the same kind: a ratio may declare ``zero`` for its denominator and ``null`` for its
    numerator, and the two produce different columns.

    ``projection_datasets`` is keyed by body path for a sharper reason still. Passed as a sequence,
    a numerator and a denominator differ only by position — and swapping them inverts every value in
    the column while leaving a perfectly plausible number behind. Keyed, the swap is unrepresentable.

    Args:
        ir: the feature's compiled plan. Its expressions are what is rendered, one per body path of
            its ``final_operation``; its ``grain_keys`` are what each aggregate groups by.
        feature: the planned column — its ``ir_hash`` binds the manifest to this computation and its
            ``PhysicalType`` carries §6's rounding and overflow obligations.
        plan: the group's packing list, which decides the landing key columns and the business-date
            column the staged row is keyed by.
        empty_window: per body path, what an entity with NO source rows evaluates to (§8 rule 4).
        null_input: per body path, what a NULL operand value does to that aggregate (§8 rule 4).
        projection_datasets: per body path, the catalog name of that expression's PIT projection
            (Task 13b's output). One entry for an identity body; two for a ratio or a difference.
        spine_dataset: the catalog name of the declared population (Task 13a's output).
        staging_dataset: the catalog name this node writes the feature to.
        manifest_dataset: the catalog name this node writes the staging manifest to.

    Returns:
        A :class:`~featuregen.materialize.render.project.RenderedNode` with TWO outputs — the staged
        frame and its manifest — because §9 proves completeness by manifest and a node that wrote
        only the data would leave the evidence to something that never saw the computation.

    Raises:
        TypeError: an argument is not the type it must be.
        ValueError: the feature cannot be calculated as one node — the plan and the IR describe
            different columns, the compiled body paths are not the ones the final operation has, a
            body path has no declared policy or no projection, a ratio carries no
            ``zero_denominator`` (or a non-ratio carries one), the grain keys are not the landing
            keys, the rounding mode has no exact Spark rendering, or a governed filter names a run
            parameter.
    """
    if not isinstance(ir, FormulaExecutionIRV1):
        raise TypeError(
            f"rendering a calculation needs the compiled FormulaExecutionIRV1, got "
            f"{type(ir).__name__}: the aggregate, the grain and the governed filter are what decide "
            f"the number, and none of them can be inferred from a column name")
    if not isinstance(feature, PlannedFeature):
        raise TypeError(
            f"rendering a calculation needs the PlannedFeature, got {type(feature).__name__}: §6's "
            f"resolved PhysicalType carries the rounding and overflow obligations the rendered code "
            f"must discharge, and a bare type string carries neither")
    if not isinstance(plan, FeatureGroupPlanV1):
        raise TypeError(
            f"rendering a calculation needs the FeatureGroupPlanV1, got {type(plan).__name__}: the "
            f"landing key columns and the business-date column are the plan's, and a staged row "
            f"keyed by anything else has no spine row to land on")
    _dataset(spine_dataset, "the calculation's spine dataset")
    _dataset(staging_dataset, "the calculation's staging dataset")
    _dataset(manifest_dataset, "the calculation's manifest dataset")

    column = _staged_column(ir, feature, plan)
    slots = _body_slots(ir, column, empty_window, null_input, projection_datasets)
    keys = _grain_key_columns(ir, plan, slots)
    physical = feature.physical_type

    body: list[str] = []
    for slot in slots:
        body.extend(_expression_lines(slot, keys, physical))
        body.append("")
    body.extend(_spine_reduction_lines(slots, keys))
    for slot in slots:
        body.append("")
        body.extend(_empty_window_lines(slot, physical))
    final = _final_operation_lines(ir, column, slots, physical)
    if final:
        body.append("")
        body.extend(final)
    body.append("")

    source = "\n".join([
        *_signature_lines(calculation_func_name(column), slots),
        *_calculation_docstring(ir, column, keys, slots),
        *_comment(
            "The business date is read only to BIND the manifest to this run. The staged frame's "
            "own business_dt comes from the spine, which decides which population the row belongs "
            "to — deriving it here as well would be a second answer that could disagree with it."),
        "    business_date = str(business_dt)",
        "",
        *body,
        *_rounding_lines(column, physical),
        *_overflow_lines(column, physical),
        *_staging_shape_lines(column, keys, plan.business_dt_column),
        "",
        *_manifest_lines(column, feature),
    ]) + "\n"

    imports = [_DATAFRAME_IMPORT, _FUNCTIONS_IMPORT, "import hashlib", "import json",
               "import pathlib"]
    if "Decimal(" in source:
        imports.append("from decimal import Decimal")
    if "date.fromisoformat(" in source:
        imports.append("from datetime import date")
    return RenderedNode(
        name=calculation_node_name(column),
        func_name=calculation_func_name(column),
        source=source,
        inputs=(*(slot.dataset for slot in slots), spine_dataset, _BUSINESS_DT_PARAMETER,
                "params:generation_id", "params:run_id", "params:sandbox_execution_hash",
                "params:staging_root"),
        outputs=(staging_dataset, manifest_dataset),
        imports=tuple(sorted(imports)),
        tags=("feature_staging", "calculation"),
    )


def _signature_lines(func_name: str, slots: tuple[_Slot, ...]) -> list[str]:
    """``def calculate_x(…)`` — one parameter per line once there is more than one projection.

    Kedro binds a node's inputs POSITIONALLY against this signature, so the one thing a reviewer of a
    multi-input node must be able to do is count the parameters against the wiring. A ratio's two
    projections differ only by position, and a reader who miscounts them reads the ratio upside down.
    One aggregate needs no such care and keeps the grouping it has, so the identity form is unchanged.
    """
    tail = ") -> tuple[DataFrame, dict]:"
    if len(slots) == 1:
        return [
            f"def {func_name}(",
            f"        {slots[0].parameter}: DataFrame, spine: DataFrame, business_dt: str,",
            "        generation_id: str, run_id: str, sandbox_execution_hash: str,",
            f"        staging_root: str{tail}",
        ]
    parameters = [f"{slot.parameter}: DataFrame" for slot in slots] + [
        "spine: DataFrame", "business_dt: str", "generation_id: str", "run_id: str",
        "sandbox_execution_hash: str", "staging_root: str"]
    return [
        f"def {func_name}(",
        *(f"        {parameter}," for parameter in parameters[:-1]),
        f"        {parameters[-1]}{tail}",
    ]


# ── what the caller must have got right (calculation) ────────────────────────────────────────────


def _staged_column(ir: FormulaExecutionIRV1, feature: PlannedFeature,
                   plan: FeatureGroupPlanV1) -> str:
    """The ONE published column this node stages, proved to be the same one in all three objects."""
    column = hive_identifier(ir.feature_name)
    if feature.column_name != column:
        raise ValueError(
            f"the planned feature is {feature.column_name!r} and the compiled IR computes "
            f"{ir.feature_name!r}: the manifest binds an ir_hash to a column name, and a node built "
            f"from two different features would stage one computation under the other's evidence")
    if column not in {planned.column_name for planned in plan.features}:
        raise ValueError(
            f"the group plan does not publish {column!r} (it publishes "
            f"{sorted(planned.column_name for planned in plan.features)}): §9 checks completeness "
            f"per PLANNED feature, so an output for a column nobody planned is an extra column at "
            f"assembly rather than evidence about this group")
    return column


@dataclass(frozen=True, slots=True)
class _Slot:
    """One operand of the final operation: its expression, its policies and its rendered names.

    A slot exists because the two halves of a ratio are not two views of one thing. Each carries its
    own governed filter, its own point-in-time projection, its own window and its own §8 rule 4
    policies — so every rendered binding a slot produces is namespaced by it, and nothing can be
    computed for one half and read for the other.

    ``value_column`` is where the grain aggregate LANDS. For an identity body that is the published
    column itself; for an operand it is a double-underscored intermediate, which cannot collide with
    a governed column and is dropped before staging so it can never reach the published table.
    """

    expr_path: str
    expression: ExpressionExecutionIR
    dataset: str
    parameter: str
    local: str
    value_column: str
    what: str
    empty: EmptyWindowResult
    nulls: NullInput
    operand: str | None
    read_set: tuple[str, ...]

    @property
    def marker_column(self) -> str:
        """The ``__source_rows`` marker for THIS slot — see :data:`_SOURCE_ROWS`."""
        return _SOURCE_ROWS if not self.local else f"__{self.local}source_rows"

    @property
    def subject(self) -> str:
        """How a rendered comment REFERS to this slot's declarations.

        A single aggregate needs no qualification — there is no other half its policy could be
        confused with — so it keeps the plain form. An operand is named by its body path, because
        that is the only thing that says which half of the operation the sentence is about.
        """
        return "the" if not self.local else f"`{self.expr_path}`'s"


def _body_slots(ir: FormulaExecutionIRV1, column: str,
                empty_window: Mapping[str, EmptyWindowResult],
                null_input: Mapping[str, NullInput],
                projection_datasets: Mapping[str, str]) -> tuple[_Slot, ...]:
    """One slot per body path of ``ir.final_operation``, in slot order — or the refusal saying why not.

    Everything checked here is a way the rendered node would compute a plausible number for the wrong
    reason. A compiled body whose paths are not the operation's would render one operand twice or
    silently drop one; a policy or a projection missing for a path would need a default, and every
    default here is this renderer answering a question §8 rule 4 assigns to the formula.
    """
    if ir.final_operation not in _BODY_SLOTS:
        raise ValueError(
            f"{ir.feature_name!r} declares a {ir.final_operation.value!r} body, and this renderer "
            f"knows the operand slots of {sorted(op.value for op in _BODY_SLOTS)} only: rendering "
            f"an operation whose operands it cannot name would be a guess at which half is which")
    paths = _BODY_SLOTS[ir.final_operation]
    compiled = {expression.expr_path: expression for expression in ir.expressions}
    if len(compiled) != len(ir.expressions) or tuple(sorted(compiled)) != tuple(sorted(paths)):
        raise ValueError(
            f"{ir.feature_name!r} declares a {ir.final_operation.value!r} body, whose operands are "
            f"{list(paths)}, and compiles to {[e.expr_path for e in ir.expressions]}: the body path "
            f"is what says WHICH operand an expression is, so a set that is not the operation's own "
            f"would render one of them twice or drop one and still produce a number")
    _one_zero_denominator(ir)
    empties = _per_path(empty_window, paths, "empty_window", ir.feature_name)
    nulls = _per_path(null_input, paths, "null_input", ir.feature_name)
    datasets = _per_path(projection_datasets, paths, "projection_datasets", ir.feature_name)

    single = len(paths) == 1
    slots = []
    for path in paths:
        leaf = path.rsplit(".", 1)[-1]
        expression = compiled[path]
        read_set = _read_set_columns(expression)
        slots.append(_Slot(
            expr_path=path,
            expression=expression,
            dataset=_dataset(datasets[path], f"the {path} projection dataset"),
            parameter="projection" if single else f"{leaf}_projection",
            local="" if single else f"{leaf}_",
            value_column=column if single else f"__{leaf}",
            what=column if single else f"{path} of {column}",
            empty=EmptyWindowResult(empties[path]),
            nulls=NullInput(nulls[path]),
            operand=_operand_column(expression, read_set),
            read_set=read_set,
        ))
    return tuple(slots)


def _one_zero_denominator(ir: FormulaExecutionIRV1) -> None:
    """``zero_denominator`` is declared for a RATIO and for nothing else (``ir.py``).

    Both directions refuse. A ratio without one has no declared answer for the ÷0 case and the
    renderer would have to choose one; anything else WITH one describes a division that is not in the
    body, so the two objects disagree about what the feature is.
    """
    if ir.final_operation is FinalOperation.RATIO:
        _zero_denominator(ir)
        return
    if ir.zero_denominator is not None:
        raise ValueError(
            f"{ir.feature_name!r} declares a {ir.final_operation.value!r} body and carries a "
            f"zero_denominator policy of {ir.zero_denominator.value!r}: there is no division in "
            f"that body for it to govern, so the compiled objects disagree about what the feature is")


def _zero_denominator(ir: FormulaExecutionIRV1) -> ZeroDenominator:
    """A RATIO's declared ÷0 policy — TOTAL, so every ratio branch reads exactly one answer."""
    if ir.zero_denominator is None:
        raise ValueError(
            f"{ir.feature_name!r} declares a ratio body and carries no zero_denominator policy: §8 "
            f"rule 4 assigns the ÷0 answer to the formula, and null, zero and error are three "
            f"different columns — picking one here would publish this renderer's choice as the "
            f"formula's")
    return ir.zero_denominator


def _per_path(declared: Mapping[str, _Declared], paths: tuple[str, ...], what: str,
              feature_name: str) -> Mapping[str, _Declared]:
    """``declared`` proved to carry exactly ``paths`` — no missing key, and no extra one."""
    if not isinstance(declared, Mapping):
        raise TypeError(
            f"{what} must be a mapping from body path to policy, got {type(declared).__name__}: the "
            f"operands of a final operation declare their own, and one value shared between them "
            f"would apply the numerator's declaration to the denominator")
    missing = [path for path in paths if path not in declared]
    extra = sorted(set(declared) - set(paths))
    if missing or extra:
        raise ValueError(
            f"{what} for {feature_name!r} declares {sorted(declared)} and the body's operands are "
            f"{list(paths)} (missing {missing}, unexpected {extra}): a path with nothing declared "
            f"for it would be rendered under a default, and every default here is this renderer "
            f"answering a question §8 rule 4 assigns to the formula")
    return declared


def _grain_key_columns(ir: FormulaExecutionIRV1, plan: FeatureGroupPlanV1,
                       slots: tuple[_Slot, ...]) -> tuple[str, ...]:
    """The columns the aggregate groups by — the LANDING keys, in the plan's order.

    The grain keys and the landing keys are separate declarations: ``derive_contract`` says a
    feature's own grain columns "may be a different table's spelling of the same entity". Nothing
    declares the mapping between the two spellings, so this renderer requires them to be the SAME
    names and refuses otherwise. Pairing them positionally would be a guess, and a wrong guess
    aliases one key's values under another key's name — which produces a full table of plausible
    numbers keyed to the wrong entities.
    """
    grain = tuple(_column(ref, "a grain key") for ref in ir.grain_keys)
    for slot in slots:
        outside = sorted(set(grain) - set(slot.read_set))
        if outside:
            raise ValueError(
                f"grain key column(s) {outside} are not in {slot.expr_path}'s authorized read set "
                f"{list(slot.read_set)}: the aggregate groups by them and the projection selects "
                f"only the read set, so the node would fail on the cluster reading a column Gate 2 "
                f"never granted")
    landing = tuple(plan.entity_key_columns)
    if sorted(grain) != sorted(landing):
        raise ValueError(
            f"the feature's grain columns {sorted(grain)} are not the plan's landing key columns "
            f"{sorted(landing)}: the two are separate declarations and nothing states how one "
            f"spelling maps onto the other, so joining them would be a guess — and a wrong one "
            f"lands every entity's value under another entity's key with nothing to report it")
    return landing


def _operand_column(expression: ExpressionExecutionIR, read_set: tuple[str, ...]) -> str | None:
    """The aggregate's operand column — ``None`` for ``COUNT_ROWS``, which has none.

    ``operand is None`` IFF ``COUNT_ROWS`` is Child-1's own grammar rule, so both directions are
    checked: an operand under a row count is a column nothing reads, and a missing one under any
    other aggregate is an aggregate with nothing to aggregate.
    """
    operands = sorted({ref.column for ref in expression.physical_read_set
                       if RefRole.OPERAND in ref.roles and ref.column is not None})
    if expression.aggregation is AggregateFunction.COUNT_ROWS:
        if operands:
            raise ValueError(
                f"a count_rows aggregate carries operand column(s) {operands}: Child-1's grammar "
                f"gives it no operand at all, so a read set that names one describes a different "
                f"expression from the one the aggregate says it is")
        return None
    if len(operands) != 1:
        raise ValueError(
            f"the {expression.aggregation.value} aggregate has {len(operands)} operand column(s) "
            f"{operands}: exactly one column is aggregated, and neither zero nor two of them names "
            f"the value this feature publishes")
    outside = [column for column in operands if column not in read_set]
    if outside:
        raise ValueError(
            f"the operand column {outside[0]!r} is not in the authorized read set {list(read_set)}: "
            f"§1.3 authorized a SET of columns, and aggregating one outside it is a read this group "
            f"was never granted")
    return operands[0]

# ── the rendered calculation body ────────────────────────────────────────────────────────────────


def _calculation_docstring(ir: FormulaExecutionIRV1, column: str,
                           keys: tuple[str, ...], slots: tuple[_Slot, ...]) -> list[str]:
    landing = f"({', '.join(_safe_text(key, 'a landing key column') for key in keys)}, business_dt)"
    if len(slots) == 1:
        summary = (
            f"Exactly one row per {landing}, carrying {_safe_text(column, 'the feature column')} and "
            f"nothing else. The aggregate is reduced onto the SPINE with a LEFT join (§8 rule 3), so "
            f"an entity with no source rows is still present and carries this formula's declared "
            f"empty-window value ({slots[0].empty.value}). An inner join here would shrink the "
            f"population and the feature would still look plausible.")
        paragraphs = [summary]
    else:
        declared = ", ".join(f"{slot.expr_path}: {slot.empty.value}" for slot in slots)
        summary = (
            f"Exactly one row per {landing}, carrying {_safe_text(column, 'the feature column')} and "
            f"nothing else. EACH of this body's aggregates is reduced onto the SPINE with its own "
            f"LEFT join (§8 rule 3), so an entity with no source rows is still present and carries "
            f"that expression's own declared empty-window value ({declared}). An inner join here "
            f"would shrink the population and the feature would still look plausible.")
        paragraphs = [summary, _final_operation_note(ir)]
    returns = (
        "Returns the staged frame and its StagingManifestV1 (§9) — both, or neither. A gate below "
        "raises, Kedro writes no output for the node at all, and assembly reports a MISSING "
        "manifest rather than reading a column no manifest describes.")
    wrapped: list[str] = []
    for paragraph in [*paragraphs, returns]:
        wrapped.extend([*(f"    {part}" for part in _wrap(paragraph, 92)), ""])
    return [
        f'    """{_safe_text(ir.feature_name, "the feature name")} for one business date (§8).',
        "",
        *wrapped[:-1],
        '    """',
    ]


def _final_operation_note(ir: FormulaExecutionIRV1) -> str:
    """The docstring paragraph naming the operation between the two operands, and its own policy."""
    if ir.final_operation is FinalOperation.RATIO:
        return (
            f"The final operation is a RATIO, and the formula's declared zero_denominator policy is "
            f"`{_zero_denominator(ir).value}`. A denominator of ZERO and an ABSENT denominator are "
            f"different facts that both look like 'no value' at the end: the first is answered by "
            f"that policy and the second by the denominator's own empty_window declaration, so the "
            f"test below is on the denominator's VALUE and never on the quotient.")
    return (
        "The final operation is a DIFFERENCE. A NULL on either side makes the difference NULL, and "
        "it is deliberately not coalesced: an operand absent from its window has already been "
        "answered by its own empty_window declaration, and substituting a zero here would replace "
        "that answer with this node's — 0 - x is a real, plausible number.")


def _expression_lines(slot: _Slot, keys: tuple[str, ...], physical: PhysicalType) -> list[str]:
    """One operand, end to end: its header, its governed filter and its grain-level aggregate."""
    return [
        *_slot_header(slot),
        *_filter_lines(slot),
        "",
        *_grain_aggregate_lines(slot, keys, physical),
    ]


def _slot_header(slot: _Slot) -> list[str]:
    """The comment naming WHICH operand the block below computes — nothing for a single aggregate.

    A reader of a two-operand node has to keep track of which half they are looking at, and the only
    thing that says so is the body path. It is stated once per block rather than repeated on every
    line, and the identity body emits none: there is no other half to confuse it with.
    """
    if not slot.local:
        return []
    return _comment(
        f"`{slot.expr_path}` — a FULL aggregate with its own governed filter, its own point-in-time "
        f"projection and its own window; the operands of a final operation are separate expressions "
        f"and are not guaranteed to share any of the three. Any literal it needs carries the "
        f"PUBLISHED type, because §6 resolves a type for the published column and none for an "
        f"operand, so that is the only declared type there is to state.")


def _filter_lines(slot: _Slot) -> list[str]:
    """§6's GOVERNED filter, applied before the aggregate — or a comment saying there is none.

    The projection selects the filter's columns (they are in the read set) but applies nothing: it
    decides which rows the expression may SEE, and the filter decides which of those it COUNTS.
    """
    if slot.expression.filter_tree is None:
        return _comment(
            "The expression declares no filter, so every row the point-in-time projection admitted "
            "is aggregated. Named rather than silent: a filter dropped between the compiler and "
            "here would leave exactly this shape and nothing would say which case it was.") + [
            f"    {slot.local}rows = {slot.parameter}",
        ]
    condition = _filter_condition(slot.expression.filter_tree, slot.read_set, "filter")
    condition = _unwrapped(condition)
    return [
        *_comment(
            "§6's governed filter, read from the compiled filter tree — never assembled from text. "
            "It runs BEFORE the aggregate: the projection decided which rows this expression may "
            "SEE, and this decides which of those it counts."),
        f"    {slot.local}rows = {slot.parameter}.where({condition})",
    ]


def _unwrapped(condition: str) -> str:
    """``condition`` without ONE redundant enclosing bracket pair.

    Every subtree renders parenthesized so no composition can regroup it, which leaves the whole
    filter wrapped inside ``where(...)`` where nothing can regroup it either. A reader checking that
    nothing is mis-grouped counts brackets, so the pair that carries no meaning is removed — but
    only after proving the first bracket really is the partner of the last, because ``(a) & (b)``
    also starts with ``(`` and ends with ``)`` and stripping those changes what it means.
    """
    if not condition.startswith("(") or not condition.endswith(")"):
        return condition
    depth = 0
    for index, character in enumerate(condition):
        depth += (character == "(") - (character == ")")
        if depth == 0:
            return condition[1:-1] if index == len(condition) - 1 else condition
    return condition


def _filter_condition(node: object, read_set: tuple[str, ...], path: str) -> str:
    """One filter subtree as a rendered PySpark expression."""
    if not isinstance(node, Mapping):
        raise ValueError(
            f"the compiled filter at {path} is {type(node).__name__} and not a node: the filter "
            f"tree is Child-1's canonical plain form, and anything else is a compilation this "
            f"renderer cannot read")
    kind = node.get("kind")
    if kind == FilterKind.BOOL.value:
        return _filter_bool(node, read_set, path)
    if kind == FilterKind.PREDICATE.value:
        return _filter_predicate(node, read_set, path)
    raise ValueError(
        f"the compiled filter at {path} declares kind {kind!r}, which is outside Child-1's closed "
        f"vocabulary ({FilterKind.BOOL.value}, {FilterKind.PREDICATE.value}): a kind this renderer "
        f"cannot read would otherwise become a filter it silently did not apply")


def _filter_bool(node: Mapping[str, object], read_set: tuple[str, ...], path: str) -> str:
    operator = FilterBoolOp(str(node.get("op")))
    children = node.get("children")
    if not isinstance(children, Sequence) or isinstance(children, str) or not children:
        raise ValueError(
            f"the {operator.value} node at {path} has no children ({children!r}): a boolean over "
            f"nothing is a predicate this renderer would have to invent")
    rendered = [_filter_condition(child, read_set, f"{path}.children[{index}]")
                for index, child in enumerate(children)]
    if operator is FilterBoolOp.NOT:
        if len(rendered) != 1:
            raise ValueError(
                f"the NOT at {path} negates {len(rendered)} children: negation takes exactly one "
                f"operand, and a NOT over several is ambiguous between NOT(a AND b) and "
                f"NOT(a) AND NOT(b) — two different filters")
        return f"(~{rendered[0]})"
    joiner = " & " if operator is FilterBoolOp.AND else " | "
    return f"({joiner.join(rendered)})"


def _filter_predicate(node: Mapping[str, object], read_set: tuple[str, ...], path: str) -> str:
    operator = FilterPredicateOp(str(node.get("op")))
    left = _read_column(str(node.get("left")), read_set, f"the filter predicate at {path}")
    if node.get("right_param") is not None:
        raise ValueError(
            f"the predicate at {path} compares against the run parameter "
            f"{node['right_param']!r}: §11.1's run parameters are a CLOSED set that a formula's own "
            f"parameters are not part of, so nothing would bind it and the rendered node would read "
            f"a name the pipeline never supplies")
    if operator in _NULL_TESTS:
        return f"F.col({left!r}).{_NULL_TESTS[operator]}()"
    if operator in _SET_TESTS:
        members = node.get("right_set")
        if not isinstance(members, Sequence) or isinstance(members, str) or not members:
            raise ValueError(
                f"the {operator.value} predicate at {path} has no right_set ({members!r}): a "
                f"membership test over an empty set keeps nothing (or everything, negated), and "
                f"neither is a filter anybody declared")
        rendered = ", ".join(_filter_literal(member, f"{path}.right_set[{index}]")
                             for index, member in enumerate(members))
        inside = f"F.col({left!r}).isin([{rendered}])"
        return inside if operator is FilterPredicateOp.IN else f"(~{inside})"
    literal = node.get("right_literal")
    if literal is None:
        raise ValueError(
            f"the {operator.value} predicate at {path} has no right_literal: §6 gives a comparison "
            f"exactly one column reference and one typed literal, and a comparison missing its "
            f"right-hand side has nothing to compare against")
    return (f"(F.col({left!r}) {_COMPARISONS[operator]} "
            f"{_filter_literal(literal, f'{path}.right_literal')})")


def _filter_literal(literal: object, path: str) -> str:
    """One ``TypedLiteral`` as a rendered Python value, TYPED — never as its canonical string.

    Child-1 carries every literal value as a canonical string (§A) so an identity hash never sees a
    float. Rendering that string straight into a comparison would compare a governed decimal against
    text, which Spark resolves by coercing one side — silently, and not always the same way.
    """
    if not isinstance(literal, Mapping):
        raise ValueError(f"the literal at {path} is {type(literal).__name__} and not a typed one")
    kind = LiteralType(str(literal.get("type")))
    value = literal.get("value")
    if not isinstance(value, str):
        raise ValueError(
            f"the literal at {path} carries {value!r}: Child-1 canonicalizes every literal value to "
            f"a STRING, and a value that is not one did not come through that canonicalization")
    if kind is LiteralType.STRING:
        return repr(value)
    if kind is LiteralType.BOOLEAN:
        if value not in {"true", "false"}:
            raise ValueError(
                f"the boolean literal at {path} is {value!r}, and Child-1's canonical booleans are "
                f"'true' and 'false': anything else would render as a truthy string")
        return "True" if value == "true" else "False"
    if kind is LiteralType.INTEGER:
        try:
            return repr(int(value))
        except ValueError as exc:
            raise ValueError(f"the integer literal at {path} is {value!r} ({exc})") from exc
    if kind is LiteralType.DECIMAL:
        try:
            Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"the decimal literal at {path} is {value!r} ({exc})") from exc
        return f"Decimal({value!r})"
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"the date literal at {path} is {value!r} ({exc})") from exc
    return f"date.fromisoformat({value!r})"


def _grain_aggregate_lines(slot: _Slot, keys: tuple[str, ...],
                           physical: PhysicalType) -> list[str]:
    """The grain-level aggregate §8 rule 3 reduces onto the spine, plus rule 4's null policy.

    The two comments are deliberately on their own statements. Stacked above one line they read as
    a single paragraph that changes subject halfway, and a reader then has to work out which claim
    describes what — which is exactly how a comment ends up describing a line it is not about.
    """
    aggregated = [f"{slot.local}aggregate.alias({slot.value_column!r})"]
    if slot.empty is not EmptyWindowResult.NULL:
        aggregated.append(f"F.count(F.lit(1)).alias({slot.marker_column!r})")
    grouping = ", ".join(f"F.col({key!r})" for key in keys)
    return [
        *_comment(_null_input_note(slot)),
        *_aggregate_expression(slot, physical),
        "",
        *_comment(
            f"The grain-level aggregate. `{slot.expression.aggregation.value}` is the expression's "
            f"own declared aggregate, and the grouping is the DECLARED grain — one row per landing "
            f"key, which is what the spine reduction below can then join onto exactly once."),
        *_call_lines(f"    {slot.local}grouped = {slot.local}rows.groupBy({grouping}).agg(",
                     aggregated, ")"),
    ]


def _call_lines(head: str, arguments: list[str], tail: str) -> list[str]:
    """``head(arg, arg)tail`` on one line where it fits, one argument per line where it does not.

    Splitting a call that fits makes a reader look for the reason it was split, and the reason is
    the first thing they will assume is significant. The width is fixed, so the choice depends on
    nothing but the rendered names — the bytes cannot move with the machine that rendered them.
    """
    single = f"{head}{', '.join(arguments)}{tail}"
    if len(single) <= _RENDERED_WIDTH:
        return [single]
    indent = " " * (len(head) - len(head.lstrip()) + 4)
    return [head, *(f"{indent}{argument}," for argument in arguments), f"{' ' * 4}{tail}"]


def _aggregate_expression(slot: _Slot, physical: PhysicalType) -> list[str]:
    """``aggregate = …`` — a NAMED binding, because the propagate form is two aggregates deep."""
    aggregation, operand, nulls = slot.expression.aggregation, slot.operand, slot.nulls
    name = f"{slot.local}aggregate"
    if aggregation is AggregateFunction.COUNT_ROWS:
        return [f"    {name} = F.count(F.lit(1))"]
    value = f"F.col({operand!r})"
    if nulls is NullInput.ZERO:
        value = f"F.coalesce({value}, {_typed_literal('0', physical)})"
    core = f"{_AGGREGATE_CALLS[aggregation]}({value})"
    if nulls is not NullInput.PROPAGATE:
        return [f"    {name} = {core}"]
    # `count(x)` skips nulls and `count(1)` does not, so a shortfall between them IS "some value in
    # this group was null". Spark's aggregates all skip nulls, so propagation has to be rendered.
    marker = f"{slot.local}any_null"
    absent = f"    {marker} = F.count(F.col({operand!r})) < F.count(F.lit(1))"
    single = f"    {name} = F.when({marker}, {_typed_literal('None', physical)}).otherwise({core})"
    if len(single) <= _RENDERED_WIDTH:
        return [absent, single]
    return [
        absent,
        f"    {name} = F.when({marker}, {_typed_literal('None', physical)}).otherwise(",
        f"        {core})",
    ]


def _typed_literal(value: str, physical: PhysicalType) -> str:
    """``F.lit(x)`` carrying the PUBLISHED type explicitly.

    An untyped literal in a ``when``/``coalesce`` leaves Spark to resolve a common type between it
    and the aggregate — ``F.lit(0)`` is an INT beside a ``DECIMAL(38,6)``, and ``F.lit(None)`` is a
    NullType beside anything. Both resolutions are probably what anyone would want, and "probably"
    on the type of the published column is the kind of thing that changes a number quietly. The
    declared type is available here, so it is stated.
    """
    return f"F.lit({value}).cast({physical.sql_type.lower()!r})"


def _null_input_note(slot: _Slot) -> str:
    aggregation, nulls, operand = slot.expression.aggregation, slot.nulls, slot.operand
    if aggregation is AggregateFunction.COUNT_ROWS:
        return (f"§8 rule 4 — the declared null_input is `{nulls.value}` and this aggregate has no "
                f"operand at all, so there is no value for it to act on. Stated rather than "
                f"skipped: a policy silently not applied reads exactly like one that was.")
    if nulls is NullInput.IGNORE:
        return (f"§8 rule 4 — the declared null_input is `ignore`, which is what Spark's aggregates "
                f"do: a NULL {operand} is skipped. Nothing is rendered for it, so this comment is "
                f"the evidence that the policy was READ rather than defaulted to.")
    if nulls is NullInput.ZERO:
        return (f"§8 rule 4 — the declared null_input is `zero`, so a NULL {operand} is COUNTED as "
                f"0 rather than skipped. For a count that changes the answer: a null row now "
                f"contributes, which is what the declaration asks for.")
    return (f"§8 rule 4 — the declared null_input is `propagate`, so ONE null {operand} anywhere in "
            f"the group makes the whole aggregate NULL. Spark's aggregates skip nulls, so this is "
            f"rendered explicitly; it is not what any of them does on its own.")


def _spine_reduction_lines(slots: tuple[_Slot, ...], keys: tuple[str, ...]) -> list[str]:
    """§8 rule 3 — LEFT JOIN onto the spine. The one line the whole population depends on.

    One paragraph and one line per aggregate, deliberately. A join is the statement a reviewer must
    be able to check at a glance, and ``how='left'`` on a continuation line is the half of it that
    would be read last. Each operand joins separately because each is its own grain-level aggregate;
    neither can fan out, because ``groupBy(...).agg(...)`` produces one row per group by
    construction and the spine holds one row per key by §4.2 rule 5's blocking gate.
    """
    lines = [
        *_comment(
            "§8 rule 3 — the spine reduction. LEFT, and never INNER: an entity with no source rows "
            "in this window must still be present, carrying the declared empty-window value, and "
            "an inner join would silently drop it — the population shrinks, every downstream count "
            "moves with it, and the feature still looks plausible. It cannot inflate either: the "
            "spine holds one row per (keys…, business_dt) (§4.2 rule 5 makes a duplicate there a "
            "blocking gate) and the aggregate holds one row per key. Nothing de-duplicates here; "
            "fan-out is refused upstream, and a row-collapsing repair in the renderer is how row "
            "inflation becomes invisible."),
    ]
    for index, slot in enumerate(slots):
        left = "spine" if index == 0 else "staged"
        lines.extend(_call_lines(f"    staged = {left}.join(",
                                 [f"{slot.local}grouped", f"on={list(keys)!r}", "how='left'"], ")"))
    return lines


def _empty_window_lines(slot: _Slot, physical: PhysicalType) -> list[str]:
    """§8 rule 4's empty-window value, told apart from a null the AGGREGATE produced.

    The marker column is why the two are distinguishable at all. After a left join, an entity with
    no rows and an entity whose aggregate evaluated to NULL both show NULL — and they are different
    facts with different declared answers.

    Resolved PER OPERAND and BEFORE the final operation, because that is where the declaration sits:
    each ``AggregateExpression`` carries its own ``WindowPolicy``. The final operation then combines
    whatever the two declarations produced, and never re-answers either.
    """
    column, marker = slot.value_column, slot.marker_column
    if slot.empty is EmptyWindowResult.NULL:
        return _comment(
            f"§8 rule 4 — {slot.subject} declared empty_window is `null`, which is what the LEFT "
            f"join already leaves for an entity with no rows. No marker column is rendered because "
            f"none is needed: a null from an empty window and a null from the aggregate are the "
            f"same declared answer here, so nothing has to tell them apart.")
    absent = f"{slot.local}no_source_rows"
    lines = [f"    {absent} = F.col({marker!r}).isNull()"]
    if slot.empty is EmptyWindowResult.ZERO:
        value = f"{slot.local}empty_value"
        body = [
            *_comment(
                f"§8 rule 4 — {slot.subject} declared empty_window is `zero`. The marker is what "
                f"distinguishes an entity with NO rows from one whose aggregate evaluated to NULL: "
                f"both are null after the join, and only the first is an empty window. Coalescing "
                f"instead would answer the second one's policy question with this one's answer."),
            *lines,
            f"    {value} = {_typed_literal('0', physical)}",
            "    staged = staged.withColumn(",
            f"        {column!r},",
            f"        F.when({absent}, {value}).otherwise(F.col({column!r})))",
        ]
    else:
        empty = f"{slot.local}empty"
        body = [
            *_comment(
                f"§8 rule 4 — {slot.subject} declared empty_window is `error`, so an entity with no "
                f"rows in the window STOPS the run. The marker is what makes that decidable: after "
                f"the join an empty window and a null aggregate look identical, and only one of "
                f"them is what the formula declared an error."),
            *_comment(
                "This is the FORMULA's policy, not a §9 validation gate, so it deliberately carries "
                "no ValidationGateCode: §14's vocabulary has no member for it, and borrowing one "
                "would put this decision into the gate table's mouth."),
            *lines,
            f"    {empty} = staged.where({absent})",
            f"    if {empty}.limit(1).count() > 0:",
            *_raise_lines(
                f"{_safe_text(slot.what, 'the feature column')} declares empty_window=error, and "
                f"at least one entity in the declared population has no source row in its window. "
                f"The formula asks for the run to stop rather than for a value to be invented for "
                f"it. Entities affected: ",
                tail=f"str({empty}.count())"),
        ]
    return [*body, "", *_comment(
        "The marker is dropped here: per-feature staging carries the keys, the business date and "
        "ONE feature column, and a column this node invented would collide at assembly."),
        f"    staged = staged.drop({marker!r})"]


def _final_operation_lines(ir: FormulaExecutionIRV1, column: str, slots: tuple[_Slot, ...],
                           physical: PhysicalType) -> list[str]:
    """The operation BETWEEN the operands — nothing at all for an identity body.

    It runs last of the compute, after each operand's own §8 rule 4 policies have been applied, and
    it never re-answers one of them. That ordering is the whole design: an operand absent from its
    window has already been given the value its declaration asks for, and the operation combines
    whatever the two declarations produced.
    """
    if ir.final_operation is FinalOperation.IDENTITY:
        return []
    values = [f"    {slot.local}value = F.col({slot.value_column!r})" for slot in slots]
    names = [f"{slot.local}value" for slot in slots]
    if ir.final_operation is FinalOperation.DIFFERENCE:
        minuend, subtrahend = names
        body = [
            *_comment(
                "The DIFFERENCE. A NULL on either side makes it NULL, and it is deliberately NOT "
                "coalesced: an operand absent from its window was already answered by its own "
                "empty_window declaration above, and substituting a zero here would replace that "
                "answer with this node's — `0 - x` is a real, plausible number and nothing "
                "downstream would question it."),
            *values,
            *_call_lines("    staged = staged.withColumn(",
                         [repr(column), f"{minuend} - {subtrahend}"], ")"),
        ]
        return [*body, "", *_operand_drop_lines(slots)]
    numerator, denominator = names
    policy = _zero_denominator(ir)
    body = [
        *_comment(
            f"§8 rule 4's ÷0 half — the declared zero_denominator is `{policy.value}`. The test is "
            f"on the DENOMINATOR's value and never on the quotient: a zero denominator and an "
            f"ABSENT one are different facts that both look like 'no value' at the end, the second "
            f"was already answered by {denominator}'s own empty_window declaration above, and a "
            f"division answers both with the same NULL. Reading that back would settle one policy's "
            f"question with the other's answer."),
        *_comment(
            "The literal carries no cast. The denominator is an OPERAND, and §6 resolves a type for "
            "the published column only — so there is no declared operand type for a cast to state, "
            "and a comparison against zero is exact in every numeric type Spark would promote to."),
        *values,
        f"    denominator_is_zero = {denominator}.isNotNull() & ({denominator} == F.lit(0))",
        *_zero_denominator_lines(policy, column, numerator, denominator, physical),
    ]
    return [*body, "", *_operand_drop_lines(slots)]


def _zero_denominator_lines(policy: ZeroDenominator, column: str, numerator: str,
                            denominator: str, physical: PhysicalType) -> list[str]:
    """One rendering per ``ZeroDenominator`` member — three genuinely different columns.

    ``null`` and ``zero`` replace the divisor BEFORE the division rather than repairing the quotient
    afterwards, so no zero ever reaches a ``/``. Non-ANSI Spark would answer ``x / 0`` with a NULL
    and ANSI Spark raises, which means leaving the division unguarded would make the DECLARED policy
    depend on a session setting — the ``null`` case would be right by accident today and become an
    error the day someone enables ANSI.

    ``error`` genuinely RAISES. `error` is not a mode Spark has for this either: it would return the
    same NULL that the `null` policy asks for, so a formula that declared "stop the run" would
    publish a column of nulls and report nothing.
    """
    if policy is ZeroDenominator.ERROR:
        return [
            *_comment(
                "Rendered as a real refusal. `error` is not a mode Spark has: division by zero "
                "returns the same NULL that the `null` policy asks for, so a formula declaring "
                "that the run should STOP would otherwise publish a column of nulls and report "
                "nothing. It is the FORMULA's policy and not a §9 validation gate, so it carries "
                "no ValidationGateCode — §14's vocabulary has no member for it."),
            "    divided_by_zero = staged.where(denominator_is_zero)",
            "    if divided_by_zero.limit(1).count() > 0:",
            *_raise_lines(
                f"{_safe_text(column, 'the feature column')} declares zero_denominator=error, and "
                f"at least one entity's denominator is exactly zero. An ABSENT denominator is not "
                f"this: it was answered by the empty_window policy above and is not reported here. "
                f"Entities affected: ",
                tail="str(divided_by_zero.count())"),
            *_comment(
                "The division is unguarded because the check above proved there is nothing to "
                "guard against: no denominator reaching this line is zero."),
            *_call_lines("    staged = staged.withColumn(",
                         [repr(column), f"{numerator} / {denominator}"], ")"),
        ]
    lines = [
        f"    undefined = {_typed_literal('None', physical)}",
        f"    divisor = F.when(denominator_is_zero, undefined).otherwise({denominator})",
    ]
    if policy is ZeroDenominator.NULL:
        return [*lines, *_call_lines("    staged = staged.withColumn(",
                                     [repr(column), f"{numerator} / divisor"], ")")]
    return [
        *lines,
        f"    zero_value = {_typed_literal('0', physical)}",
        "    staged = staged.withColumn(",
        f"        {column!r},",
        f"        F.when(denominator_is_zero, zero_value).otherwise({numerator} / divisor))",
    ]


def _operand_drop_lines(slots: tuple[_Slot, ...]) -> list[str]:
    """The two operand columns, dropped once the operation has consumed them."""
    dropped = ", ".join(repr(slot.value_column) for slot in slots)
    return [
        *_comment(
            "The operand columns are dropped: per-feature staging carries the keys, the business "
            "date and ONE feature column, and the two halves of an operation the caller never asked "
            "for would be extra columns §9 reports against the whole group at assembly."),
        f"    staged = staged.drop({dropped})",
    ]


def _decimal_scale(sql_type: str) -> tuple[int, int]:
    """``(precision, scale)`` of a published ``DECIMAL(p,s)``, or the refusal saying it is not one."""
    matched = _DECIMAL_SQL_TYPE.fullmatch(sql_type.strip())
    if matched is None:
        raise ValueError(
            f"the published type {sql_type!r} carries a rounding or overflow obligation and is not "
            f"a DECIMAL(p,s): §6 attaches both to decimal arithmetic only, and applying either to "
            f"another type would be an instruction this renderer invented")
    return int(matched.group(1)), int(matched.group(2))


def _rounding_lines(column: str, physical: PhysicalType) -> list[str]:
    """§6 — rounding is EXPLICIT. Never left to whatever the engine happens to do.

    An integral published type gets ONE comment covering both this obligation and the overflow one
    (:func:`_overflow_lines` then emits none), rather than two blocks each saying that nothing is
    done. Seven lines of prose stating twice that nothing happens reads as generated boilerplate,
    and a comment a reviewer skims is a comment that can be wrong without anyone noticing.
    """
    if physical.rounding is None:
        return [*_comment(
            f"§6 — the published type is {_safe_text(physical.sql_type, 'the published type')}, "
            f"which is integral. Neither of §6's decimal obligations applies to it: there is no "
            f"decimal arithmetic for a rounding mode to govern, and no decimal overflow to check "
            f"for. A rounding mode recorded against it would be an instruction this node could act "
            f"on wrongly."), ""]
    if physical.rounding not in _ROUNDING_CALLS:
        raise ValueError(
            f"the formula declares {physical.rounding.value!r} rounding, and no Spark function "
            f"implements it exactly: `round` is HALF_UP and `bround` is HALF_EVEN, and the other "
            f"four would have to be composed from floor/ceil over a scaled value — which is this "
            f"renderer deciding a rounding rule rather than applying the declared one")
    _precision, scale = _decimal_scale(physical.sql_type)
    return [
        *_comment(
            f"§6 — rounding is applied EXPLICITLY, from the formula's own declared "
            f"`{physical.rounding.value}` mode, and never inherited from an engine default. "
            f"`{_ROUNDING_CALLS[physical.rounding]}` is Spark's function for exactly that mode — "
            f"{_ROUNDING_NOTES[physical.rounding]} A different mode would move every tie in this "
            f"column, and an engine default states no mode at all."),
        *_call_lines(
            "    staged = staged.withColumn(",
            [repr(column), f"{_ROUNDING_CALLS[physical.rounding]}(F.col({column!r}), {scale})"],
            ")"),
        "",
    ]


def _overflow_lines(column: str, physical: PhysicalType) -> list[str]:
    """§9's ``OVERFLOW_VIOLATION`` — the check that turns Spark's silent NULL into a refusal."""
    if physical.overflow is None:
        # Nothing at all: `_rounding_lines` already said so for both obligations at once. A second
        # block here would state it twice, and the first draft's version pointed at "the check
        # below" — which is not in the file when this branch is taken.
        return []
    if physical.overflow is not OverflowBehavior.ERROR:
        raise ValueError(
            f"the formula declares {physical.overflow.value!r} overflow: nothing in this slice "
            f"clamps a decimal, so rendering it would substitute different overflow semantics for "
            f"the declared ones. `physical_types` refuses it at §6 and this renderer will not "
            f"quietly succeed where that refused")
    precision, scale = _decimal_scale(physical.sql_type)
    return [
        *_comment(
            f"§9 OVERFLOW_VIOLATION. The formula declares `error` on overflow, and `error` is not a "
            f"mode Spark has: its default is to return NULL for a value that does not fit "
            f"DECIMAL({precision},{scale}). So the cast is compared against the value that went "
            f"into it — a row that was NOT null and became null overflowed, and a NULL silently "
            f"replacing a number is exactly what the declaration refuses."),
        f"    typed = F.col({column!r}).cast('decimal({precision},{scale})')",
        *_comment(
            "A null that was ALREADY null passes: that is the empty-window or null-input policy's "
            "own answer, not an overflow, and reporting it here would fire the wrong gate."),
        f"    overflowed = staged.where(F.col({column!r}).isNotNull() & typed.isNull())",
        "    if overflowed.limit(1).count() > 0:",
        *_refuse(
            ValidationGateCode.OVERFLOW_VIOLATION,
            f"{_safe_text(column, 'the feature column')} produced at least one value that does not "
            f"fit its declared "
            f"{_safe_text(physical.sql_type, 'the published type')}. The formula declares "
            f"overflow=error, so the run stops rather than publishing the NULL Spark would "
            f"otherwise substitute. Rows affected: ",
            tail="str(overflowed.count())"),
        f"    staged = staged.withColumn({column!r}, typed)",
        "",
    ]


def _staging_shape_lines(column: str, keys: tuple[str, ...], business_dt_column: str) -> list[str]:
    """§10.2 — ``(keys…, business_dt, one feature column)``, and nothing else."""
    selected = [f"F.col({name!r})" for name in (*keys, business_dt_column, column)]
    return [
        *_comment(
            "§10.2 — per-feature staging carries the landing keys, the business date and ONE "
            "feature column, and nothing else. The three system columns are added ONCE, at "
            "assembly: one per staging output would collide there, and any other column this node "
            "invented would be an extra column §9 reports against the whole group. The business "
            "date is the SPINE's — it decides which population the row belongs to, and re-deriving "
            "it from the run parameter would be a second answer that could disagree with the row."),
        *_call_lines("    staged = staged.select(", selected, ")"),
    ]


def _manifest_lines(column: str, feature: PlannedFeature) -> list[str]:
    """§9's per-feature evidence — counts, hashes and locations only (``StagingManifestV1``).

    ``generated_project_hash`` is READ from ``GENERATED.lock`` at run time. §7 excludes it from
    every other generated file because the hash is taken over those files, so a literal here would
    either state a hash that could not yet be known or feed an older one back into the bytes.
    """
    location = "/" + feature_staging_path(column)
    return [
        *_comment(
            "§9's evidence. `generated_project_hash` is read from the lock AT RUN TIME: §7 keeps it "
            "out of every other generated file, because the hash is taken OVER those files, so a "
            "literal here would be a value the hash is computed from."),
        f"    project_root = pathlib.Path(__file__).resolve().parents[{_LOCK_DEPTH}]",
        f"    lock = json.loads("
        f"(project_root / {GENERATED_LOCK_FILENAME!r}).read_text(encoding='utf-8'))",
        "",
        *_comment(
            "The staged output's own column NAMES, plus the type THIS compilation declared for the "
            "feature column. It is not a read of the physical dtype: §9 compares types against "
            "`expected_schema` on the ASSEMBLED group, where every column of the published row is "
            "present, and a second per-feature answer would be a second verdict on one question."),
        "    schema_hash = hashlib.sha256(json.dumps(",
        "        {'columns': staged.columns,",
        f"         'feature': [{column!r}, {feature.physical_type.sql_type!r}, "
        f"{feature.physical_type.nullable!r}]" + "},",
        "        separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()",
        "",
        *_comment(
            "Where the output went. Composed from the `staging_root` run parameter and the "
            "catalog's OWN relative path for this dataset, so the manifest cannot name a path "
            "nothing wrote to. The root is a run parameter because §9's staging area is "
            "generation-scoped: one fixed at render time would be shared by every run."),
        f"    output_location = str(staging_root).rstrip('/') + {location!r}",
        "",
        *_comment(
            "No data value appears in a manifest and none may be added (§14): counts, types, hashes "
            "and locations only. The generation/run/date binding is what makes a reused staging "
            "path detectable — an older SUCCESSFUL manifest whose ir_hash still matches would "
            "otherwise publish stale output past every other check (§9)."),
        "    manifest = {",
        f"        'intent_feature_name': {column!r},",
        f"        'ir_hash': {feature.ir_hash!r},",
        "        'generation_id': str(generation_id),",
        "        'run_id': str(run_id),",
        "        'business_dt': business_date,",
        "        'generated_project_hash': lock['generated_project_hash'],",
        "        'sandbox_execution_hash': str(sandbox_execution_hash),",
        "        'output_location': output_location,",
        "        'schema_hash': schema_hash,",
        "        'row_count': staged.count(),",
        f"        'status': {StagingStatus.COMPLETED.value!r},",
        "    }",
        "    return staged, manifest",
    ]

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

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import MaterializationContractV1
from featuregen.materialize.expression_ir import (
    AvailabilityBasis,
    ExpressionExecutionIR,
    PitSpec,
    RefRole,
)
from featuregen.materialize.group_plan import FeatureGroupPlanV1
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import (
    EventTimePartition,
    PartitionTransform,
    StaticSnapshot,
)
from featuregen.materialize.render.project import RenderedNode, slug
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
    "projection_func_name",
    "projection_node_name",
    "render_projection_node",
    "render_spine_node",
]

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
    wrapped = _wrap(f"{code.value}: {text}", 84)
    inner = indent + "    "
    parts = [f'{inner}"{part} "' for part in wrapped[:-1]] + [f'{inner}"{wrapped[-1]}"']
    if tail is not None:
        # The space belongs INSIDE the literal: `"…for:" + repr(x)` renders `for:'2020-01-01'`.
        parts[-1] = f'{inner}"{wrapped[-1]} " + {tail}'
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
        *_window_boundaries(pit, clock),
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
        "    # §1.3's authorized read set, named column by column. NEVER a star: a star reads",
        "    # whatever the table happens to hold today, including a column added AFTER the",
        "    # authorization — so the group would read more than it was granted, and nothing would",
        "    # say so.",
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
        "    # §8 rule 1 — the availability gate. This is the filter whose violation is INVISIBLE:",
        "    # a row kept here that could not yet have been READ at the cutoff produces a feature",
        "    # that backtests beautifully and is wrong in production, and nothing raises.",
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


def _window_boundaries(pit: PitSpec, clock: str) -> list[str]:
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

    lines = [
        "    # §8 rule 2 — the window. Its boundaries are computed as DATES with calendar",
        "    # arithmetic and turned into instants only at the end. A month is NOT thirty days:",
        "    # three months before 2026-07-06 is 2026-04-06 and ninety days before it is",
        "    # 2026-04-07, so a day-count conversion moves the boundary and changes the answer.",
        "    anchor = F.to_date(F.lit(business_date))",
        *_period_end(pit),
        *_period_start(pit),
        "",
        *_comment(
            f"The window's OWN governed zone is {pit.window_timezone} — a different field from the "
            f"cadence's cutoff zone, and stated here for the same reason: the rendered session is "
            f"pinned to UTC, so a zone left to be inherited moves both boundaries by hours."),
        "    starts_at = F.to_utc_timestamp(",
        f"        window_start.cast('timestamp'), {pit.window_timezone!r})",
        "    ends_at = F.to_utc_timestamp(",
        f"        window_end.cast('timestamp'), {pit.window_timezone!r})",
        "",
        *_comment(
            f"Both flags as DECLARED: start {pit.window_start_inclusive} ({start_op}), end "
            f"{pit.window_end_inclusive} ({end_op}). Two filters, because they are two boundaries."),
        f"    rows = rows.where(F.col({clock!r}) {start_op} starts_at)",
        f"    rows = rows.where(F.col({clock!r}) {end_op} ends_at)",
    ]
    return lines


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
                "    # A calendar period of days: the period a date falls in IS that date, so there",
                "    # is nothing to truncate to. The end is the business date.",
                "    window_end = anchor",
            ]
        raise ValueError(
            f"the window's unit is {pit.window_unit!r}: WindowUnit is CLOSED, and a calendar period "
            f"this renderer cannot truncate to has no first day — Spark's `trunc` returns NULL for "
            f"a format it does not know, and a NULL boundary keeps or drops every row silently")
    return [
        f"    # A calendar period: the window ends where the CURRENT {pit.window_unit} begins, so",
        f"    # the incomplete {pit.window_unit} the business date sits in is outside it. That is",
        "    # the whole difference from a trailing window, and it is a different set of rows.",
        f"    window_end = F.trunc(anchor, {period!r})",
    ]


def _period_start(pit: PitSpec) -> list[str]:
    """Where the window STARTS — ``length`` units back, in CALENDAR arithmetic."""
    days = _DAYS_PER_UNIT.get(pit.window_unit)
    if days is not None:
        # A day is a day and a week is seven days in every calendar. Counting these is exact; it is
        # a month that is not a fixed number of days, and there is deliberately no day count for one.
        return [
            f"    # {pit.window_length} {pit.window_unit}(s) back. A {pit.window_unit} is a whole "
            f"number of days in",
            "    # every calendar, so counting days here is the calendar operation, not a shortcut.",
            f"    window_start = F.date_sub(window_end, {pit.window_length * days})",
        ]
    months = _MONTHS_PER_UNIT.get(pit.window_unit)
    if months is None:
        raise ValueError(
            f"the window's unit is {pit.window_unit!r}, which this renderer has no calendar "
            f"arithmetic for: WindowUnit is CLOSED, and the default an unmodelled unit would fall "
            f"into is a day count — the exact conversion §8 rule 2 forbids")
    return [
        f"    # {pit.window_length} {pit.window_unit}(s) back, as CALENDAR months: `add_months`",
        "    # steps whole months and clamps to the end of the month, so 2026-01-31 back one month",
        "    # is 2026-02-28. Subtracting a day count instead would land on 2026-01-01.",
        f"    window_start = F.add_months(window_end, {-pit.window_length * months})",
    ]

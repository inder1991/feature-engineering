"""Spec §4.2 / §8 — the COMPUTE nodes of the generated pipeline. Task 13a renders the spine.

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
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import MaterializationContractV1
from featuregen.materialize.group_plan import FeatureGroupPlanV1
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import (
    EventTimePartition,
    PartitionTransform,
    StaticSnapshot,
)
from featuregen.materialize.render.project import RenderedNode
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
        lines += [
            f"    # §8's cutoff: the business date at {cutoff_time} in {cutoff_zone}. The rendered",
            "    # session timezone is pinned to UTC, so the governed zone is STATED here rather",
            "    # than inherited — a cutoff computed in the wrong zone moves silently.",
            "    cutoff = F.to_utc_timestamp(",
            f"        F.to_timestamp(F.lit(business_date + {' ' + cutoff_time!r})), {cutoff_zone!r})",
            "",
        ]
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

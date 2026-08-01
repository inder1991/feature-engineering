"""Spec §3.3 / §11.1 — RUN preparation: the exact partitions a run reads, and its execution identity.

**This is where ``business_dt`` first appears in the program**, and the whole point of the module is
that it appears *here* and nowhere earlier. :mod:`featuregen.materialize.inputs` derives the STATIC
:class:`PhysicalInputRequirement` — partition COLUMNS and the DECLARED mapping — which enters
``ir_hash`` and :class:`CompilationIdentity`. This module resolves that requirement against a
business date and a live metastore into a :class:`PhysicalInputSnapshot`, which enters
``sandbox_execution_hash`` and nothing else.

The two rules pull in opposite directions and are both load-bearing:

* **snapshots must not move ``ir_hash``** — a generated project that changed every business date
  would not be a hash-identified artifact, it would be a daily rebuild wearing one's name. Nothing
  here writes to an IR, and a snapshot's only compilation-side field is the requirement it was
  resolved FROM, whose payload ``ir_hash`` already covers;
* **``sandbox_execution_hash`` must move with ``business_dt``** — §9's staging manifests bind an
  output to a generation, a run *and* a date, so two runs over two dates that shared an execution
  identity could not be told apart. The date is hashed directly, not merely through the partitions
  it happens to select: over a verified-unpartitioned input the two runs resolve identical
  partition sets, and their execution hashes must still differ.

**Snapshots resolve PER EXPRESSION.** A group holds a 30-day feature, two 90-day features and a
ratio whose two expressions may read different tables with different windows and different
availability bases, so a single ``window`` argument cannot describe those reads (§3.3). Run
preparation therefore takes one :class:`RunInputRequest` per expression, each carrying its own
``PitSpec``, and returns snapshots keyed by ``(feature_name, expr_path, requirement_id)``.
De-duplication happens **after** semantics are compared: :meth:`PhysicalInputSnapshot.snapshot_id`
covers the requirement, the resolved partitions and the business date, so two reads of one table
under two windows can never collapse — collapsing them is how one expression silently inherits
another's window.

**``business_dt`` is never assumed to BE the partition column.** A table partitioned on ``load_dt``
is read through its DECLARED mapping (§3.4). A 30-day window over a one-day ``EVENT_TIME_PARTITION``
mapping resolves the window's own 30 dates — not the business date — and an
``AVAILABILITY_PARTITION`` mapping resolves MORE than the window's days, because a transaction that
happened inside the window may have arrived after it closed and lives in the arrival partition of
the day it landed. *"A 90-day window resolves 90 partitions"* is true only of a declared one-day
event-time mapping and is never a general rule.

**The metastore is asked one question, and only when the declaration cannot answer.** A windowed
read never consults it: intersecting the window's partitions with the ones that happen to exist
would turn a missing partition into a smaller number with no error, when L1 (§11.2) reports it as
``PARTITION_ABSENT``. Only ``FULL_SCAN`` needs the live list, because *"every partition"* is not
something a declaration states. Nothing here reads a row — the control plane never reads feature
data.

**What raises and what refuses.** A malformed business date, a duplicated request key or a
transform this module has no form for are defects at the call site, which §14's closed vocabularies
have no member for, so they raise. A verdict about the ENVIRONMENT — a table the inventory no longer
declares, a layout that moved since compilation, a full scan the metastore lists nothing for — is
governed, and is RETURNED as a :class:`MaterializationRefused`.
"""
from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from featuregen.materialize import compile as _compile  # noqa: F401 — read via `identity`
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import PitSpec
from featuregen.materialize.identity import RenderedArtifactIdentity, sandbox_execution_hash
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import (
    AvailabilityPartition,
    ClusterInventoryV1,
    EventTimePartition,
    FullScan,
    PartitionTransform,
    StaticSnapshot,
    VerifiedUnpartitioned,
)
from featuregen.materialize.ir import BridgeExecutionAuthorization, FormulaExecutionIRV1
from featuregen.materialize.render.project import REQUIRED_RUN_PARAMETERS
from featuregen.materialize.spine import (
    ActivePopulation,
    CurrentSnapshot,
    LatestAvailableAsOf,
    SpineSpec,
)
from featuregen.overlay.upload.object_ref import parse_ref

__all__ = [
    "DAYS_PER_UNIT",
    "MONTHS_PER_UNIT",
    "PARTITION_VALUE_FORMS",
    "PERIOD_STARTS",
    "SPINE_EXPR_PATH",
    "SPINE_FEATURE_NAME",
    "MetastorePartitions",
    "PartitionSpec",
    "PhysicalInputSnapshot",
    "RunInputRequest",
    "RunPreparation",
    "input_snapshot_ids",
    "prepare_run",
    "resolve_snapshots",
    "run_input_requests",
    "spine_input_request",
    "staging_root_for",
]

#: What the spine's request calls itself. Dunder-wrapped because a request's key is
#: ``(feature_name, expr_path, requirement_id)`` and :func:`resolve_snapshots` raises on a duplicate:
#: a spine that named itself after its table could collide with a feature computed from that table.
SPINE_FEATURE_NAME = "__spine__"
SPINE_EXPR_PATH = "spine"

#: How a resolved DATE becomes a partition VALUE, one entry per ``PartitionTransform`` member. The
#: transform is DECLARED (§3.4) and this only applies it; a member missing from here raises rather
#: than defaulting, because the default an unmodelled transform falls into is "the date as written",
#: which selects an empty partition and returns a smaller number with no error. The two forms are
#: the ones ``render.nodes_compute._TRANSFORMS`` renders — `date_iso` as the date, `date_compact`
#: with the dashes removed — and a test pins that this table covers the same closed enum.
PARTITION_VALUE_FORMS = {
    PartitionTransform.DATE_ISO: lambda day: day.isoformat(),
    PartitionTransform.DATE_COMPACT: lambda day: day.strftime("%Y%m%d"),
}

#: How many DAYS one window unit is, for the two units that ARE a whole number of days in every
#: calendar. A SECOND statement of ``render.nodes_compute._DAYS_PER_UNIT`` rather than an import,
#: and a test pins the two equal: the renderer decides which ROWS a window keeps and this decides
#: which PARTITIONS are read, so a disagreement means a run reads partitions whose rows the rendered
#: predicate then drops — or, worse, does not read partitions whose rows it would have kept.
DAYS_PER_UNIT: dict[str, int] = {"day": 1, "week": 7}

#: The units that are a whole number of MONTHS. A month is not thirty days, and the difference moves
#: a boundary: three months before 2026-07-06 is 2026-04-06 where ninety days is 2026-04-07.
MONTHS_PER_UNIT: dict[str, int] = {"month": 1, "quarter": 3, "year": 12}

#: The first day of the calendar period a date falls in, per unit — the anchor a ``calendar_period``
#: window ends at. ``day`` is absent for the reason the renderer's table omits it: the period a date
#: falls in IS that date, so there is nothing to truncate to. ``week`` starts on Monday, which is
#: what Spark's ``trunc(date, 'week')`` returns.
PERIOD_STARTS: dict[str, Any] = {
    "week": lambda day: day - dt.timedelta(days=day.weekday()),
    "month": lambda day: day.replace(day=1),
    "quarter": lambda day: day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1),
    "year": lambda day: day.replace(month=1, day=1),
}

_TRAILING = "trailing"
_CALENDAR_PERIOD = "calendar_period"
_INCLUSIVE = "inclusive"
_EXCLUSIVE = "exclusive"


class MetastorePartitions(Protocol):
    """The metastore seam run preparation needs — METADATA only, and one question.

    Task 0 owns the real ``MetastoreInventoryAdapter``; this states what it must satisfy. The single
    method answers *which partitions does this table have*, as ordered ``(column, value)`` tuples in
    the metastore's own order — the same shape :class:`PartitionSpec` carries, so nothing has to
    reinterpret it. It is deliberately the only method: a seam that could also fetch rows would be a
    seam through which the control plane could read feature data.
    """

    def list_partitions(self, *, schema: str, table: str
                        ) -> Sequence[Sequence[tuple[str, str]]]:
        """Every partition of one physical table, or an empty sequence when it has none."""
        ...


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    """ONE partition, as ordered ``(column, value)`` pairs (§3.3).

    Ordered because a multi-column partition is addressed positionally on the cluster, and a set
    that agreed about the values while disagreeing about their order would name a different
    partition.
    """

    columns: tuple[tuple[str, str], ...]

    def identity_payload(self) -> list[list[str]]:
        return [[column, value] for column, value in self.columns]


@dataclass(frozen=True, slots=True)
class RunInputRequest:
    """ONE expression's read of ONE physical table, and the point-in-time semantics it reads under.

    The field set is the plan's Produces line exactly. ``pit_spec`` is per request rather than per
    call because §3.3 says a single window cannot describe a group's reads: a ratio's numerator and
    denominator are two expressions with two ``PitSpec``s, and a 30-day and a 90-day feature over
    one table select two different partition sets.

    ``pit_spec`` is ``None`` for a read that is not windowed at all — a spine's ``PARTITION_MAPPED``
    population, where the DECLARED mapping alone selects the set (§4.2). It is not a default: a
    windowed read that lost its ``PitSpec`` would resolve the business date's single partition and
    return a 90-day feature computed from one day of data.
    """

    feature_name: str
    expr_path: str
    physical_requirement: PhysicalInputRequirement
    pit_spec: PitSpec | None


@dataclass(frozen=True, slots=True)
class PhysicalInputSnapshot:
    """The exact partitions ONE expression reads for ONE run (§3.3) — never part of ``ir_hash``.

    ``feature_name`` and ``expr_path`` are here because §3.3 keys snapshots by
    ``(feature_name, expr_path, requirement_id)`` and a resolution that dropped them could not say
    which expression it belongs to — the spec's field sketch omits them, and the plan's own test
    reads them off the snapshot.

    ``partition_specs is None`` means VERIFIED UNPARTITIONED, never "unknown": an unknown layout
    refuses at compilation with ``PARTITION_IDENTITY_UNKNOWN`` and never reaches here. For an
    unpartitioned mutable table the snapshot is honestly **not content-addressed** (§3.4), which is
    one more reason this slice is sandbox-only.
    """

    feature_name: str
    expr_path: str
    requirement: PhysicalInputRequirement
    partition_specs: tuple[PartitionSpec, ...] | None
    resolved_at_business_dt: str

    def key(self) -> tuple[str, str, str]:
        """What §3.3 keys a snapshot by. Unique across a run's requests, and checked to be."""
        return (self.feature_name, self.expr_path, self.requirement.requirement_id())

    def read_payload(self) -> dict[str, Any]:
        """What this snapshot READS — and deliberately not who asked for it.

        Two expressions that resolve the same requirement to the same partitions on the same date
        perform ONE read, so they share an id and are de-duplicated in ``input_snapshot_ids``. The
        payload carries the requirement id (which covers the table, its declared mapping and its
        layout), the ordered partitions and the business date; a read that differs in any of them —
        a different window above all — yields a different id and is never collapsed.
        """
        return {
            "requirement_id": self.requirement.requirement_id(),
            "partition_specs": (None if self.partition_specs is None
                                else [spec.identity_payload() for spec in self.partition_specs]),
            "business_dt": self.resolved_at_business_dt,
        }

    def snapshot_id(self) -> str:
        """The id §3.4 puts inside ``sandbox_execution_hash`` — derived, never stored."""
        return materialize_hash(self.read_payload())

    def parameter_payload(self) -> dict[str, Any]:
        """One entry of the ``input_snapshots`` run parameter (§11.1).

        This is the shape Task 13a declined to invent (A.16): it names the read, the expression it
        belongs to and the physical table. It is prepared EVIDENCE, not an enforced read scope:
        the set is resolved here at preparation and consumed by L1's ``PARTITION_ABSENT`` check
        (the same resolved partitions must still exist at validation time); the rendered nodes
        filter by the window/availability predicates and do not re-apply the snapshot list, so
        nothing proves afterwards that the run read precisely these partitions (DEFERRED-WORK
        A.31). It carries no data value and no location — a location is a catalog entry, and a
        value is what the run computes.
        """
        return {
            "snapshot_id": self.snapshot_id(),
            "feature_name": self.feature_name,
            "expr_path": self.expr_path,
            "requirement_id": self.requirement.requirement_id(),
            "catalog_source": self.requirement.catalog_source,
            "schema": self.requirement.schema,
            "table": self.requirement.table,
            "partition_specs": (None if self.partition_specs is None
                                else [spec.identity_payload() for spec in self.partition_specs]),
        }


@dataclass(frozen=True, slots=True)
class RunPreparation:
    """Everything ONE run needs, and what §11.1 says submission must pass to execution.

    ``parameters`` is exactly ``render.project.REQUIRED_RUN_PARAMETERS`` — the rendered
    ``RunParametersHook`` refuses a run that is missing one *or* carries one it did not plan for, so
    equality is the only shape that runs at all. It is a read-only mapping: the values were hashed,
    and a caller free to edit one afterwards would be submitting a run the hash does not describe.
    """

    snapshots: tuple[PhysicalInputSnapshot, ...]
    sandbox_execution_hash: str
    parameters: Mapping[str, Any]

    def covered_parameters(self) -> Mapping[str, Any]:
        """The parameters the execution hash is computed OVER — every one except itself.

        The same non-circularity ``generated_project_hash`` needs, for the same reason: §11.1 makes
        ``sandbox_execution_hash`` one of the values execution is given, and a hash over a mapping
        containing itself cannot be computed. Excluding exactly one key keeps the other five inside
        the identity, so a run submitted with a different staging root or a different generation is
        a different execution.
        """
        return MappingProxyType({name: value for name, value in self.parameters.items()
                                 if name != "sandbox_execution_hash"})


# ── the business date ────────────────────────────────────────────────────────────────────────────


def _business_date(business_dt: str) -> dt.date:
    """Parse the run's date, strictly. A.13 left the FORMAT to this task, and this is why.

    ``2026-7-27`` and ``27-07-2026`` are read as different days by different readers and as nothing
    by others; either way the partition value built from one selects a partition that does not
    exist, and an empty partition is a smaller number rather than an error. A datetime is refused
    too: a business date is a DATE, and the time part would silently vanish into the partition
    value while remaining inside the execution hash.
    """
    if not isinstance(business_dt, str) or not business_dt.strip():
        raise ValueError(
            f"business_dt is blank ({business_dt!r}): every partition this run reads is resolved "
            f"from it, and a blank date resolves nothing while still identifying an execution")
    text = business_dt.strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(
            f"business_dt {business_dt!r} is not an ISO-8601 calendar date (YYYY-MM-DD): a date "
            f"this module cannot read is a partition value nothing on the cluster matches, which "
            f"reads an empty partition and returns a number rather than an error") from error
    if parsed.isoformat() != text:
        raise ValueError(
            f"business_dt {business_dt!r} is not the canonical form of {parsed.isoformat()!r}: the "
            f"date is recorded verbatim on every snapshot and inside the execution hash, so two "
            f"spellings of one day would be two execution identities for one run")
    return parsed


# ── the window, in DATES ─────────────────────────────────────────────────────────────────────────


def _add_months(day: dt.date, months: int) -> dt.date:
    """Spark's ``add_months``: whole months, clamped to the end of the target month.

    2026-03-31 back one month is 2026-02-28, not 2026-03-03. The clamp is the behaviour, not an
    edge case — a date-arithmetic answer that differs from the renderer's moves the boundary.
    """
    total = (day.year * 12 + (day.month - 1)) + months
    year, month = divmod(total, 12)
    return day.replace(year=year, month=month + 1,
                       day=min(day.day, calendar.monthrange(year, month + 1)[1]))


def _window_days(pit: PitSpec, anchor: dt.date) -> tuple[dt.date, dt.date]:
    """The FIRST and LAST calendar day the declared window can contain an instant on.

    Mirrors ``render.nodes_compute._window_boundaries`` step for step: the boundaries are DATES
    first and instants second, which is what makes a calendar period a calendar period and what
    keeps a boundary from crossing a daylight-saving change wrong.

    Both inclusivity flags are honoured, as DAYS rather than as instants — that is the one place
    this differs from the rendered predicate, and it is exact rather than approximate. Each boundary
    lands on local midnight, so an EXCLUSIVE start excludes one instant and leaves the rest of that
    day inside the window (the day stays), while an EXCLUSIVE end excludes the whole of the end day
    except an instant that is itself excluded (the day goes).
    """
    if not isinstance(pit.window_length, int) or isinstance(pit.window_length, bool):
        raise ValueError(
            f"the declared window length is {pit.window_length!r}, which is not a whole number of "
            f"units: it is resolved into date arithmetic, where anything else is a boundary "
            f"somebody rounded")
    if pit.window_length < 1:
        raise ValueError(
            f"the declared window length is {pit.window_length}: a window of no units spans no "
            f"time, so the read would select nothing and every feature would evaluate to its "
            f"empty-window policy with no error anywhere")
    if pit.window_start_inclusive not in (_INCLUSIVE, _EXCLUSIVE) or \
            pit.window_end_inclusive not in (_INCLUSIVE, _EXCLUSIVE):
        raise ValueError(
            f"the window's boundaries are declared {pit.window_start_inclusive!r} / "
            f"{pit.window_end_inclusive!r}: Inclusivity is CLOSED, and the flags are carried per "
            f"expression precisely because they vary")
    if pit.window_basis not in (_TRAILING, _CALENDAR_PERIOD):
        raise ValueError(
            f"the window's basis is {pit.window_basis!r}: WindowBasis is CLOSED, and the two bases "
            f"anchor the window at DIFFERENT dates")

    if pit.window_basis == _TRAILING or pit.window_unit == "day":
        end = anchor
    else:
        period = PERIOD_STARTS.get(pit.window_unit)
        if period is None:
            raise ValueError(
                f"the window's unit is {pit.window_unit!r}: WindowUnit is CLOSED, and a calendar "
                f"period this module cannot truncate to has no first day")
        end = period(anchor)

    days = DAYS_PER_UNIT.get(pit.window_unit)
    if days is not None:
        start = end - dt.timedelta(days=pit.window_length * days)
    else:
        months = MONTHS_PER_UNIT.get(pit.window_unit)
        if months is None:
            raise ValueError(
                f"the window's unit is {pit.window_unit!r}, which this module has no calendar "
                f"arithmetic for: WindowUnit is CLOSED, and the default an unmodelled unit would "
                f"fall into is a day count — the exact conversion §8 rule 2 forbids")
        start = _add_months(end, -pit.window_length * months)

    last = end if pit.window_end_inclusive == _INCLUSIVE else end - dt.timedelta(days=1)
    if last < start:
        raise ValueError(
            f"the declared window spans no day: it starts at {start.isoformat()} and ends at "
            f"{last.isoformat()}, so the read would select nothing")
    return start, last


def _dates(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]


# ── resolution, one request at a time ────────────────────────────────────────────────────────────


def _refuse(code: CompilationRefusalCode, detail: str) -> MaterializationRefused:
    return MaterializationRefused(code, detail)


def _fold(identifier: str) -> str:
    return identifier.strip().lower()


def _time_partition_specs(mapping: EventTimePartition | AvailabilityPartition,
                          pit: PitSpec | None, anchor: dt.date) -> tuple[PartitionSpec, ...]:
    """A time-mapped read: the DECLARED partition column, over the days the read can need.

    Three widenings, each declared rather than inferred:

    1. an ``AVAILABILITY_PARTITION`` extends the set forward by ``late_arrival_days`` (§3.4): an
       event inside the window may have arrived after it closed and lives in the arrival partition
       of the day it landed, so reading only the window's own days drops it;
    2. a mapping whose declared time zone differs from the window's extends by one day at each end.
       An instant at 02:00 in the window's zone is the previous day in a UTC-partitioned table, and
       every real offset is inside 24 hours, so one day is sufficient and provably so;
    3. no window at all (a spine's ``PARTITION_MAPPED`` population) is the business date's own
       partition, which is what the rendered spine node selects.

    Every one of them changes what is READ, never what is COUNTED — the rendered predicate still
    decides which rows the window keeps.
    """
    form = PARTITION_VALUE_FORMS.get(mapping.transform)
    if form is None:
        raise ValueError(
            f"the declared mapping renders partition values with {mapping.transform!r}, which this "
            f"module has no form for: PartitionTransform is CLOSED, and applying an unmodelled one "
            f"would build a value nothing on the cluster matches")

    first, last = _window_days(pit, anchor) if pit is not None else (anchor, anchor)

    if isinstance(mapping, AvailabilityPartition):
        if not isinstance(mapping.late_arrival_days, int) or \
                isinstance(mapping.late_arrival_days, bool) or mapping.late_arrival_days < 0:
            raise ValueError(
                f"the availability mapping declares late_arrival_days="
                f"{mapping.late_arrival_days!r}: it is BY HOW MUCH the partition set widens, and a "
                f"value that is not a whole number of days widens by nothing")
        last = last + dt.timedelta(days=mapping.late_arrival_days)

    if pit is not None and _fold(mapping.timezone) != _fold(pit.window_timezone):
        first, last = first - dt.timedelta(days=1), last + dt.timedelta(days=1)

    return tuple(PartitionSpec(columns=((mapping.partition_column, form(day)),))
                 for day in _dates(first, last))


def _resolve_one(request: RunInputRequest, inventory: ClusterInventoryV1,
                 metastore: MetastorePartitions, anchor: dt.date
                 ) -> tuple[PartitionSpec, ...] | None | MaterializationRefused:
    """The partitions ONE request reads — or a governed refusal about the environment."""
    requirement = request.physical_requirement
    layout = inventory.layout_for(requirement.schema, requirement.table)
    if layout is None:
        return _refuse(
            CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
            f"the {inventory.environment_id!r} inventory has no entry for {requirement.schema}."
            f"{requirement.table}, which this compilation was authorized to read: a table nobody "
            f"captured is UNKNOWN, not unpartitioned, and resolving its partitions from a "
            f"declaration that is no longer there would read a layout nothing describes")
    if materialize_hash(layout.semantic_payload()) != requirement.layout_fingerprint:
        return _refuse(
            CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
            f"{requirement.schema}.{requirement.table} was compiled against a layout whose "
            f"fingerprint the {inventory.environment_id!r} inventory no longer states: the "
            f"partition columns, physical types or declared mapping have moved since, and which of "
            f"the two describes the table this run would read is exactly what is unknown")

    mapping = requirement.partition_mapping
    if mapping is None:
        return _refuse(
            CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED,
            f"{requirement.schema}.{requirement.table} declares no partition mapping, so nothing "
            f"states how this read's time window maps onto its partitions — and inferring one from "
            f"a column name drops late-arriving rows with no error anywhere")

    if isinstance(mapping, VerifiedUnpartitioned):
        if requirement.partition_columns is not None:
            return _refuse(
                CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED,
                f"{requirement.schema}.{requirement.table} is declared VERIFIED unpartitioned but "
                f"names {len(requirement.partition_columns)} partition column(s): one of the two "
                f"declarations is stale, and reading the table whole would read partitions nobody "
                f"scoped")
        return None

    if isinstance(mapping, StaticSnapshot):
        return (PartitionSpec(columns=tuple(mapping.partition_values)),)

    if isinstance(mapping, FullScan):
        listed = tuple(PartitionSpec(columns=tuple((str(column), str(value))
                                                   for column, value in partition))
                       for partition in metastore.list_partitions(schema=requirement.schema,
                                                                  table=requirement.table))
        if not listed:
            return _refuse(
                CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
                f"the metastore lists no partitions for {requirement.schema}."
                f"{requirement.table}, which is declared FULL_SCAN: "
                f"'this table has no partitions' and 'the metastore did not answer' are the same "
                f"empty list, and reading nothing would return a smaller number rather than an "
                f"error")
        return listed

    return _time_partition_specs(mapping, request.pit_spec, anchor)


def resolve_snapshots(
    inventory: ClusterInventoryV1,
    metastore: MetastorePartitions,
    *,
    requests: tuple[RunInputRequest, ...],
    business_dt: str,
) -> tuple[PhysicalInputSnapshot, ...] | MaterializationRefused:
    """Resolve every expression's read against a business date and the environment (§3.3).

    There is deliberately **no** ``window`` parameter. The plan's Produces line is the authoritative
    signature and two of its own snippets are stale leftovers of a draft in which one window
    described a whole group; §3.3 settles it in prose — *"A single ``window`` argument cannot
    describe those reads"* — and the window arrives per request, on ``RunInputRequest.pit_spec``.

    Identical reads are de-duplicated **after** their semantics are compared, and only in
    :func:`input_snapshot_ids`: every request keeps its own snapshot, because a snapshot is keyed by
    the expression that asked for it, and two expressions sharing an id share it because their reads
    were proved identical rather than because their tables looked alike.

    Returns:
        Snapshots in the requests' order, or the FIRST governed refusal. Order is the caller's
        because §3.4 calls the resolved ids *the exact ordered partition set read*.

    Raises:
        ValueError: ``business_dt`` is not an ISO calendar date, two requests share one key, or a
            declaration this module has no form for (an unmodelled transform, an unmodelled window
            unit, a negative widening) is reached — all call-site defects, which §14 has no member
            for.
    """
    anchor = _business_date(business_dt)
    date_text = anchor.isoformat()

    seen: set[tuple[str, str, str]] = set()
    resolved: list[PhysicalInputSnapshot] = []
    cache: dict[str, tuple[PartitionSpec, ...] | None] = {}

    for request in requests:
        key = (request.feature_name, request.expr_path,
               request.physical_requirement.requirement_id())
        if key in seen:
            raise ValueError(
                f"two requests share the key {key!r}: one expression reads one table once, and a "
                f"second row under one key would be silently dropped by anything keying on it")
        seen.add(key)

        # The cache key is the READ's full semantics — the requirement and the PitSpec, never the
        # table. Two requests hit it only when their windows, zones, bases and boundary flags are
        # identical as well, which is what "de-duplicated after comparing semantics" means.
        semantic = materialize_hash({
            "requirement_id": request.physical_requirement.requirement_id(),
            "pit": None if request.pit_spec is None else request.pit_spec.identity_payload(),
        })
        if semantic in cache:
            specs = cache[semantic]
        else:
            outcome = _resolve_one(request, inventory, metastore, anchor)
            if isinstance(outcome, MaterializationRefused):
                return outcome
            cache[semantic] = specs = outcome

        resolved.append(PhysicalInputSnapshot(
            feature_name=request.feature_name, expr_path=request.expr_path,
            requirement=request.physical_requirement, partition_specs=specs,
            resolved_at_business_dt=date_text))

    return tuple(resolved)


def run_input_requests(
    irs: Iterable[FormulaExecutionIRV1]
) -> tuple[RunInputRequest, ...]:
    """One request per ``(feature, expression, physical requirement)`` — per EXPRESSION by shape.

    The bridge from compiled IRs, so no caller has to assemble requests by hand and no caller can
    assemble them per FEATURE: an expression's ``PitSpec`` travels with its own requirements, which
    is the property a hand-built request list is free to get wrong.

    The spine is deliberately absent. §11.2's L1 covers *every feature IR, every expression, and the
    spine*, but a spine's population is selected by a ``SnapshotPolicy`` rather than by a ``PitSpec``
    and its requirement is resolved outside the IR (the renderer takes it as a separate argument),
    so a spine request is built by the caller that holds that requirement — with ``pit_spec=None``,
    which this module resolves as the mapping's own selection.
    """
    return tuple(
        RunInputRequest(feature_name=ir.feature_name, expr_path=expression.expr_path,
                        physical_requirement=requirement, pit_spec=expression.pit)
        for ir in irs
        for expression in ir.expressions
        for requirement in expression.input_requirements)


def spine_input_request(
    spine: SpineSpec,
    spine_input: PhysicalInputRequirement,
    *,
    business_dt: str,
) -> RunInputRequest | MaterializationRefused:
    """The ONE request that covers the declared entity population (§4.2, §11.2).

    :func:`run_input_requests` walks expressions and therefore cannot produce this one: a spine's
    population is selected by a ``SnapshotPolicy``, not by a ``PitSpec``, and its
    ``PhysicalInputRequirement`` is resolved outside the IR. ``pit_spec`` is ``None`` — not a
    default but a statement that the DECLARED mapping alone selects the set, which for
    ``PARTITION_MAPPED`` is the business date's own partition, exactly what the rendered spine node
    selects.

    **This is where §4.2's vintage condition becomes reachable — for BOTH present-tense
    policies.** A table that holds no history was observed at one vintage; asked for any other
    business date it would answer with the rows it has, and the run would publish one day's
    population under another day's date. §4.2 says it refuses rather than pretending, so it does.
    The refusal is typed ``SPINE_DECLARATION_REJECTED_BY_FACTS``: §14's vocabularies are closed and
    no member names a run-preparation verdict, and of the members that exist this is the only one
    that says *this spine declaration does not stand*. The strain is real and recorded — the
    refuting input is the run's business date rather than a governed catalog fact.

    ``CurrentSnapshot`` declares its vintage as ``observed_snapshot_ref`` — an opaque identifier,
    a date or a version. ``ActivePopulation`` — the other history-free policy — declares
    ``observed_on``, the calendar date its current rows were observed valid: its status column is
    CURRENT-valued, so any other business date is review §2.1's confirmed leak (a January backfill
    run in July silently publishing July's actives), and requiring an ``availability_ref`` instead
    would only half-close it — an entity CLOSED since a historical business date has no row left
    for rule 6 to filter. Both vintages enter identity, so the remedy the refusal points at — a
    RE-DECLARATION with a new vintage — is a hash-visible, reviewable act.

    A vintage that is not a calendar date is refused too. It is an opaque declared identifier, a
    version and a date have no ordering between them, and *"this module cannot check the vintage"*
    is not *"the vintage is fine"*.

    Returns:
        The request, or a governed refusal.

    Raises:
        TypeError: ``spine`` or ``spine_input`` is not the type it must be.
        ValueError: ``business_dt`` is not an ISO calendar date.
    """
    if not isinstance(spine, SpineSpec):
        raise TypeError(
            f"the spine's request is built from the SpineSpec §4 validated, got "
            f"{type(spine).__name__}: a bare declaration is one the governed facts never checked")
    if not isinstance(spine_input, PhysicalInputRequirement):
        raise TypeError(
            f"the spine's request needs the population table's PhysicalInputRequirement, got "
            f"{type(spine_input).__name__}: the declared partition mapping lives on it")
    anchor = _business_date(business_dt)
    policy = spine.snapshot_policy

    if isinstance(policy, CurrentSnapshot | ActivePopulation):
        # ONE guard for the two present-tense policies; only WHICH declared field holds the
        # vintage differs. `label` puts the policy's name in the shared refusal, so the operator
        # reading it knows which declaration to re-declare with a new vintage.
        if isinstance(policy, CurrentSnapshot):
            vintage, vintage_source = policy.observed_snapshot_ref.strip(), "observed at"
        else:
            vintage, vintage_source = policy.observed_on.strip(), "observed on"
        label = policy.kind.name
        try:
            observed = dt.date.fromisoformat(vintage)
        except ValueError:
            return _refuse(
                CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS,
                f"the population of {spine.source_table_ref} is declared {label} {vintage_source} "
                f"{vintage!r}, which is not a calendar date: the run's business date is "
                f"{anchor.isoformat()}, and a version identifier and a date have no ordering "
                f"between them — so whether this table can answer this date cannot be established, "
                f"and an unestablished vintage is not a matching one")
        if observed != anchor:
            return _refuse(
                CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS,
                f"the population of {spine.source_table_ref} is declared {label} {vintage_source} "
                f"{observed.isoformat()}, and this run's business date is {anchor.isoformat()}: "
                f"a table that holds no history cannot answer another date, and answering with the "
                f"rows it happens to hold would publish one day's population under another day's "
                f"date")

    if isinstance(policy, LatestAvailableAsOf) and isinstance(
            spine_input.partition_mapping, EventTimePartition | AvailabilityPartition):
        return _refuse(
            CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS,
            f"the population of {spine.source_table_ref} is declared LATEST_AVAILABLE_AS_OF, which "
            f"reads effective-dated HISTORY, while the table declares a time-based partition "
            f"mapping on {spine_input.schema}.{spine_input.table}: the spine carries no PitSpec, so "
            f"the mapping would select the business date's own partition and the population would "
            f"be everyone whose record changed that day — a smaller number with no error anywhere")

    return RunInputRequest(feature_name=SPINE_FEATURE_NAME, expr_path=SPINE_EXPR_PATH,
                           physical_requirement=spine_input, pit_spec=None)


def input_snapshot_ids(snapshots: Sequence[PhysicalInputSnapshot]) -> tuple[str, ...]:
    """The ids §3.4 puts inside ``sandbox_execution_hash``, de-duplicated in FIRST-SEEN order.

    Two expressions whose reads were proved identical performed one read and contribute one id.
    Order is preserved rather than sorted, because ``sandbox_execution_hash`` keeps the caller's
    order and sorting here would declare two different read orders equivalent.
    """
    ids: list[str] = []
    for snapshot in snapshots:
        snapshot_id = snapshot.snapshot_id()
        if snapshot_id not in ids:
            ids.append(snapshot_id)
    return tuple(ids)


# ── the prepared run ─────────────────────────────────────────────────────────────────────────────


def staging_root_for(staging_base: str, *, generation_id: str) -> str:
    """The staging root for ONE generation (§9, A.15) — derived, so it cannot be forgotten.

    Staging paths are generation-scoped and immutable. Without the scoping, a reused path can leave
    an older SUCCESSFUL manifest whose ``ir_hash`` still matches, and assembly would publish stale
    output while passing every other check — the whole hazard §9's generation/run/date binding
    exists to close, reopened through the path instead. There is no parameter for a root that does
    not name its generation, which is why this is a function rather than a caller's f-string.
    """
    if not isinstance(staging_base, str) or not staging_base.strip():
        raise ValueError(
            f"staging_base is blank ({staging_base!r}): the generated catalog interpolates the "
            f"root into every staged path, and a blank one would write to a relative location "
            f"nobody declared")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError(
            f"generation_id is blank ({generation_id!r}): the staging root is scoped BY it, and an "
            f"unscoped root is the reused path §9's stale-manifest gate exists to make "
            f"unreachable")
    return f"{staging_base.strip().rstrip('/')}/{generation_id.strip()}"


def _date_typed_clock_refusal(
    requests: tuple[RunInputRequest, ...], inventory: ClusterInventoryV1
) -> MaterializationRefused | None:
    """Refuse a run whose PIT clock is a Hive ``DATE`` read under a non-UTC window zone.

    The rendered window comparison is ``F.col(clock) >= F.to_utc_timestamp(boundary, zone)``:
    casting the boundary DATE lands on midnight, and ``to_utc_timestamp`` re-reads it as ``zone``'s
    wall clock, so west of UTC the whole window shifts by a day — the first day drops and an extra
    trailing day is admitted (measured with America/New_York; east of UTC is correct by luck). The
    IR carries no physical type for the clock (``PitSpec.event_time_ref`` is a bare ref), so the
    check lives here, where the inventory's ``TableLayout.columns`` holds ``(name, physical
    type)`` — fail-closed, until the IR carries physical time types (DEFERRED-WORK A.29).

    BOTH PIT refs are checked: rule 1's availability gate compares under the same zone arithmetic
    as rule 2's window, so a DATE-typed ``availability_ref`` shifts the cutoff the same way.

    A ref whose column the requirement's layout does not list passes through, deliberately: a
    multi-requirement expression attaches its ONE ``PitSpec`` to every table it reads, so the clock
    column legitimately appears in exactly one of those layouts — and the request for that table is
    the one that refuses. A missing layout is unreachable here because :func:`resolve_snapshots`
    already refused it (``PARTITION_IDENTITY_UNKNOWN``, which also proves the fingerprint still
    matches, so ``columns`` below is the layout compilation saw); re-litigating it would give one
    verdict two owners.
    """
    for request in requests:
        pit = request.pit_spec
        if pit is None or pit.window_timezone == "UTC":
            continue
        requirement = request.physical_requirement
        layout = inventory.layout_for(requirement.schema, requirement.table)
        if layout is None:  # pragma: no cover — resolve_snapshots refused before this runs
            continue
        types = {_fold(name): dtype.strip().lower() for name, dtype in layout.columns}
        for role, ref in (("event_time_ref", pit.event_time_ref),
                          ("availability_ref", pit.availability_ref)):
            _source, _schema, _table, column = parse_ref(ref)
            if column is None:
                # A table ref cannot be a clock; the renderer already raised on it at build time.
                continue
            if types.get(_fold(column)) == "date":
                return _refuse(
                    CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED,
                    f"the {role} of {request.feature_name}/{request.expr_path} is {ref!r}, whose "
                    f"column {column!r} is a Hive DATE in {requirement.schema}."
                    f"{requirement.table}, and the declared window zone is "
                    f"{pit.window_timezone!r}: the rendered comparison casts the DATE boundary to "
                    f"midnight and to_utc_timestamp re-reads it as {pit.window_timezone!r} wall "
                    f"clock, which shifts the whole window by a day west of UTC — the first day "
                    f"drops and an extra trailing day is admitted. The IR carries no physical "
                    f"time type for the clock, so this run refuses until PitSpec carries the "
                    f"clock dtype and the renderer emits date-typed comparisons "
                    f"(DEFERRED-WORK A.29)")
    return None


def prepare_run(
    rendered: RenderedArtifactIdentity,
    inventory: ClusterInventoryV1,
    metastore: MetastorePartitions,
    *,
    generation_id: str,
    run_id: str,
    business_dt: str,
    requests: tuple[RunInputRequest, ...],
    staging_base: str,
    capability_attestation_id: str,
    bridge_authorization: BridgeExecutionAuthorization | None = None,
    additional_parameters: Mapping[str, Any] | None = None,
    required_parameters: Sequence[str] = REQUIRED_RUN_PARAMETERS,
) -> RunPreparation | MaterializationRefused:
    """Resolve a run's inputs and identify it — §11.1's ``prepare_run``.

    It takes the RENDERED identity rather than §11.1's ``result``: §2's compilation chain has no
    orchestrating module yet (A.13), and the rendered identity is the part of a result this needs —
    the compilation identity plus the project hash, which is what makes two runs of two builds of
    one compilation distinguishable.

    ``environment_id`` is read off the inventory rather than accepted, so a run cannot be identified
    as belonging to one environment while resolving its partitions against another's declarations.

    The compiler's and renderer's versions are not parameters here either: ``sandbox_execution_hash``
    reads both through their owning modules (§7), so there is nowhere on this call to freeze one.
    """
    required_bridge_dependencies = rendered.compilation.bridge_realization_dependencies
    if required_bridge_dependencies:
        if bridge_authorization is None:
            return MaterializationRefused(
                CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                "the rendered cross-catalog project has no final bridge execution authorization",
            )
        expected_ir_hashes = tuple(sorted(rendered.compilation.ir_hashes))
        if (
            bridge_authorization.environment_id != inventory.environment_id
            or bridge_authorization.ir_hashes != expected_ir_hashes
            or bridge_authorization.realization_dependencies != required_bridge_dependencies
        ):
            return MaterializationRefused(
                CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN,
                "the final bridge execution authorization does not cover this artifact's exact "
                "IRs, environment and realization dependency snapshots",
            )
    elif bridge_authorization is not None and bridge_authorization.realization_dependencies:
        raise ValueError(
            "a bridge execution authorization was supplied for an artifact that declares no "
            "cross-catalog realization dependency")

    snapshots = resolve_snapshots(inventory, metastore, requests=requests,
                                  business_dt=business_dt)
    if isinstance(snapshots, MaterializationRefused):
        return snapshots

    # AFTER snapshots resolve (so the layouts below are present and fingerprint-verified) and
    # BEFORE the hash: a run this refuses never acquires an execution identity.
    date_clock_refusal = _date_typed_clock_refusal(requests, inventory)
    if date_clock_refusal is not None:
        return date_clock_refusal

    covered: dict[str, Any] = {
        "business_dt": _business_date(business_dt).isoformat(),
        "generation_id": generation_id,
        "input_snapshots": [snapshot.parameter_payload() for snapshot in snapshots],
        "run_id": run_id,
        "staging_root": staging_root_for(staging_base, generation_id=generation_id),
    }
    extras = dict(additional_parameters or {})
    overlap = set(extras) & set(covered | {"sandbox_execution_hash": ""})
    if overlap:
        raise ValueError(
            f"additional run parameters overwrite owned parameters: {sorted(overlap)}")
    execution_hash = sandbox_execution_hash(
        rendered, environment_id=inventory.environment_id, parameters={**covered, **extras},
        business_dt=covered["business_dt"], input_snapshot_ids=input_snapshot_ids(snapshots),
        capability_attestation_id=capability_attestation_id)

    parameters = {**covered, **extras, "sandbox_execution_hash": execution_hash}
    expected_parameters = set(required_parameters)
    if not set(REQUIRED_RUN_PARAMETERS) <= expected_parameters:
        raise ValueError(
            "required_parameters omitted one or more base run parameters")
    if set(parameters) != expected_parameters:
        raise ValueError(
            f"the prepared parameters are {sorted(parameters)} and the rendered project requires "
            f"{sorted(expected_parameters)}: the rendered hook refuses a run that is missing "
            f"one OR carries one nothing planned to read, so anything but equality cannot run")
    return RunPreparation(snapshots=snapshots, sandbox_execution_hash=execution_hash,
                          parameters=MappingProxyType(parameters))

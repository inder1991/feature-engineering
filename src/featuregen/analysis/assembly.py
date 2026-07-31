"""What still stands between a plan and an executable one — enumerated in code, not in prose.

Every piece was built separately: a binding registry, an eligibility policy store, a window resolver,
a declared population. This walks them in order and names the FIRST thing missing. Enumerating them
here rather than in a comment is the point — a hand-maintained list of "what's left" drifts the moment
a store lands, and this cannot.

**Not every gap is configuration, and that distinction is the useful part.**

* ``PHYSICAL_BINDING_ABSENT``, ``ELIGIBILITY_ABSENT``, ``ATTRIBUTION_ABSENT`` — someone records
  something. An operator or a data owner closes them. Addressing goes through ``resolve_table``, so
  declaring a CATALOG's engine and tier once addresses every table in it; a per-table binding is the
  exception, for the table pointed at a snapshot or caught mid-migration.
* ``POPULATION_UNDECLARED`` — a person must CHOOSE, and no store may choose for them. See
  ``materialize/spine.py``: look-alike population tables are indistinguishable to the catalog, and a
  wrong choice silently shrinks every number that follows.
* ``PARTITION_CALENDAR_UNKNOWN`` — nothing records whether a partition column names months or days.
  Guessing from a value like ``2026-05`` is inference that fails silently on a table partitioned by
  something else.
* ``JOIN_EVIDENCE_ABSENT`` — **this one cannot be configured at all.** A verified join rests on
  OBSERVED evidence: ``observe_relationship`` measures uniqueness, coverage and fan-out against live
  data. No amount of setup produces it, and that is the design working — a relationship someone
  merely declared is exactly what the analysis refuses to trust. It is checked last because its
  answer is "probe the warehouse", not "fill something in".

So every deployment gets a gap today, and the final one is always the probe. That is a true statement
about the system rather than an unfinished one, and putting it here means the screen says it too.
"""

from __future__ import annotations

from datetime import datetime

from featuregen.analysis.plan import AnalysisPlanV1
from featuregen.analysis.windows import (
    PartitionGranularity,
    WindowResolutionError,
    resolve_window_partitions,
)
from featuregen.data_agent.binding_store import resolve_table
from featuregen.data_agent.connection import ConnectionError_
from featuregen.data_agent.eligibility_store import resolve_eligibility

#: Reported in this order, cheapest first: an address before a decision, a decision before a probe.
GAP_ORDER: tuple[str, ...] = (
    "PHYSICAL_BINDING_ABSENT",
    "POPULATION_UNDECLARED",
    "ELIGIBILITY_ABSENT",
    "PARTITION_CALENDAR_UNKNOWN",
    "ATTRIBUTION_ABSENT",
    "JOIN_EVIDENCE_ABSENT",
)


def _source_and_table(ref: str) -> tuple[str, str]:
    """From a TABLE ref (``source::[schema.]table``) — the last segment is the table."""
    source, _, rest = ref.partition("::")
    parts = [p for p in rest.split(".") if p]
    return source.strip().lower(), (parts[-1] if parts else "")


def _source_and_table_of_column(ref: str) -> tuple[str, str]:
    """From a COLUMN ref (``source::[schema.]table.column``) — the table is second from last.

    Deliberately separate from `_source_and_table`: passing a column ref to that one yields the
    COLUMN as the table name, so `ftr::cust_dim.segment` looked up a table called `segment` and
    reported it unbound. Mirrors `grounding._parse`, which reads the last two segments for the same
    reason.
    """
    source, _, rest = ref.partition("::")
    parts = [p for p in rest.split(".") if p]
    return source.strip().lower(), (parts[-2] if len(parts) >= 2 else "")


def first_unmet_requirement(
    conn, plan: AnalysisPlanV1, *,
    reference: datetime | None = None,
    granularity: PartitionGranularity | None = None,
) -> tuple[str, str] | None:
    """`(code, subject)` for the first thing missing, or `None` when nothing is.

    `None` is currently UNREACHABLE: join evidence has no source but a live probe. The type says so
    rather than a tuple whose first half is always empty, and when the probe result and an
    attribution policy have homes, the final branch becomes the assembled inputs.

    `granularity` and `reference` are passed in rather than looked up — nothing records a partition
    column's calendar, and the reference instant is the caller's clock. Absent either, the calendar
    gap is reported instead of a month being assumed.
    """
    event_source, event_table = _source_and_table(plan.base_table_ref)
    try:
        if resolve_table(conn, catalog_source=event_source, table=event_table) is None:
            return ("PHYSICAL_BINDING_ABSENT", f"{event_source}::{event_table}")
    except ConnectionError_ as exc:
        # A refused grant is a governance answer and must not read as "nobody set this up".
        return (exc.code, f"{event_source}::{event_table}")

    if not plan.population_table_ref or not plan.population_key_ref:
        return ("POPULATION_UNDECLARED", plan.base_table_ref)
    spine_source, spine_table = _source_and_table(plan.population_table_ref)
    try:
        if resolve_table(conn, catalog_source=spine_source, table=spine_table) is None:
            return ("PHYSICAL_BINDING_ABSENT", plan.population_table_ref)
    except ConnectionError_ as exc:
        return (exc.code, plan.population_table_ref)

    if resolve_eligibility(conn, catalog_source=event_source, table=event_table) is None:
        return ("ELIGIBILITY_ABSENT", f"{event_source}::{event_table}")

    anchor = plan.windows[0].anchor_ref if plan.windows else plan.base_table_ref
    if granularity is None or reference is None:
        return ("PARTITION_CALENDAR_UNKNOWN", anchor)
    try:
        # Called for its REFUSALS: a day-span window against calendar partitions, an unlabelled
        # window, a negative lag. The values are discarded here because the last gap blocks anyway.
        resolve_window_partitions(
            plan.windows, granularity=granularity, reference=reference,
            lag_hours=plan.windows[0].availability_lag_hours if plan.windows else None)
    except WindowResolutionError as exc:
        return (exc.code, exc.subject)

    if plan.dimensions:
        dim_source, dim_table = _source_and_table_of_column(
            plan.dimensions[0].logical_ref)
        try:
            if resolve_table(conn, catalog_source=dim_source, table=dim_table) is None:
                return ("PHYSICAL_BINDING_ABSENT", plan.dimensions[0].logical_ref)
        except ConnectionError_ as exc:
            return (exc.code, plan.dimensions[0].logical_ref)
        # Nothing records whether a customer is classified as of the cutoff, per period, or as they
        # are today — and those give different answers.
        return ("ATTRIBUTION_ABSENT", plan.dimensions[0].logical_ref)

    return ("JOIN_EVIDENCE_ABSENT", f"{plan.population_key_ref} <- {plan.entity_ref}")

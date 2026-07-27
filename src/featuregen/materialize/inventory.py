"""Spec §0 / §3.4 — the DECLARED target-environment inventory: what the cluster actually looks like.

Everything here is a **declaration about a physical environment**, not a governed catalog fact. The
catalog knows what a column MEANS; only the cluster knows how its table is laid out, and nothing in
the catalog can answer "does a 90-day event window map onto 90 load partitions?". So the layout is
declared once, per environment, and the compiler reads it.

**The mapping is declared, never inferred (§3.4).** A partition column named ``load_dt`` does not
tell you how an event-time window maps onto load partitions: a transaction that happened on the 1st
and arrived on the 5th sits in the 5th's partition, so a window resolved by *event* date silently
drops it. Inferring a mapping from a column name would produce a smaller number with no error
anywhere. Hence a closed set of variants, each of which states exactly how a time window becomes a
partition set, and a missing one refuses (``PARTITION_MAPPING_NOT_DECLARED``) rather than defaulting.

**Absent is not unpartitioned.** ``TableLayout.partition_columns is None`` means the capture VERIFIED
the table has no partitions. A table missing from :class:`ClusterInventoryV1.tables` means nobody
looked, which refuses with ``PARTITION_IDENTITY_UNKNOWN``. Collapsing the two would let an
unexamined table read as "just scan it".

**What is identity and what is provenance.** ``captured_at``, ``location`` and
``rewritten_in_place`` describe an OBSERVATION of the environment, not the semantics a compilation
depends on, so :meth:`TableLayout.semantic_payload` excludes them: re-capturing identical metadata
must leave every downstream hash unchanged (spec §3.3). ``location`` and ``rewritten_in_place`` are
kept because run preparation and the honesty note in §3.4 (an unpartitioned mutable table is not
content-addressed) need them — they are simply not identity.

**Status: partial.** Task 5 needs the typed declarations, so they live here, in the module Task 0
owns. Task 0 adds ``EngineVersions``, ``load_inventory(path)`` and ``MetastoreInventoryAdapter``,
plus the ``conf/environments/*.yml`` these are loaded from. It should extend this file rather than
restate it — two definitions of a layout are two layouts, and the second one is the one nobody
governs.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

__all__ = [
    "PARTITION_MAPPING_TYPES",
    "AvailabilityPartition",
    "ClusterInventoryV1",
    "EventTimePartition",
    "FullScan",
    "PartitionMappingKind",
    "PartitionMappingV1",
    "PartitionTransform",
    "StaticSnapshot",
    "TableLayout",
    "VerifiedUnpartitioned",
]


class PartitionMappingKind(StrEnum):
    """How a time window becomes a partition set. CLOSED (spec §3.4)."""

    EVENT_TIME_PARTITION = "event_time_partition"
    AVAILABILITY_PARTITION = "availability_partition"
    STATIC_SNAPSHOT = "static_snapshot"
    FULL_SCAN = "full_scan"
    VERIFIED_UNPARTITIONED = "verified_unpartitioned"


class PartitionTransform(StrEnum):
    """How an instant is rendered as a partition VALUE. CLOSED, and deliberately small.

    A free-text transform would be a format string nobody governs, evaluated against a live
    metastore, with a wrong answer that looks like an empty partition rather than an error. Only the
    forms this slice has actually seen are listed; a cluster that partitions some other way gets a
    member added here, which is the visible moment somebody decided it was correct.

    Rendering happens at RUN preparation (§3.3) — this module names the transform, it never applies
    one, because applying one needs a business date and no business date exists at generation time.
    """

    DATE_ISO = "date_iso"            # 2026-07-27
    DATE_COMPACT = "date_compact"    # 20260727


@dataclass(frozen=True, slots=True)
class EventTimePartition:
    """The partition column tracks the EVENT time, so a window maps onto exactly its own range.

    ``time_ref`` is the LOGICAL column ref whose time this partitioning follows — recorded so a
    mapping declared for one clock cannot be silently reused for another. ``partition_column`` is
    the PHYSICAL partition column; the two are different things and a table may name them
    differently.
    """

    time_ref: str
    partition_column: str
    transform: PartitionTransform
    timezone: str
    kind: ClassVar[PartitionMappingKind] = PartitionMappingKind.EVENT_TIME_PARTITION

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "time_ref": self.time_ref,
                "partition_column": self.partition_column, "transform": self.transform.value,
                "timezone": self.timezone}

    def partition_columns_named(self) -> tuple[str, ...]:
        return (self.partition_column,)


@dataclass(frozen=True, slots=True)
class AvailabilityPartition:
    """The partition column tracks ARRIVAL, not event time — so the partition set must be WIDER.

    An event inside the window may have arrived after the window closed, and it lives in the
    arrival partition of the day it landed. Reading only the window's own partitions therefore
    drops late data and returns a smaller number with no error. §3.4 requires the set to be
    extended; ``late_arrival_days`` is BY HOW MUCH.

    It is a required declaration for the same reason the mapping itself is: an inferred widening is
    a guess about a specific bank's specific feed, and the failure mode of guessing low is invisible.
    It is identity-bearing — two widenings read two different partition sets, so they are two
    different computations. The event-time predicate still applies inside the widened set; the
    widening changes what is READ, never what is COUNTED.
    """

    time_ref: str
    partition_column: str
    transform: PartitionTransform
    timezone: str
    late_arrival_days: int
    kind: ClassVar[PartitionMappingKind] = PartitionMappingKind.AVAILABILITY_PARTITION

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "time_ref": self.time_ref,
                "partition_column": self.partition_column, "transform": self.transform.value,
                "timezone": self.timezone, "late_arrival_days": self.late_arrival_days}

    def partition_columns_named(self) -> tuple[str, ...]:
        return (self.partition_column,)


@dataclass(frozen=True, slots=True)
class StaticSnapshot:
    """A FIXED partition selection that does not move with the business date.

    The values are declared constants (a vintage, a reference-data drop), so they are static across
    every run and belong in identity — the §3.3 ban is on partition values RESOLVED FROM A BUSINESS
    DATE, which are a run observation. A declared constant is part of what the artifact IS.
    """

    partition_values: tuple[tuple[str, str], ...]
    kind: ClassVar[PartitionMappingKind] = PartitionMappingKind.STATIC_SNAPSHOT

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value,
                "partition_values": [[column, value] for column, value in self.partition_values]}

    def partition_columns_named(self) -> tuple[str, ...]:
        return tuple(column for column, _value in self.partition_values)


@dataclass(frozen=True, slots=True)
class FullScan:
    """Every partition is read. An explicit, expensive, reviewable declaration — never a default."""

    kind: ClassVar[PartitionMappingKind] = PartitionMappingKind.FULL_SCAN

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    def partition_columns_named(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class VerifiedUnpartitioned:
    """The capture VERIFIED there are no partitions. Pairs with ``partition_columns is None``."""

    kind: ClassVar[PartitionMappingKind] = PartitionMappingKind.VERIFIED_UNPARTITIONED

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    def partition_columns_named(self) -> tuple[str, ...]:
        return ()


#: The CLOSED union (spec §3.4). A mapping that is not one of these five is refused, never
#: defaulted: the default a missing variant falls into is "read the window's own partitions", which
#: is precisely the late-arrival data loss §3.4 exists to prevent.
PartitionMappingV1 = (
    EventTimePartition | AvailabilityPartition | StaticSnapshot | FullScan | VerifiedUnpartitioned)

#: Kind -> variant, one-to-one. A test pins it against ``PartitionMappingKind`` exactly, so a new
#: kind cannot be added without a variant that says what partitions it selects.
PARTITION_MAPPING_TYPES: Mapping[PartitionMappingKind, type] = {
    PartitionMappingKind.EVENT_TIME_PARTITION: EventTimePartition,
    PartitionMappingKind.AVAILABILITY_PARTITION: AvailabilityPartition,
    PartitionMappingKind.STATIC_SNAPSHOT: StaticSnapshot,
    PartitionMappingKind.FULL_SCAN: FullScan,
    PartitionMappingKind.VERIFIED_UNPARTITIONED: VerifiedUnpartitioned,
}


@dataclass(frozen=True, slots=True)
class TableLayout:
    """One physical table as the environment declares it.

    ``columns`` are the DATA columns, ordered, as ``(name, physical type)`` — Hive lists partition
    columns separately and so does this, because the two answer different questions (what a value
    is, versus how to find the rows). Physical types are here rather than derived from the catalog
    on purpose: ``graph_node.data_type`` is the LOGICAL representation a decision governed, and the
    cluster's ``decimal(18,2)`` is a different fact about a different system.
    """

    schema: str
    table: str
    partition_columns: tuple[tuple[str, str], ...] | None
    partition_mapping: PartitionMappingV1 | None
    columns: tuple[tuple[str, str], ...]
    location: str
    rewritten_in_place: bool

    def semantic_payload(self) -> dict[str, Any]:
        """The three things §3.3 names as SEMANTIC: partition columns+types, physical types, mapping.

        ``location`` and ``rewritten_in_place`` are excluded. They are operational facts about the
        same governed table — moving a warehouse directory does not change what a feature means,
        and a compilation identity that moved with it would force a recompile for an estate
        migration that changed nothing semantic.
        """
        return {
            "partition_columns": (None if self.partition_columns is None
                                  else [[c, t] for c, t in self.partition_columns]),
            "columns": [[c, t] for c, t in self.columns],
            "partition_mapping": (None if self.partition_mapping is None
                                  else self.partition_mapping.identity_payload()),
        }


@dataclass(frozen=True, slots=True)
class ClusterInventoryV1:
    """A target environment as somebody looked at it and wrote it down.

    ``tables`` is keyed by ``"<physical schema>.<table>"``, case-folded on lookup. A KeyError-shaped
    absence is deliberate: §3.4's "absent is not unpartitioned" only holds if the two are
    distinguishable, so there is no ``.get(...)`` default anywhere in this type's contract.

    ``logical_schema_map`` is §3.5 step 2 — the environment's DECLARED logical-to-physical schema
    mapping, keyed by the canonical LOGICAL TABLE ref (``source::schema.table``). It is consulted
    only when the governed catalog attests no schema, and it exists because ``graph_node.schema_name``
    is nullable: a schema-less upload leaves the catalog with no opinion, and the alternative to a
    declaration is defaulting to ``public`` and reading a different table.

    ``captured_at`` is OBSERVATION provenance and never enters identity.
    """

    environment_id: str
    tables: Mapping[str, TableLayout]
    logical_schema_map: Mapping[str, str]
    captured_at: str

    def layout_for(self, schema: str, table: str) -> TableLayout | None:
        """The declared layout for one physical table, or ``None`` when nobody captured it.

        Unquoted SQL identifiers fold, so the lookup folds; what the inventory DECLARED is returned
        unrewritten, because that is what the generated project should render.
        """
        wanted = f"{schema.strip().lower()}.{table.strip().lower()}"
        for key, layout in self.tables.items():
            if key.strip().lower() == wanted:
                return layout
        return None

    def declared_schema_for(self, table_ref: str) -> str | None:
        """The declared physical schema for a canonical LOGICAL table ref, or ``None``."""
        wanted = table_ref.strip().lower()
        for key, schema in self.logical_schema_map.items():
            if key.strip().lower() == wanted:
                return schema
        return None

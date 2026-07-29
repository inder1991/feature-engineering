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

**How it was built.** Task 5 needed the typed declarations, so they landed here, in the module Task 0
owns. Task 12 added :class:`EngineVersions` and :attr:`ClusterInventoryV1.engine_versions` — the
generated project pins its dependencies from them (§7), and a renderer that accepted the versions
as loose strings would pin the project to whatever a caller happened to type rather than to what the
environment was captured as. Task 0 then added the two ways an inventory comes into existence —
:func:`load_inventory` (a human wrote it down) and :class:`MetastoreInventoryAdapter` (a probe looked)
— in this file rather than a second one, because two definitions of a layout are two layouts and the
second one is the one nobody governs.

**Neither entry point defaults.** A YAML that omits a field, leaves it null, or names a partition
mapping kind nobody implemented raises rather than filling a blank in, and the adapter refuses a
table the metastore does not have rather than leaving it out of ``tables`` — an omission reads
downstream as "nobody looked", which is a different and much quieter failure than "it is not there".
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from zoneinfo import ZoneInfo

import yaml

__all__ = [
    "PARTITION_MAPPING_TYPES",
    "AvailabilityPartition",
    "ClusterInventoryV1",
    "EngineVersions",
    "EventTimePartition",
    "FullScan",
    "MetastoreInventoryAdapter",
    "MetastoreTableMetadata",
    "PartitionMappingKind",
    "PartitionMappingV1",
    "PartitionTransform",
    "StaticSnapshot",
    "TableDeclaration",
    "TableLayout",
    "VerifiedUnpartitioned",
    "load_inventory",
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
class EngineVersions:
    """The runtimes the target environment actually runs (spec §0), all of them REQUIRED.

    Three of these — ``python``, ``pyspark``, ``kedro`` (and ``kedro_datasets``, below) — are what
    §7's generated project pins its own dependencies to. The other four describe the cluster the
    project is rendered FOR: they decide whether a publish mechanism is even available (§10.3's
    attestation is keyed on ``hive``/``spark``/``metastore``), and ``java`` is what a Spark version
    is only meaningful against.

    Every field is required and non-blank, because the failure mode of a missing one is silent. An
    unpinned dependency resolves to whatever the installer's index offers on the day, so a project
    that "ran last week" is not the project that runs today — and a rendered artifact identified by
    the hash of its bytes would be claiming an identity its behaviour does not have.

    ``kedro_datasets`` is not in the plan's field sketch, and it is here because Kedro moved its
    dataset implementations into a SEPARATELY VERSIONED distribution: ``spark.SparkHiveDataset``
    resolves out of ``kedro-datasets``, not out of ``kedro``, so a lock naming only ``kedro`` leaves
    the class that reads every governed source table unpinned.
    """

    hive: str
    spark: str
    metastore: str
    python: str
    java: str
    pyspark: str
    kedro: str
    kedro_datasets: str

    def __post_init__(self) -> None:
        for name in ("hive", "spark", "metastore", "python", "java", "pyspark", "kedro",
                     "kedro_datasets"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"engine version {name} is blank ({value!r}): an environment is described by "
                    f"what it RUNS, and a blank version pins nothing — the generated project would "
                    f"install whatever the index offers on the day it is built")

    def identity_payload(self) -> dict[str, str]:
        """Every field, in a stable order. TOTAL — no parsing, nothing that can raise."""
        return {"hive": self.hive, "spark": self.spark, "metastore": self.metastore,
                "python": self.python, "java": self.java, "pyspark": self.pyspark,
                "kedro": self.kedro, "kedro_datasets": self.kedro_datasets}


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

    ``engine_versions`` is what the environment RUNS. It is required rather than optional for the
    same reason a table's layout is: an inventory that omitted it would let a project be rendered
    against an environment nobody established the runtimes of, and the resulting artifact would pin
    nothing (§7).

    ``captured_at`` is OBSERVATION provenance and never enters identity.
    """

    environment_id: str
    tables: Mapping[str, TableLayout]
    logical_schema_map: Mapping[str, str]
    engine_versions: EngineVersions
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


# ── the loader: an inventory a human wrote down ──────────────────────────────────────────────────

#: Top-level keys :func:`load_inventory` CONSUMES. Anything outside this set and
#: :data:`_DECLARED_FOR_LATER` refuses, because the alternative is that ``enviroment_id`` loads an
#: inventory with no environment and every downstream message names the empty string.
_CONSUMED_KEYS = frozenset(
    {"environment_id", "captured_at", "engine_versions", "logical_schema_map", "tables"})

#: Keys the §0 inventory legitimately carries that NOTHING typed consumes yet. Today that is §3.4b's
#: per-``(source_type, source)`` engine map: the refusal code exists (``SOURCE_ENGINE_UNSUPPORTED``)
#: but no reader does, and inventing a type for it here would be a second, ungoverned definition of
#: the shape whoever wires that gate has to pick. Listed rather than silently ignored so that
#: "declared but unread" is a visible statement and not an accident of a permissive parser.
_DECLARED_FOR_LATER = frozenset({"engines"})

#: Every key one ``tables:`` entry carries. All REQUIRED — including the ones whose value may be
#: ``null``, because ``partition_columns: null`` (verified unpartitioned) and an absent
#: ``partition_columns`` (nobody looked) are the exact pair §3.4 forbids collapsing, and a parser
#: that read both with ``.get()`` would collapse them.
_LAYOUT_KEYS = frozenset(
    {"partition_columns", "partition_mapping", "columns", "location", "rewritten_in_place"})

#: Field order for the required-version sweep, so the message names the FIRST missing runtime rather
#: than whichever one a set happened to yield.
_ENGINE_VERSION_FIELDS = ("hive", "spark", "metastore", "python", "java", "pyspark", "kedro",
                          "kedro_datasets")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{where} must be a non-blank string, got {value!r}: a blank identifier names nothing, "
            f"and every use of one downstream builds a path or a query out of the empty string")
    return value


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a mapping, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{where} has a non-string key {key!r}")
    return value


def _required(node: Mapping[str, Any], key: str, where: str) -> Any:
    """The value at ``key``, refusing when the key is ABSENT — ``null`` is returned as ``None``.

    Presence and nullity are different declarations everywhere in this file: a null is somebody
    saying "there are none", an absence is nobody saying anything.
    """
    if key not in node:
        raise ValueError(
            f"{where} declares no {key!r}. Every field of an inventory is required: an omitted one "
            f"would be filled in by whatever this loader defaulted to, and the default nobody chose "
            f"is the one nobody reviews")
    return node[key]


def _no_unknown_keys(node: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise ValueError(
            f"{where} declares {', '.join(repr(k) for k in unknown)}, which nothing reads. A "
            f"misspelled key is indistinguishable from an omitted one to a permissive parser, and "
            f"an omitted key here is a layout nobody captured")


def _pairs(value: Any, where: str) -> tuple[tuple[str, str], ...]:
    """``[[name, type], ...]`` as an ordered tuple of pairs. Order is preserved, never sorted."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(
            f"{where} must be a list of [name, type] pairs, got {type(value).__name__}")
    out: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str) or not isinstance(item, Sequence) or len(item) != 2:
            raise ValueError(f"{where}[{index}] must be a [name, type] pair, got {item!r}")
        out.append((_text(item[0], f"{where}[{index}] name"),
                    _text(item[1], f"{where}[{index}] type")))
    return tuple(out)


def _timezone(value: Any, where: str) -> str:
    zone = _text(value, where)
    try:
        ZoneInfo(zone)
    # Not a bare `Exception`: `ZoneInfoNotFoundError` is a `KeyError`, a malformed key is a
    # `ValueError` and a non-string is a `TypeError` — one declaration bug wearing three types. An
    # `OSError` (unreadable tz database) is an environment failure and must not be reported as
    # "your zone is wrong".
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(
            f"{where} is not a known IANA zone ({exc}): a partition boundary is a wall-clock "
            f"instant in a named zone, so an unknown zone puts the boundary somewhere nobody "
            f"declared") from exc
    return zone


def _transform(value: Any, where: str) -> PartitionTransform:
    try:
        return PartitionTransform(value)
    except ValueError as exc:
        raise ValueError(
            f"{where} must be one of {[t.value for t in PartitionTransform]}, got {value!r}: the "
            f"transform is how an instant is RENDERED as a partition value, and an unlisted one "
            f"would select a partition that does not exist — which reads as an empty result, not "
            f"as an error") from exc


def _late_arrival_days(value: Any, where: str) -> int:
    # `isinstance(True, int)` is True, and `late_arrival_days: yes` is a YAML bool: accepting it
    # would widen the partition set by exactly one day because `True == 1`.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"{where} must be a non-negative whole number of days, got {value!r}: it is BY HOW MUCH "
            f"an availability-partitioned read is widened past the event window, and a widening "
            f"that does not say by how much is unimplementable")
    return value


#: How one declared field of a partition mapping is read. Keyed by FIELD NAME across every variant,
#: because the variants share their fields; a field on a variant that is missing from here raises,
#: so adding a field to a variant is a visible decision about how it is parsed rather than an
#: untyped passthrough.
_MAPPING_FIELD_READERS: Mapping[str, Callable[[Any, str], Any]] = {
    "time_ref": _text,
    "partition_column": _text,
    "transform": _transform,
    "timezone": _timezone,
    "late_arrival_days": _late_arrival_days,
    "partition_values": _pairs,
}


def _load_partition_mapping(value: Any, where: str) -> PartitionMappingV1 | None:
    """One closed §3.4 variant, or ``None`` when the declaration is explicitly null.

    ``None`` is a legal LOAD result and an illegal COMPILE input: a partitioned table with no
    declared mapping refuses with ``PARTITION_MAPPING_NOT_DECLARED`` at
    :func:`~featuregen.materialize.inputs.derive_requirement`. Refusing it here instead would make
    an inventory unloadable while a human is still working out the mapping for one of its tables.
    """
    if value is None:
        return None
    node = _mapping(value, where)
    raw_kind = _required(node, "kind", where)
    try:
        kind = PartitionMappingKind(raw_kind)
    except ValueError as exc:
        raise ValueError(
            f"{where}.kind must be one of {[k.value for k in PartitionMappingKind]}, got "
            f"{raw_kind!r}: the variants are CLOSED because the behaviour an unlisted one falls "
            f"back to is 'read the window's own partitions', which is exactly the late-arrival "
            f"data loss §3.4 exists to prevent") from exc

    variant = PARTITION_MAPPING_TYPES[kind]
    # Derived from the dataclass, never restated: a field added to a variant cannot be forgotten
    # here, it becomes a missing-key refusal on the next load.
    expected = {field.name for field in fields(variant)}
    _no_unknown_keys(node, frozenset(expected | {"kind"}), where)
    kwargs: dict[str, Any] = {}
    for name in sorted(expected):
        reader = _MAPPING_FIELD_READERS.get(name)
        if reader is None:  # pragma: no cover - a new variant field with no declared reader
            raise ValueError(
                f"{where}.{name} has no declared reader: a partition-mapping field that this loader "
                f"does not know how to parse cannot be read as raw YAML")
        kwargs[name] = reader(_required(node, name, where), f"{where}.{name}")
    mapping: PartitionMappingV1 = variant(**kwargs)
    return mapping


def _load_engine_versions(value: Any, where: str) -> EngineVersions:
    node = _mapping(value, where)
    _no_unknown_keys(node, frozenset(_ENGINE_VERSION_FIELDS), where)
    captured: dict[str, str] = {}
    for name in _ENGINE_VERSION_FIELDS:
        raw = node.get(name)
        if name not in node or raw is None or not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"{where}.{name} is missing or null. Every runtime version is required: an "
                f"unpinned dependency resolves to whatever the index offers on the day, so the "
                f"generated project that 'ran last week' is not the project that runs today, and a "
                f"rendered artifact identified by the hash of its bytes would be claiming an "
                f"identity its behaviour does not have")
        captured[name] = raw
    return EngineVersions(**captured)


def _split_table_key(key: str, where: str) -> tuple[str, str]:
    """``SCHEMA.TABLE`` as declared, case preserved — it is what the generated project renders."""
    text = _text(key, where)
    parts = text.split(".")
    if len(parts) != 2:
        raise ValueError(
            f"{where} must be exactly '<schema>.<table>', got {key!r}: a physical table is named by "
            f"two parts, and guessing which of three is the schema would read a different table")
    return _text(parts[0], f"{where} schema"), _text(parts[1], f"{where} table")


def _load_layout(key: str, value: Any, where: str) -> TableLayout:
    node = _mapping(value, where)
    _no_unknown_keys(node, frozenset(_LAYOUT_KEYS), where)
    schema, table = _split_table_key(key, f"{where} key")

    raw_partitions = _required(node, "partition_columns", where)
    if raw_partitions is None:
        partition_columns = None                     # VERIFIED unpartitioned (§3.4)
    else:
        partition_columns = _pairs(raw_partitions, f"{where}.partition_columns")
        if not partition_columns:
            raise ValueError(
                f"{where}.partition_columns is an EMPTY list. `null` means the capture verified "
                f"there are none; empty means nobody said — and 'we do not know how this table is "
                f"partitioned' is not 'scan it'")

    columns = _pairs(_required(node, "columns", where), f"{where}.columns")
    if not columns:
        raise ValueError(
            f"{where}.columns is empty: a captured table has columns, so an empty list is a "
            f"capture that did not happen rather than a table that has none")

    return TableLayout(
        schema=schema,
        table=table,
        partition_columns=partition_columns,
        partition_mapping=_load_partition_mapping(
            _required(node, "partition_mapping", where), f"{where}.partition_mapping"),
        columns=columns,
        location=_text(_required(node, "location", where), f"{where}.location"),
        rewritten_in_place=_strict_bool(
            _required(node, "rewritten_in_place", where), f"{where}.rewritten_in_place"),
    )


def _strict_bool(value: Any, where: str) -> bool:
    # Truthiness would read the string "false" as True, and `rewritten_in_place` decides whether an
    # unpartitioned snapshot is content-addressed at all (§3.4).
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be true or false, got {value!r}")
    return value


def load_inventory(path: str | os.PathLike[str]) -> ClusterInventoryV1:
    """Read one ``conf/environments/*.yml`` into the typed §0 inventory, or refuse.

    The Markdown record in ``docs/architecture/`` is for humans; THIS is what compilation and run
    preparation consume, because a prose file cannot be a runtime input (§0).

    Raises:
        ValueError: the file is not parseable YAML, is not a mapping, declares a key nothing reads,
            omits a required field, leaves a required field null, or names a partition mapping kind
            or transform outside the closed §3.4 sets. There is no partial load: an inventory that
            described half an environment would let a compilation bind to the half that was there.
        OSError: the file cannot be read. Deliberately not translated — "the operator pointed at a
            path that is not there" is an operational fact, not a malformed declaration.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{path} is not parseable YAML ({exc}): an environment inventory that cannot be read is "
            f"an environment nobody described, and every table in it would compile as unknown") from exc

    where = str(path)
    node = _mapping(raw, where)
    _no_unknown_keys(node, frozenset(_CONSUMED_KEYS | _DECLARED_FOR_LATER), where)

    tables_node = _mapping(_required(node, "tables", where), f"{where}.tables")
    tables: dict[str, TableLayout] = {}
    for key, layout_node in tables_node.items():
        layout = _load_layout(key, layout_node, f"{where}.tables[{key!r}]")
        folded = key.strip().lower()
        if folded in {existing.strip().lower() for existing in tables}:
            raise ValueError(
                f"{where}.tables declares {key!r} twice under different casing: unquoted SQL "
                f"identifiers fold, so the two entries name one table and only one of them would "
                f"ever be read")
        tables[key] = layout

    schema_map_node = _mapping(
        _required(node, "logical_schema_map", where), f"{where}.logical_schema_map")
    schema_map = {
        _text(ref, f"{where}.logical_schema_map key"):
            _text(schema, f"{where}.logical_schema_map[{ref!r}]")
        for ref, schema in schema_map_node.items()}

    return ClusterInventoryV1(
        environment_id=_text(_required(node, "environment_id", where), f"{where}.environment_id"),
        tables=tables,
        logical_schema_map=schema_map,
        engine_versions=_load_engine_versions(
            _required(node, "engine_versions", where), f"{where}.engine_versions"),
        captured_at=_text(_required(node, "captured_at", where), f"{where}.captured_at"),
    )


# ── the adapter: an inventory a probe looked at ──────────────────────────────────────────────────


class MetastoreTableMetadata(Protocol):
    """The three questions a CAPTURE asks the metastore. Metadata only — nothing returns a row.

    ``describe_partition_columns`` is separate from
    :meth:`~featuregen.materialize.runprep.MetastorePartitions.list_partitions` on purpose, and the
    difference is the whole point of §3.4's absent/verified distinction: listing partitions of a
    partitioned table that happens to have none yet returns the same empty sequence as listing
    partitions of an unpartitioned table, so the partition SCHEMA has to be asked for separately or
    "no partitions exist" and "no partition columns exist" become one answer.
    """

    def describe_columns(self, *, schema: str, table: str) -> Sequence[Sequence[str]] | None:
        """Ordered ``(name, physical type)`` DATA columns, or ``None`` when the table is absent."""
        ...

    def describe_partition_columns(self, *, schema: str, table: str) -> Sequence[Sequence[str]]:
        """Ordered ``(name, physical type)`` PARTITION columns — empty when there are none.

        Order is semantic, not cosmetic: the partition path is built from it, so a listing that
        sorted these would address a directory that does not exist.
        """
        ...

    def table_location(self, *, schema: str, table: str) -> str:
        """Where the table's data lives. Observation provenance — never part of identity."""
        ...


@dataclass(frozen=True, slots=True)
class TableDeclaration:
    """The two things about a table that a metastore CANNOT answer and a human must declare.

    ``partition_mapping`` is §3.4's declared-never-inferred variant — the metastore knows a column
    is called ``load_dt`` and cannot know whether a 90-day event window maps onto 90 of its
    partitions. ``rewritten_in_place`` is whether history is rewritten under the same path, which is
    a statement about how a feed operates, not about how a table is registered.

    ``partition_mapping=None`` is permitted so a table can be captured while its mapping is still
    being worked out; compilation then refuses it with ``PARTITION_MAPPING_NOT_DECLARED``.
    """

    partition_mapping: PartitionMappingV1 | None
    rewritten_in_place: bool


@dataclass(frozen=True, slots=True)
class MetastoreInventoryAdapter:
    """Turns what a metastore reports into typed :class:`TableLayout`s — and refuses the rest.

    The adapter is where "we looked" is established, so it is the only thing entitled to write
    ``partition_columns=None`` and :class:`VerifiedUnpartitioned` — the loader can only transcribe a
    human saying the same thing. Everything a metastore cannot answer (which environment this is,
    what it runs, how a logical schema maps to a physical one, how a window maps onto partitions)
    is a keyword argument, so a capture can never quietly invent one.
    """

    def capture(
        self,
        conn: MetastoreTableMetadata,
        tables: Sequence[str],
        *,
        environment_id: str,
        engine_versions: EngineVersions,
        declarations: Mapping[str, TableDeclaration],
        logical_schema_map: Mapping[str, str],
        clock: Callable[[], str],
    ) -> ClusterInventoryV1:
        """Capture ``tables`` (``"SCHEMA.TABLE"``) from ``conn`` into a typed inventory.

        Args:
            conn: the metastore seam. Metadata only.
            tables: the physical tables to capture, as declared. Case is preserved in the result.
            environment_id: which environment was looked at.
            engine_versions: what it RUNS (§0) — no metastore reports a Kedro version.
            declarations: per table (folded ``"schema.table"``), the two facts §3.4 requires a human
                to declare. A table with no entry refuses: the alternative is defaulting
                ``rewritten_in_place``, and both defaults are a claim about a feed nobody made.
            logical_schema_map: §3.5's declared logical→physical schema mapping. Pass ``{}`` for an
                environment that declares none.
            clock: produces ``captured_at``. Injected because it is the one value in the result that
                differs between two captures of an unchanged environment, and a test has to be able
                to make it differ on purpose.

        Raises:
            ValueError: a requested table is not in the metastore, is requested twice, reports no
                columns, reports a partition column that also appears as a data column, or carries a
                declaration that contradicts what was observed. None of these is recoverable by
                omitting the table: an absent entry means "nobody looked" (§3.4).
        """
        captured: dict[str, TableLayout] = {}
        seen: set[str] = set()
        for ref in tables:
            schema, table = _split_table_key(ref, f"requested table {ref!r}")
            folded = f"{schema.strip().lower()}.{table.strip().lower()}"
            if folded in seen:
                raise ValueError(
                    f"{ref!r} was requested twice: unquoted SQL identifiers fold, so two spellings "
                    f"are one table and the second capture would silently replace the first")
            seen.add(folded)
            captured[ref] = self._layout(conn, ref, schema, table,
                                         self._declaration(declarations, folded, ref))
        return ClusterInventoryV1(
            environment_id=_text(environment_id, "environment_id"),
            tables=captured,
            logical_schema_map=dict(logical_schema_map),
            engine_versions=engine_versions,
            captured_at=_text(clock(), "captured_at"),
        )

    @staticmethod
    def _declaration(declarations: Mapping[str, TableDeclaration], folded: str,
                     ref: str) -> TableDeclaration:
        for key, declaration in declarations.items():
            if key.strip().lower() == folded:
                return declaration
        raise ValueError(
            f"no declaration for {ref!r}. A capture reads a table's SHAPE from the metastore, but "
            f"how a time window maps onto its partitions and whether its history is rewritten in "
            f"place are statements about a feed that no metastore holds — so they are declared, and "
            f"a capture with neither would produce a layout that looks complete and is not")

    @staticmethod
    def _layout(conn: MetastoreTableMetadata, ref: str, schema: str, table: str,
                declaration: TableDeclaration) -> TableLayout:
        where = f"{ref} as reported by the metastore"
        raw_columns = conn.describe_columns(schema=schema, table=table)
        if raw_columns is None:
            raise ValueError(
                f"the metastore has no table {ref!r}. A capture refuses rather than leaving it out "
                f"of the inventory: an absent entry reads downstream as 'nobody looked' "
                f"(PARTITION_IDENTITY_UNKNOWN), which would hide the fact that somebody looked and "
                f"it was not there")
        columns = _pairs(raw_columns, f"{where}: columns")
        if not columns:
            raise ValueError(
                f"the metastore reports no columns for {ref!r}: a table that exists has columns, so "
                f"an empty description is a capture that did not happen")

        raw_partitions = _pairs(
            conn.describe_partition_columns(schema=schema, table=table),
            f"{where}: partition columns")
        overlap = sorted({name.strip().lower() for name, _type in raw_partitions}
                         & {name.strip().lower() for name, _type in columns})
        if overlap:
            raise ValueError(
                f"{ref!r} reports {', '.join(overlap)} as both a data column and a partition "
                f"column: the two answer different questions (what a value is, versus how to find "
                f"the rows), and one name with two declared physical types has no single answer to "
                f"either")

        # The ONE inference a capture is entitled to make, because it is an OBSERVATION: nothing was
        # partitioned, so it is VERIFIED unpartitioned rather than unknown. Note the shape — `None`,
        # never `()`, because an empty tuple means nobody said (see `_refuse_incoherent_layout`).
        partition_columns = raw_partitions or None
        mapping = declaration.partition_mapping
        if partition_columns is None:
            if mapping is not None and mapping.kind is not PartitionMappingKind.VERIFIED_UNPARTITIONED:
                raise ValueError(
                    f"{ref!r} is declared with a {mapping.kind.value} mapping but the metastore "
                    f"reports no partition columns: the declaration is stale, and a mapping over "
                    f"partitions that do not exist selects nothing")
            mapping = VerifiedUnpartitioned()
        elif mapping is not None and mapping.kind is PartitionMappingKind.VERIFIED_UNPARTITIONED:
            raise ValueError(
                f"{ref!r} is declared VERIFIED unpartitioned but the metastore reports "
                f"{len(partition_columns)} partition column(s): the mapping would read the table as "
                f"if the partitions were not there")

        return TableLayout(
            schema=schema, table=table,
            partition_columns=partition_columns,
            partition_mapping=mapping,
            columns=columns,
            location=_text(conn.table_location(schema=schema, table=table), f"{where}: location"),
            rewritten_in_place=declaration.rewritten_in_place,
        )

"""The typed observation result — identical whichever executor produced it.

This is the contract §3c rests on: *the ontology must not be able to tell which executor produced
its evidence*. Direct Hive SQL, ODS native SQL and a Spark/Kedro job all return this, so swapping
executors is a deployment decision rather than an architectural one.

It carries **what was read and how**, not only the numbers. A statistic without its input snapshot,
coverage and method is not evidence — it is a number whose meaning depends on facts the caller no
longer has. In particular `method` decides what the evidence can later support: a sampled profile
that finds a duplicate DISPROVES uniqueness, while one that finds none proves nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ColumnObservationV1:
    """One column's statistics from one observation."""

    column: str
    non_null_count: int
    distinct_count: int
    #: Present only when the plan opted this column in via `bounds_columns`. A bound is a real
    #: VALUE — `MIN(cif_id)` is a customer identifier — so it is absent by default.
    minimum: str | None = None
    maximum: str | None = None
    #: Rows observed for the whole statement; carried per column so a null rate is derivable without
    #: the caller having to hold the parent result.
    observed_rows: int = 0

    @property
    def null_count(self) -> int:
        return max(0, self.observed_rows - self.non_null_count)

    @property
    def null_rate(self) -> float:
        return (self.null_count / self.observed_rows) if self.observed_rows else 0.0


@dataclass(frozen=True, slots=True)
class ColumnTypeObservationV1:
    """One column's ENGINE-REPORTED type from one schema observation.

    Deliberately not a :class:`ColumnObservationV1`: a schema read carries no counts and no values
    — only what the engine says the column physically IS. That narrowness is the point; it is what
    lets `graph_node.data_type` be upgraded from this and from nothing weaker (a glossary's
    declared type never travels through this shape)."""

    column: str
    engine_type: str


@dataclass(frozen=True, slots=True)
class SchemaObservationResultV1:
    """One schema observation over one physical table — engine-reported column types only.

    Carries no ``method``: a schema read is a metadata census (`DESCRIBE` / information_schema),
    exact by construction, so there is no exact/approximate axis to report. ``complete`` still
    matters — a failed read must never let "unread" masquerade as "absent from the table"."""

    physical_id: str
    columns: tuple[ColumnTypeObservationV1, ...] = ()
    complete: bool = True
    failures: tuple[str, ...] = field(default_factory=tuple)

    def types_by_column(self) -> dict[str, str]:
        """``{lower-cased column: engine type}`` — the lookup an attestation consumer needs.
        Lower-cased because Hive identifiers are case-insensitive."""
        return {c.column.strip().lower(): c.engine_type.strip() for c in self.columns}


@dataclass(frozen=True, slots=True)
class DataObservationResultV1:
    """One bounded observation over one physical table."""

    physical_id: str
    row_count: int
    columns: tuple[ColumnObservationV1, ...] = ()
    #: The exact partitions read. Empty for an unpartitioned table — never empty as a shorthand for
    #: "everything", which is the ambiguity that turns a pruning bug into a silent full scan.
    partitions_read: tuple[str, ...] = ()
    method: str = "approximate"          # exact | approximate
    complete: bool = True
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def coverage(self) -> str:
        """Human-readable coverage. A partial observation must never read as a complete one."""
        if not self.complete:
            return "partial"
        return "exact" if self.method == "exact" else "approximate"

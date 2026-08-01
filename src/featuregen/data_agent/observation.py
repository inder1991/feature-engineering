"""The observation plan — WHAT to observe, independent of who executes it.

One plan, more than one executor (roadmap §3c). The plan owns every property that must hold however
the work runs: which physical object, which partitions, which columns, which bounds, and whether the
answer is exact or approximate. A dialect renders it; an executor runs it; both are swappable.

Validation happens **here**, at plan construction, rather than in a dialect. A refusal that lived in
the SQL renderer would be a refusal the Spark executor could forget — and the one that matters most,
refusing to scan a partitioned table without a selector, is exactly the kind of cost failure a
second executor would silently reintroduce.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1

#: Refusal codes — closed vocabulary.
NO_PARTITION_SELECTOR = "OBSERVATION_NO_PARTITION_SELECTOR"
UNKNOWN_PARTITION_COLUMN = "OBSERVATION_UNKNOWN_PARTITION_COLUMN"
EMPTY_PARTITION_SELECTION = "OBSERVATION_EMPTY_PARTITION_SELECTION"
TOO_MANY_COLUMNS = "OBSERVATION_MAX_COLUMNS_EXCEEDED"
TOO_MANY_PARTITIONS = "OBSERVATION_MAX_PARTITIONS_EXCEEDED"
UNSAFE_IDENTIFIER = "OBSERVATION_UNSAFE_IDENTIFIER"
NO_COLUMNS = "OBSERVATION_NO_COLUMNS"
BOUNDS_NOT_PROFILED = "OBSERVATION_BOUNDS_COLUMN_NOT_PROFILED"

#: A Hive identifier. Anything outside this is a mistake or an attack, and neither is rendered —
#: quoting is not the defence, refusal is.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ObservationPlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def require_identifier(name: str, *, what: str = "identifier") -> str:
    if not _IDENTIFIER.match((name or "").strip()):
        raise ObservationPlanError(
            UNSAFE_IDENTIFIER, f"{what} {name!r} is not a plain identifier")
    return name.strip()


@dataclass(frozen=True, slots=True)
class PartitionSelector:
    """The exact partitions to read. Values are literals, validated and rendered as quoted strings."""

    column: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservationPlanV1:
    """One bounded observation over one physical table."""

    binding: PhysicalDatasetBindingV1
    columns: tuple[str, ...]
    policy: ProfilePolicyV1
    partitions: PartitionSelector | None = None
    #: Columns for which MIN/MAX may be computed. Empty by DEFAULT, because a bound is a real VALUE:
    #: `MIN(cif_id)` is a customer identifier and `MIN(cust_name)` is a customer name. "Aggregate"
    #: is not the same as "safe" — an aggregate over one column of one row IS that row. Opt in for
    #: the columns where a bound is a statistic (amounts, dates) rather than a disclosure.
    bounds_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.columns:
            raise ObservationPlanError(NO_COLUMNS, "an observation must name at least one column")
        for column in self.columns:
            require_identifier(column, what="column")
        if len(self.columns) > self.policy.max_columns:
            raise ObservationPlanError(
                TOO_MANY_COLUMNS,
                f"{len(self.columns)} columns exceeds max_columns={self.policy.max_columns}")

        profiled = {c.strip().lower() for c in self.columns}
        for column in self.bounds_columns:
            require_identifier(column, what="bounds column")
            if column.strip().lower() not in profiled:
                raise ObservationPlanError(
                    BOUNDS_NOT_PROFILED,
                    f"bounds column {column!r} is not among the profiled columns")

        declared = {c.strip().lower() for c in self.binding.partition_columns}
        if declared and self.partitions is None:
            # THE expensive mistake: a predicate-free scan of a partitioned bank table.
            raise ObservationPlanError(
                NO_PARTITION_SELECTOR,
                f"{self.binding.identity.table_id} declares partition columns "
                f"{sorted(declared)}; an observation must select partitions rather than scan")
        if self.partitions is not None:
            require_identifier(self.partitions.column, what="partition column")
            if self.partitions.column.strip().lower() not in declared:
                raise ObservationPlanError(
                    UNKNOWN_PARTITION_COLUMN,
                    f"partition column {self.partitions.column!r} is not declared by the binding "
                    f"{sorted(declared) or '[]'}")
            if not self.partitions.values:
                raise ObservationPlanError(
                    EMPTY_PARTITION_SELECTION,
                    "selecting no partitions is either a mistake or a full scan awaiting a fallback")
            if len(self.partitions.values) > self.policy.max_partitions:
                raise ObservationPlanError(
                    TOO_MANY_PARTITIONS,
                    f"{len(self.partitions.values)} partitions exceeds "
                    f"max_partitions={self.policy.max_partitions}")

    @property
    def method(self) -> str:
        """`exact` or `approximate` — carried on the RESULT so a later consumer knows what the
        evidence can support. Sampled uniqueness never proves uniqueness."""
        return "exact" if self.policy.exact_distinct else "approximate"


@dataclass(frozen=True, slots=True)
class SchemaObservationPlanV1:
    """One schema read over one physical table — engine-reported column types, no data.

    Deliberately NARROWER than :class:`ObservationPlanV1`: a schema observation is a metadata read
    (Hive ``DESCRIBE`` / information_schema), so it names no columns (the engine reports what
    physically exists — an expected column's ABSENCE is itself the finding), selects no partitions
    (nothing scans, so the partitioned-table refusal above does not apply), and can never carry
    bounds (no value crosses the boundary). The policy rides along for the statement timeout only.

    The identity's schema/table are validated as plain identifiers HERE, per the module rule:
    validation at plan construction, refusal over quoting — a dialect must never be the last line
    of defence for a name it renders."""

    binding: PhysicalDatasetBindingV1
    policy: ProfilePolicyV1

    def __post_init__(self) -> None:
        require_identifier(self.binding.identity.schema, what="schema")
        require_identifier(self.binding.identity.table, what="table")

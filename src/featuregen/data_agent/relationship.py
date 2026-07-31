"""Relationship evidence — what the DATA says about a candidate join.

Every fact about a relationship in this system has so far been declared: a file said `joins_to`, or
a concept registry said two columns denote the same entity, or an ingest path fabricated `N:1` for a
blank cell. This is the first evidence that comes from the values.

It produces six things a declaration cannot:

* **uniqueness per side** — whether either column is actually a key;
* **null rate** on the referencing side;
* **referential coverage** — how many distinct left values have a matching right value. An
  unmatched key is the strongest cheap signal that two identifier columns do NOT share a namespace;
* **source tuple frequency** — how many source rows share one identifier;
* **join multiplier** — how many target rows each source row actually matches;
* **observed cardinality**, which is what `graph.py`'s propose-time `or "N:1"` currently guesses.

**It promotes nothing.** Release 1 produces evidence; Release 2 decides what it may support. The
rule governing that decision lives here as :meth:`RelationshipEvidenceV1.uniqueness_verdict`, and it
is deliberately asymmetric: a duplicate DISPROVES uniqueness however it was found, while finding no
duplicate proves nothing unless the probe was exact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from featuregen.data_agent.executor import DirectSqlExecutor
from featuregen.data_agent.observation import PartitionSelector, require_identifier
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship_observation import (
    RelationshipColumnPairV2,
    RelationshipMethod,
    RelationshipObservationPlanV2,
    RelationshipObservationScopeV1,
    RelationshipObservationV2,
    RowCoverage,
)


@dataclass(frozen=True, slots=True)
class RelationshipProbeV1:
    """One candidate relationship: a referencing column and the column it may reference."""

    left_binding: PhysicalDatasetBindingV1
    left_column: str
    right_binding: PhysicalDatasetBindingV1
    right_column: str
    policy: ProfilePolicyV1
    left_partitions: PartitionSelector | None = None
    right_partitions: PartitionSelector | None = None
    execution_principal: str = "legacy-data-agent"
    realization_revision_id: str = "legacy-scalar-relationship-v1"
    left_source_snapshot_id: str | None = None
    right_source_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.left_column, what="left column")
        require_identifier(self.right_column, what="right column")

    def to_v2(self) -> RelationshipObservationPlanV2:
        pair = RelationshipColumnPairV2(self.left_column, self.right_column)
        method = (
            RelationshipMethod.EXACT
            if self.policy.exact_distinct
            else RelationshipMethod.APPROXIMATE
        )
        scope = RelationshipObservationScopeV1(
            scope_id="legacy-scalar-scope-v1",
            left_binding_revision_id=self.left_binding.binding_revision_id,
            right_binding_revision_id=self.right_binding.binding_revision_id,
            left_partitions=self.left_partitions,
            right_partitions=self.right_partitions,
            normalization_ids=(pair.normalization_id,),
            execution_principal=self.execution_principal,
            method=method,
        )
        return RelationshipObservationPlanV2(
            left_binding=self.left_binding,
            right_binding=self.right_binding,
            column_pairs=(pair,),
            scope=scope,
            policy=self.policy,
            realization_revision_id=self.realization_revision_id,
            left_source_snapshot_id=(
                self.left_source_snapshot_id or self.left_binding.binding_revision_id),
            right_source_snapshot_id=(
                self.right_source_snapshot_id or self.right_binding.binding_revision_id),
            shortlist_position=1,
            shortlist_size=1,
        )


@dataclass(frozen=True, slots=True)
class RelationshipEvidenceV1:
    """Observed facts about one candidate relationship. Counts and ratios only — never a key value:
    an unmatched-key COUNT is a statistic, while the unmatched keys themselves are identifiers."""

    left_physical_id: str
    left_binding_revision_id: str
    left_binding_content_hash: str
    left_column: str
    right_physical_id: str
    right_binding_revision_id: str
    right_binding_content_hash: str
    right_column: str
    left_rows: int
    left_distinct: int
    left_nulls: int
    right_rows: int
    right_distinct: int
    right_nulls: int
    matched_distinct: int
    unmatched_distinct: int
    max_left_rows_per_tuple: int
    max_right_matches_per_left_row: int
    method: str
    complete: bool = True
    row_coverage: str = RowCoverage.FULL.value
    left_partitions_read: tuple[str, ...] = ()
    right_partitions_read: tuple[str, ...] = ()

    @property
    def left_is_unique(self) -> bool:
        """Observed, not asserted — see :meth:`uniqueness_verdict` for what it may SUPPORT."""
        return (
            self.left_rows > 0
            and self.left_nulls == 0
            and self.left_distinct == self.left_rows
        )

    @property
    def right_is_unique(self) -> bool:
        return (
            self.right_rows > 0
            and self.right_nulls == 0
            and self.right_distinct == self.right_rows
        )

    @property
    def coverage_ratio(self) -> float:
        """Distinct left values with a match, over distinct left values. 1.0 means every referencing
        value exists on the other side."""
        total = self.matched_distinct + self.unmatched_distinct
        return (self.matched_distinct / total) if total else 0.0

    @property
    def observed_cardinality(self) -> str:
        """What the data shows, in the vocabulary the governed fact uses."""
        if self.right_is_unique and self.left_is_unique:
            return "one_to_one"
        if self.right_is_unique:
            return "many_to_one"
        if self.left_is_unique:
            return "one_to_many"
        return "many_to_many"

    def uniqueness_verdict(self, side: str) -> str:
        """`unique` | `not_unique` | `unknown` — deliberately ASYMMETRIC.

        A duplicate disproves uniqueness however it was found, so `not_unique` is safe from an
        approximate probe. The reverse is not: an approximate distinct count that happens to equal
        the row count proves nothing, and returning `unique` there is exactly how a cheap profile
        would silently promote a bad key. Only an EXACT probe may assert uniqueness.
        """
        is_unique = self.left_is_unique if side == "left" else self.right_is_unique
        if not is_unique:
            return "not_unique"
        partitions = (
            self.right_partitions_read if side == "right"
            else self.left_partitions_read
        )
        if (
            self.method != "exact"
            or not self.complete
            or self.row_coverage != RowCoverage.FULL.value
            or bool(partitions)
        ):
            return "unknown"
        return "unique"

    @classmethod
    def from_v2(cls, observation: RelationshipObservationV2) -> RelationshipEvidenceV1:
        if len(observation.left.columns) != 1 or len(observation.right.columns) != 1:
            raise ValueError("RelationshipEvidenceV1 compatibility supports scalar tuples only")
        return cls(
            left_physical_id=observation.left.physical_id,
            left_binding_revision_id=observation.left.binding_revision_id,
            left_binding_content_hash=observation.left.binding_content_hash,
            left_column=observation.left.columns[0],
            right_physical_id=observation.right.physical_id,
            right_binding_revision_id=observation.right.binding_revision_id,
            right_binding_content_hash=observation.right.binding_content_hash,
            right_column=observation.right.columns[0],
            left_rows=observation.left.row_count,
            left_distinct=observation.left.distinct_tuple_count,
            left_nulls=observation.left.null_row_count,
            right_rows=observation.right.row_count,
            right_distinct=observation.right.distinct_tuple_count,
            right_nulls=observation.right.null_row_count,
            matched_distinct=observation.matched_left_distinct,
            unmatched_distinct=observation.unmatched_left_distinct,
            max_left_rows_per_tuple=observation.left.max_rows_per_tuple,
            max_right_matches_per_left_row=(
                observation.max_right_matches_per_left_row),
            method=observation.method,
            complete=observation.complete,
            row_coverage=observation.row_coverage.value,
            left_partitions_read=observation.left.partitions_read,
            right_partitions_read=observation.right.partitions_read,
        )


#: Why a piece of evidence does not support a join. Named here rather than in `analysis.py` because
#: the rule is about relationships, not about one query shape — a second consumer would otherwise
#: re-derive it and drift.
JOIN_EVIDENCE_MISSING = "JOIN_EVIDENCE_MISSING"
JOIN_EVIDENCE_MISMATCHED = "JOIN_EVIDENCE_MISMATCHED"
JOIN_KEY_NOT_UNIQUE = "JOIN_KEY_NOT_UNIQUE"
JOIN_UNIQUENESS_UNKNOWN = "JOIN_UNIQUENESS_UNKNOWN"

RelationshipEvidence: TypeAlias = RelationshipEvidenceV1 | RelationshipObservationV2


def join_refusal(evidence: RelationshipEvidence | None, *,
                 referencing_table_id: str, referencing_column: str,
                 referenced_table_id: str, referenced_column: str) -> tuple[str, str] | None:
    """`(code, message)` if this evidence does not support joining these two columns, else `None`.

    This is the Release 2 half of the sentence at the top of this module: Release 1 produces the
    evidence, and this decides what it may support — for exactly one use, an analysis join.

    Three refusals, in order of how badly they mislead:

    * **no evidence** — the relationship has never been looked at. Refusal is the default, because
      an analysis that joins on an unexamined relationship is indistinguishable from one that joins
      on a verified relationship once the result is in a spreadsheet.
    * **mismatched evidence** — worse than none. It reads as verification in the audit trail while
      describing a relationship the query does not perform.
    * **the referenced key is not unique, or cannot be shown to be** — the correctness property. In
      a population-spine analysis the referenced side is the LEFT side of the query, so a duplicate
      key multiplies the whole population and overstates every total that includes it.

    The uniqueness test goes through the evidence contract's ``uniqueness_verdict`` rather than
    reading a raw boolean, because only that method knows an approximate/partial probe may not
    assert uniqueness. Reading the raw property is how a cheap profile silently promotes a bad key.

    The referencing side is deliberately NOT required to be unique: many transactions per customer
    is the expected shape, and the analysis aggregates that side before joining anyway.
    """
    if evidence is None:
        return (JOIN_EVIDENCE_MISSING,
                f"no relationship evidence for {referencing_table_id}.{referencing_column} -> "
                f"{referenced_table_id}.{referenced_column}: the join has never been observed")

    if isinstance(evidence, RelationshipObservationV2):
        actual = (
            evidence.left.physical_id,
            evidence.left.columns,
            evidence.right.physical_id,
            evidence.right.columns,
        )
        expected = (
            referencing_table_id,
            (referencing_column,),
            referenced_table_id,
            (referenced_column,),
        )
    else:
        actual = (
            evidence.left_physical_id,
            (evidence.left_column,),
            evidence.right_physical_id,
            (evidence.right_column,),
        )
        expected = (
            referencing_table_id,
            (referencing_column,),
            referenced_table_id,
            (referenced_column,),
        )
    if actual != expected:
        return (JOIN_EVIDENCE_MISMATCHED,
                f"evidence describes {actual[0]}.{actual[1]} -> {actual[2]}.{actual[3]}, but the "
                f"join is {expected[0]}.{expected[1]} -> {expected[2]}.{expected[3]}")

    verdict = evidence.uniqueness_verdict("right")
    if (
        isinstance(evidence, RelationshipObservationV2)
        and evidence.right.partitions_read
        and verdict == "unique"
    ):
        # This analysis IR has no partition-scoped join contract. Evidence proving the target key
        # only in named partitions cannot authorize its unrestricted spine join.
        verdict = "unknown"
    if verdict == "not_unique":
        if isinstance(evidence, RelationshipObservationV2):
            right_rows = evidence.right.row_count
            right_distinct = evidence.right.distinct_tuple_count
        else:
            right_rows = evidence.right_rows
            right_distinct = evidence.right_distinct
        return (JOIN_KEY_NOT_UNIQUE,
                f"{referenced_table_id}.{referenced_column} is not unique "
                f"({right_rows} rows, {right_distinct} distinct); joining on it "
                "would duplicate the population and overstate every total")
    if verdict == "unknown":
        return (JOIN_UNIQUENESS_UNKNOWN,
                f"uniqueness of {referenced_table_id}.{referenced_column} was probed with method "
                f"{evidence.method!r}; only an exact probe may assert a key")
    return None


def observe_relationship(conn, probe: RelationshipProbeV1, *, dialect) -> RelationshipEvidenceV1:
    """Compatibility seam backed by the one tuple-aware V2 executor."""
    observation = DirectSqlExecutor(conn, dialect).observe_relationship(probe.to_v2())
    return RelationshipEvidenceV1.from_v2(observation)

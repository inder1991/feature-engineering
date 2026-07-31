"""Tuple-aware, bounded observations for one directional identifier relationship.

The plan is data-plane executable and contains no free-form SQL. It pins both physical binding
revisions and source snapshots, requires partition selectors wherever a binding is partitioned, and
records the exact normalization/predicate vocabulary tested. The result contains aggregates only:
no identifier value can cross the boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from featuregen.data_agent.observation import (
    ObservationPlanError,
    ObservationPlanV1,
    PartitionSelector,
    require_identifier,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.materialize.canonical import materialize_hash

RELATIONSHIP_OBSERVATION_CONTRACT_VERSION = "2.0.0"
MAX_RELATIONSHIP_SHORTLIST = 20

RELATIONSHIP_EMPTY_TUPLE = "RELATIONSHIP_EMPTY_TUPLE"
RELATIONSHIP_TUPLE_ARITY_MISMATCH = "RELATIONSHIP_TUPLE_ARITY_MISMATCH"
RELATIONSHIP_DUPLICATE_TUPLE_MEMBER = "RELATIONSHIP_DUPLICATE_TUPLE_MEMBER"
RELATIONSHIP_UNKNOWN_NORMALIZATION = "RELATIONSHIP_UNKNOWN_NORMALIZATION"
RELATIONSHIP_SCOPE_MISMATCH = "RELATIONSHIP_SCOPE_MISMATCH"
RELATIONSHIP_NOT_SHORTLISTED = "RELATIONSHIP_NOT_SHORTLISTED"
RELATIONSHIP_UNSUPPORTED_ROW_COVERAGE = "RELATIONSHIP_UNSUPPORTED_ROW_COVERAGE"


class RowCoverage(StrEnum):
    FULL = "full"
    SAMPLED = "sampled"
    PARTIAL = "partial"


class RelationshipMethod(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"


class NormalizationId(StrEnum):
    IDENTITY_V1 = "identity_v1"
    TRIM_LOWER_V1 = "trim_lower_v1"


@dataclass(frozen=True, slots=True)
class RelationshipColumnPairV2:
    left_column: str
    right_column: str
    normalization_id: str = NormalizationId.IDENTITY_V1.value

    def __post_init__(self) -> None:
        require_identifier(self.left_column, what="left relationship column")
        require_identifier(self.right_column, what="right relationship column")
        try:
            NormalizationId(self.normalization_id)
        except ValueError as exc:
            raise ObservationPlanError(
                RELATIONSHIP_UNKNOWN_NORMALIZATION,
                f"normalization {self.normalization_id!r} is not supported",
            ) from exc


@dataclass(frozen=True, slots=True)
class RelationshipFixedPredicateV1:
    """A governed equality-to-literal predicate on one endpoint.

    Values are quoted literals; ``predicate_id`` is the stable policy/ontology identifier recorded
    in the evidence. Arbitrary SQL is intentionally impossible.
    """

    predicate_id: str
    side: str
    column: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.predicate_id.strip():
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH, "predicate_id must not be blank")
        if self.side not in {"left", "right"}:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                f"predicate side must be left or right, got {self.side!r}",
            )
        require_identifier(self.column, what="relationship predicate column")
        if not self.values:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                f"predicate {self.predicate_id!r} must select at least one value",
            )


@dataclass(frozen=True, slots=True)
class RelationshipObservationScopeV1:
    """The exact applicability and read scope one relationship probe claims."""

    scope_id: str
    left_binding_revision_id: str
    right_binding_revision_id: str
    left_partitions: PartitionSelector | None
    right_partitions: PartitionSelector | None
    normalization_ids: tuple[str, ...]
    predicates: tuple[RelationshipFixedPredicateV1, ...] = ()
    snapshot_or_as_of: str | None = None
    execution_principal: str = ""
    method: RelationshipMethod = RelationshipMethod.EXACT
    row_coverage: RowCoverage = RowCoverage.FULL

    def __post_init__(self) -> None:
        for name in (
            "scope_id",
            "left_binding_revision_id",
            "right_binding_revision_id",
            "execution_principal",
        ):
            if not str(getattr(self, name)).strip():
                raise ObservationPlanError(
                    RELATIONSHIP_SCOPE_MISMATCH, f"{name} must not be blank")
        normalizations = tuple(self.normalization_ids)
        if not normalizations:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                "relationship scope must name the tested normalization for every tuple member",
            )
        for normalization in normalizations:
            try:
                NormalizationId(normalization)
            except ValueError as exc:
                raise ObservationPlanError(
                    RELATIONSHIP_UNKNOWN_NORMALIZATION,
                    f"normalization {normalization!r} is not supported",
                ) from exc
        if len(set(p.predicate_id for p in self.predicates)) != len(self.predicates):
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH, "predicate_id values must be unique")

    def identity_payload(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "left_binding_revision_id": self.left_binding_revision_id,
            "right_binding_revision_id": self.right_binding_revision_id,
            "left_partitions": _selector_payload(self.left_partitions),
            "right_partitions": _selector_payload(self.right_partitions),
            "normalization_ids": list(self.normalization_ids),
            "predicates": [asdict(predicate) for predicate in self.predicates],
            "snapshot_or_as_of": self.snapshot_or_as_of,
            "execution_principal": self.execution_principal,
            "method": self.method.value,
            "row_coverage": self.row_coverage.value,
        }


def _selector_payload(selector: PartitionSelector | None) -> dict[str, object] | None:
    if selector is None:
        return None
    return {"column": selector.column, "values": list(selector.values)}


@dataclass(frozen=True, slots=True)
class RelationshipObservationPlanV2:
    left_binding: PhysicalDatasetBindingV1
    right_binding: PhysicalDatasetBindingV1
    column_pairs: tuple[RelationshipColumnPairV2, ...]
    scope: RelationshipObservationScopeV1
    policy: ProfilePolicyV1
    realization_revision_id: str
    left_source_snapshot_id: str
    right_source_snapshot_id: str
    shortlist_position: int
    shortlist_size: int

    def __post_init__(self) -> None:
        pairs = tuple(self.column_pairs)
        object.__setattr__(self, "column_pairs", pairs)
        if not pairs:
            raise ObservationPlanError(
                RELATIONSHIP_EMPTY_TUPLE, "a relationship tuple must not be empty")
        left = tuple(pair.left_column.strip().lower() for pair in pairs)
        right = tuple(pair.right_column.strip().lower() for pair in pairs)
        if len(set(left)) != len(left) or len(set(right)) != len(right):
            raise ObservationPlanError(
                RELATIONSHIP_DUPLICATE_TUPLE_MEMBER,
                "a relationship tuple must not repeat either endpoint member",
            )
        if self.scope.left_binding_revision_id != self.left_binding.binding_revision_id:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                "scope left binding revision does not match the executable binding",
            )
        if self.scope.right_binding_revision_id != self.right_binding.binding_revision_id:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                "scope right binding revision does not match the executable binding",
            )
        pair_normalizations = tuple(pair.normalization_id for pair in pairs)
        if self.scope.normalization_ids != pair_normalizations:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                "scope normalization_ids do not match the ordered column pairs",
            )
        requested_method = (
            RelationshipMethod.EXACT
            if self.policy.exact_distinct
            else RelationshipMethod.APPROXIMATE
        )
        if self.scope.method is not requested_method:
            raise ObservationPlanError(
                RELATIONSHIP_SCOPE_MISMATCH,
                "scope method does not match the profile policy",
            )
        if self.scope.row_coverage is not RowCoverage.FULL:
            raise ObservationPlanError(
                RELATIONSHIP_UNSUPPORTED_ROW_COVERAGE,
                "the direct SQL relationship executor supports full reads of the declared "
                "partition scope; sampled/partial results may be stored but not requested here",
            )
        if (
            self.shortlist_position < 1
            or self.shortlist_size < self.shortlist_position
            or self.shortlist_size > MAX_RELATIONSHIP_SHORTLIST
        ):
            raise ObservationPlanError(
                RELATIONSHIP_NOT_SHORTLISTED,
                f"relationship must be in positions 1..{MAX_RELATIONSHIP_SHORTLIST} of a bounded "
                f"shortlist, got position={self.shortlist_position}, size={self.shortlist_size}",
            )
        for name in (
            "realization_revision_id",
            "left_source_snapshot_id",
            "right_source_snapshot_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ObservationPlanError(
                    RELATIONSHIP_SCOPE_MISMATCH, f"{name} must not be blank")

        # Reuse the single-table observation admission seam for identifiers, caps and the
        # load-bearing "partitioned table requires a selector" rule on BOTH sides.
        left_predicate_columns = tuple(
            predicate.column for predicate in self.scope.predicates
            if predicate.side == "left"
        )
        right_predicate_columns = tuple(
            predicate.column for predicate in self.scope.predicates
            if predicate.side == "right"
        )
        ObservationPlanV1(
            binding=self.left_binding,
            columns=tuple(dict.fromkeys((*left, *left_predicate_columns))),
            policy=self.policy,
            partitions=self.scope.left_partitions,
        )
        ObservationPlanV1(
            binding=self.right_binding,
            columns=tuple(dict.fromkeys((*right, *right_predicate_columns))),
            policy=self.policy,
            partitions=self.scope.right_partitions,
        )

    @property
    def requested_method(self) -> str:
        return self.scope.method.value

    @property
    def plan_hash(self) -> str:
        return materialize_hash({
            "contract_version": RELATIONSHIP_OBSERVATION_CONTRACT_VERSION,
            "realization_revision_id": self.realization_revision_id,
            "left_binding_revision_id": self.left_binding.binding_revision_id,
            "right_binding_revision_id": self.right_binding.binding_revision_id,
            "column_pairs": [asdict(pair) for pair in self.column_pairs],
            "scope": self.scope.identity_payload(),
            "left_source_snapshot_id": self.left_source_snapshot_id,
            "right_source_snapshot_id": self.right_source_snapshot_id,
        })


@dataclass(frozen=True, slots=True)
class EndpointTupleObservationV2:
    physical_id: str
    binding_revision_id: str
    binding_content_hash: str
    columns: tuple[str, ...]
    row_count: int
    non_null_row_count: int
    distinct_tuple_count: int
    duplicate_tuple_count: int
    duplicate_row_count: int
    max_rows_per_tuple: int
    partitions_read: tuple[str, ...] = ()

    @property
    def null_row_count(self) -> int:
        return max(0, self.row_count - self.non_null_row_count)

    @property
    def observed_unique(self) -> bool:
        return (
            self.row_count > 0
            and self.null_row_count == 0
            and self.max_rows_per_tuple <= 1
            and self.distinct_tuple_count == self.row_count
        )


@dataclass(frozen=True, slots=True)
class RelationshipObservationV2:
    realization_revision_id: str
    plan_hash: str
    scope_id: str
    left: EndpointTupleObservationV2
    right: EndpointTupleObservationV2
    matched_left_distinct: int
    unmatched_left_distinct: int
    matched_right_distinct: int
    unmatched_right_distinct: int
    left_orphan_rows: int
    right_orphan_rows: int
    joined_row_count: int
    max_right_matches_per_left_row: int
    max_left_matches_per_right_row: int
    normalization_ids: tuple[str, ...]
    predicate_ids: tuple[str, ...]
    left_source_snapshot_id: str
    right_source_snapshot_id: str
    snapshot_or_as_of: str | None
    execution_principal: str
    method: str
    row_coverage: RowCoverage
    complete: bool
    observed_at: datetime
    failures: tuple[str, ...] = field(default_factory=tuple)
    producer: str = "profiler"
    strength: str = "supported"

    @property
    def left_distinct_containment_ratio(self) -> float:
        return (
            self.matched_left_distinct / self.left.distinct_tuple_count
            if self.left.distinct_tuple_count
            else 0.0
        )

    @property
    def right_distinct_containment_ratio(self) -> float:
        return (
            self.matched_right_distinct / self.right.distinct_tuple_count
            if self.right.distinct_tuple_count
            else 0.0
        )

    @property
    def left_distinct_orphan_ratio(self) -> float:
        return (
            self.unmatched_left_distinct / self.left.distinct_tuple_count
            if self.left.distinct_tuple_count
            else 0.0
        )

    @property
    def right_distinct_orphan_ratio(self) -> float:
        return (
            self.unmatched_right_distinct / self.right.distinct_tuple_count
            if self.right.distinct_tuple_count
            else 0.0
        )

    @property
    def left_row_orphan_ratio(self) -> float:
        return (
            self.left_orphan_rows / self.left.non_null_row_count
            if self.left.non_null_row_count
            else 0.0
        )

    @property
    def right_row_orphan_ratio(self) -> float:
        return (
            self.right_orphan_rows / self.right.non_null_row_count
            if self.right.non_null_row_count
            else 0.0
        )

    @property
    def left_row_amplification_ratio(self) -> float:
        return (
            self.joined_row_count / self.left.non_null_row_count
            if self.left.non_null_row_count
            else 0.0
        )

    @property
    def right_row_amplification_ratio(self) -> float:
        return (
            self.joined_row_count / self.right.non_null_row_count
            if self.right.non_null_row_count
            else 0.0
        )

    @property
    def observed_cardinality(self) -> str:
        if self.left.observed_unique and self.right.observed_unique:
            return "one_to_one"
        if self.right.observed_unique:
            return "many_to_one"
        if self.left.observed_unique:
            return "one_to_many"
        return "many_to_many"

    def uniqueness_verdict(self, side: str) -> str:
        """``unique``/``not_unique``/``unknown`` with asymmetric evidence semantics."""
        endpoint = self.left if side == "left" else self.right
        if endpoint.duplicate_row_count > 0 or endpoint.null_row_count > 0:
            return "not_unique"
        if endpoint.row_count == 0:
            return "unknown"
        if (
            not self.complete
            or self.row_coverage is not RowCoverage.FULL
            or self.method != RelationshipMethod.EXACT.value
        ):
            return "unknown"
        return "unique" if endpoint.observed_unique else "not_unique"

    @property
    def observation_revision_id(self) -> str:
        return "rob_" + materialize_hash({
            "contract_version": RELATIONSHIP_OBSERVATION_CONTRACT_VERSION,
            **observation_to_json(self),
        })

    @property
    def current_scope_key(self) -> str:
        """Same relationship/applicability/principal; source snapshots may advance."""
        return materialize_hash({
            "realization_revision_id": self.realization_revision_id,
            "scope_id": self.scope_id,
            "left_binding_revision_id": self.left.binding_revision_id,
            "right_binding_revision_id": self.right.binding_revision_id,
            "left_columns": list(self.left.columns),
            "right_columns": list(self.right.columns),
            "left_partitions": list(self.left.partitions_read),
            "right_partitions": list(self.right.partitions_read),
            "normalization_ids": list(self.normalization_ids),
            "predicate_ids": list(self.predicate_ids),
            "snapshot_or_as_of": self.snapshot_or_as_of,
            "execution_principal": self.execution_principal,
        })


def failed_relationship_observation(
    plan: RelationshipObservationPlanV2,
    *,
    method: str,
    observed_at: datetime,
    reason: str,
) -> RelationshipObservationV2:
    def endpoint(binding, columns, partitions):
        return EndpointTupleObservationV2(
            binding.identity.table_id,
            binding.binding_revision_id,
            binding.content_hash,
            columns,
            0,
            0,
            0,
            0,
            0,
            0,
            tuple(partitions.values) if partitions else (),
        )

    return RelationshipObservationV2(
        realization_revision_id=plan.realization_revision_id,
        plan_hash=plan.plan_hash,
        scope_id=plan.scope.scope_id,
        left=endpoint(
            plan.left_binding,
            tuple(pair.left_column for pair in plan.column_pairs),
            plan.scope.left_partitions,
        ),
        right=endpoint(
            plan.right_binding,
            tuple(pair.right_column for pair in plan.column_pairs),
            plan.scope.right_partitions,
        ),
        matched_left_distinct=0,
        unmatched_left_distinct=0,
        matched_right_distinct=0,
        unmatched_right_distinct=0,
        left_orphan_rows=0,
        right_orphan_rows=0,
        joined_row_count=0,
        max_right_matches_per_left_row=0,
        max_left_matches_per_right_row=0,
        normalization_ids=plan.scope.normalization_ids,
        predicate_ids=tuple(p.predicate_id for p in plan.scope.predicates),
        left_source_snapshot_id=plan.left_source_snapshot_id,
        right_source_snapshot_id=plan.right_source_snapshot_id,
        snapshot_or_as_of=plan.scope.snapshot_or_as_of,
        execution_principal=plan.scope.execution_principal,
        method=method,
        row_coverage=RowCoverage.PARTIAL,
        complete=False,
        observed_at=observed_at,
        failures=(reason,),
    )


def observation_to_json(observation: RelationshipObservationV2) -> dict[str, object]:
    payload = asdict(observation)
    for endpoint_name in ("left", "right"):
        endpoint = payload[endpoint_name]
        assert isinstance(endpoint, dict)
        endpoint["columns"] = list(endpoint["columns"])
        endpoint["partitions_read"] = list(endpoint["partitions_read"])
    payload["normalization_ids"] = list(observation.normalization_ids)
    payload["predicate_ids"] = list(observation.predicate_ids)
    payload["row_coverage"] = observation.row_coverage.value
    payload["observed_at"] = observation.observed_at.isoformat()
    payload["failures"] = list(observation.failures)
    return payload


def _endpoint_from_json(payload: dict[str, Any]) -> EndpointTupleObservationV2:
    return EndpointTupleObservationV2(
        physical_id=str(payload["physical_id"]),
        binding_revision_id=str(payload["binding_revision_id"]),
        binding_content_hash=str(payload["binding_content_hash"]),
        columns=tuple(str(value) for value in payload["columns"]),
        row_count=int(payload["row_count"]),
        non_null_row_count=int(payload["non_null_row_count"]),
        distinct_tuple_count=int(payload["distinct_tuple_count"]),
        duplicate_tuple_count=int(payload["duplicate_tuple_count"]),
        duplicate_row_count=int(payload["duplicate_row_count"]),
        max_rows_per_tuple=int(payload["max_rows_per_tuple"]),
        partitions_read=tuple(str(value) for value in payload["partitions_read"]),
    )


def observation_from_json(payload: dict[str, Any]) -> RelationshipObservationV2:
    return RelationshipObservationV2(
        realization_revision_id=payload["realization_revision_id"],
        plan_hash=payload["plan_hash"],
        scope_id=payload["scope_id"],
        left=_endpoint_from_json(payload["left"]),
        right=_endpoint_from_json(payload["right"]),
        matched_left_distinct=int(payload["matched_left_distinct"]),
        unmatched_left_distinct=int(payload["unmatched_left_distinct"]),
        matched_right_distinct=int(payload["matched_right_distinct"]),
        unmatched_right_distinct=int(payload["unmatched_right_distinct"]),
        left_orphan_rows=int(payload["left_orphan_rows"]),
        right_orphan_rows=int(payload["right_orphan_rows"]),
        joined_row_count=int(payload["joined_row_count"]),
        max_right_matches_per_left_row=int(
            payload["max_right_matches_per_left_row"]),
        max_left_matches_per_right_row=int(
            payload["max_left_matches_per_right_row"]),
        normalization_ids=tuple(payload["normalization_ids"]),
        predicate_ids=tuple(payload["predicate_ids"]),
        left_source_snapshot_id=payload["left_source_snapshot_id"],
        right_source_snapshot_id=payload["right_source_snapshot_id"],
        snapshot_or_as_of=payload.get("snapshot_or_as_of"),
        execution_principal=payload["execution_principal"],
        method=payload["method"],
        row_coverage=RowCoverage(payload["row_coverage"]),
        complete=bool(payload["complete"]),
        observed_at=datetime.fromisoformat(payload["observed_at"]),
        failures=tuple(payload.get("failures") or ()),
        producer=payload.get("producer", "profiler"),
        strength=payload.get("strength", "supported"),
    )


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _normalization(expression: str, normalization_id: str) -> str:
    if normalization_id == NormalizationId.IDENTITY_V1.value:
        return expression
    if normalization_id == NormalizationId.TRIM_LOWER_V1.value:
        return f"LOWER(TRIM({expression}))"
    raise AssertionError(f"plan admitted unknown normalization {normalization_id!r}")


def render_relationship_probe_sql(plan: RelationshipObservationPlanV2, *, dialect) -> str:
    """Render one portable aggregate statement. Uses no PostgreSQL ``FILTER`` syntax."""

    class _Shim:
        def __init__(self, binding):
            self.binding = binding

    q = dialect.ident
    left_table = dialect.table_ref(_Shim(plan.left_binding))
    right_table = dialect.table_ref(_Shim(plan.right_binding))
    left_keys = tuple(
        _normalization(q(pair.left_column), pair.normalization_id)
        for pair in plan.column_pairs
    )
    right_keys = tuple(
        _normalization(q(pair.right_column), pair.normalization_id)
        for pair in plan.column_pairs
    )
    aliases = tuple(f"k{index}" for index in range(len(plan.column_pairs)))

    def raw_select(expressions: tuple[str, ...]) -> str:
        return ", ".join(
            f"{expression} AS {q(alias)}"
            for expression, alias in zip(expressions, aliases, strict=True)
        )

    def predicates(side: str, selector: PartitionSelector | None) -> str:
        terms: list[str] = []
        if selector is not None:
            values = ", ".join(_literal(value) for value in selector.values)
            terms.append(f"{q(selector.column)} IN ({values})")
        for predicate in plan.scope.predicates:
            if predicate.side != side:
                continue
            values = ", ".join(_literal(value) for value in predicate.values)
            terms.append(f"{q(predicate.column)} IN ({values})")
        return " AND ".join(terms)

    left_where = predicates("left", plan.scope.left_partitions)
    right_where = predicates("right", plan.scope.right_partitions)
    non_null = " AND ".join(f"{q(alias)} IS NOT NULL" for alias in aliases)
    group_keys = ", ".join(q(alias) for alias in aliases)
    join = " AND ".join(
        f"l.{q(alias)} = r.{q(alias)}" for alias in aliases)
    right_absent = f"r.{q(aliases[0])} IS NULL"
    left_absent = f"l.{q(aliases[0])} IS NULL"

    def raw_cte(name: str, table: str, expressions: tuple[str, ...], where: str) -> str:
        suffix = f" WHERE {where}" if where else ""
        return f"{name} AS (SELECT {raw_select(expressions)} FROM {table}{suffix})"

    ctes = [
        raw_cte("left_raw", left_table, left_keys, left_where),
        raw_cte("right_raw", right_table, right_keys, right_where),
        (
            "left_raw_stats AS (SELECT COUNT(*) AS row_count, "
            f"COALESCE(SUM(CASE WHEN {non_null} THEN 1 ELSE 0 END), 0) AS non_null_count "
            "FROM left_raw)"
        ),
        (
            "right_raw_stats AS (SELECT COUNT(*) AS row_count, "
            f"COALESCE(SUM(CASE WHEN {non_null} THEN 1 ELSE 0 END), 0) AS non_null_count "
            "FROM right_raw)"
        ),
        (
            f"left_groups AS (SELECT {group_keys}, COUNT(*) AS n FROM left_raw "
            f"WHERE {non_null} GROUP BY {group_keys})"
        ),
        (
            f"right_groups AS (SELECT {group_keys}, COUNT(*) AS n FROM right_raw "
            f"WHERE {non_null} GROUP BY {group_keys})"
        ),
        (
            "left_group_stats AS (SELECT COUNT(*) AS distinct_count, "
            "COALESCE(SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0) AS duplicate_tuples, "
            "COALESCE(SUM(n - 1), 0) AS duplicate_count, "
            "COALESCE(MAX(n), 0) AS max_rows FROM left_groups)"
        ),
        (
            "right_group_stats AS (SELECT COUNT(*) AS distinct_count, "
            "COALESCE(SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0) AS duplicate_tuples, "
            "COALESCE(SUM(n - 1), 0) AS duplicate_count, "
            "COALESCE(MAX(n), 0) AS max_rows FROM right_groups)"
        ),
        (
            "pair_groups AS (SELECT l.n AS left_n, r.n AS right_n "
            f"FROM left_groups l JOIN right_groups r ON {join})"
        ),
        (
            "pair_stats AS (SELECT COUNT(*) AS matched_groups, "
            "COALESCE(SUM(left_n * right_n), 0) AS joined_rows, "
            "COALESCE(MAX(right_n), 0) AS max_right_matches, "
            "COALESCE(MAX(left_n), 0) AS max_left_matches FROM pair_groups)"
        ),
        (
            "left_orphan_stats AS (SELECT COALESCE(SUM(l.n), 0) AS orphan_rows "
            f"FROM left_groups l LEFT JOIN right_groups r ON {join} WHERE {right_absent})"
        ),
        (
            "right_orphan_stats AS (SELECT COALESCE(SUM(r.n), 0) AS orphan_rows "
            f"FROM right_groups r LEFT JOIN left_groups l ON {join} WHERE {left_absent})"
        ),
    ]
    return (
        "WITH " + ",\n     ".join(ctes) + "\n"
        "SELECT lrs.row_count, lrs.non_null_count, "
        "lgs.distinct_count, lgs.duplicate_tuples, lgs.duplicate_count, lgs.max_rows, "
        "rrs.row_count, rrs.non_null_count, "
        "rgs.distinct_count, rgs.duplicate_tuples, rgs.duplicate_count, rgs.max_rows, "
        "ps.matched_groups, los.orphan_rows, ros.orphan_rows, "
        "ps.joined_rows, ps.max_right_matches, ps.max_left_matches\n"
        "FROM left_raw_stats lrs CROSS JOIN left_group_stats lgs "
        "CROSS JOIN right_raw_stats rrs CROSS JOIN right_group_stats rgs "
        "CROSS JOIN pair_stats ps CROSS JOIN left_orphan_stats los "
        "CROSS JOIN right_orphan_stats ros"
    )


def utc_now() -> datetime:
    return datetime.now(UTC)

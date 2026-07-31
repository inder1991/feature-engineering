from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from featuregen.data_agent.executor import DirectSqlExecutor
from featuregen.data_agent.observation import PartitionSelector
from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
)
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship import JOIN_UNIQUENESS_UNKNOWN, join_refusal
from featuregen.data_agent.relationship_observation import (
    NormalizationId,
    RelationshipColumnPairV2,
    RelationshipFixedPredicateV1,
    RelationshipMethod,
    RelationshipObservationPlanV2,
    RelationshipObservationScopeV1,
    RowCoverage,
)
from featuregen.data_agent.sql_postgres import PostgresDialect

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


@pytest.fixture
def relationship_tables(db):
    db.execute("CREATE SCHEMA IF NOT EXISTS relationship_v2")
    db.execute("DROP TABLE IF EXISTS relationship_v2.events")
    db.execute("DROP TABLE IF EXISTS relationship_v2.customers")
    db.execute(
        "CREATE TABLE relationship_v2.events "
        "(customer_id text, business_dt text, partition_dt text)"
    )
    db.execute(
        "CREATE TABLE relationship_v2.customers "
        "(customer_id text, business_dt text, partition_dt text)"
    )
    # Left/source: A occurs six times across two dates, B once, C is an orphan, plus one NULL.
    for row in (
        *[("A", "2026-07-01", "p1")] * 3,
        *[("A", "2026-07-02", "p1")] * 3,
        ("B", "2026-07-01", "p1"),
        ("C", "2026-07-01", "p1"),
        (None, "2026-07-01", "p1"),
    ):
        db.execute("INSERT INTO relationship_v2.events VALUES (%s,%s,%s)", row)
    # Scalar A is duplicated because this is history; (customer_id,business_dt) is unique.
    for row in (
        ("A", "2026-07-01", "p1"),
        ("A", "2026-07-02", "p1"),
        ("B", "2026-07-01", "p1"),
        ("D", "2026-07-01", "p1"),
    ):
        db.execute("INSERT INTO relationship_v2.customers VALUES (%s,%s,%s)", row)
    return db


def _binding(table: str, *, partitioned: bool = False) -> PhysicalDatasetBindingV1:
    identity = PhysicalObjectIdentityV1(
        catalog_source="pilot",
        database="featuregen_test",
        schema="relationship_v2",
        table=table,
        object_kind="table",
    )
    return PhysicalDatasetBindingV1(
        binding_id=f"binding-{table}",
        catalog_logical_ref=f"pilot::public.{table}",
        connection_id="local-postgres",
        identity=identity,
        partition_columns=("partition_dt",) if partitioned else (),
        business_time_column="partition_dt" if partitioned else None,
    )


def _plan(
    *,
    pairs: tuple[RelationshipColumnPairV2, ...] | None = None,
    left_binding: PhysicalDatasetBindingV1 | None = None,
    right_binding: PhysicalDatasetBindingV1 | None = None,
    left_partitions: PartitionSelector | None = None,
    right_partitions: PartitionSelector | None = None,
    predicates: tuple[RelationshipFixedPredicateV1, ...] = (),
) -> RelationshipObservationPlanV2:
    left = left_binding or _binding("events")
    right = right_binding or _binding("customers")
    column_pairs = pairs or (
        RelationshipColumnPairV2("customer_id", "customer_id"),)
    scope = RelationshipObservationScopeV1(
        scope_id="pilot-customer-link",
        left_binding_revision_id=left.binding_revision_id,
        right_binding_revision_id=right.binding_revision_id,
        left_partitions=left_partitions,
        right_partitions=right_partitions,
        normalization_ids=tuple(pair.normalization_id for pair in column_pairs),
        predicates=predicates,
        snapshot_or_as_of="2026-07-30",
        execution_principal="pilot-profiler",
        method=RelationshipMethod.EXACT,
    )
    return RelationshipObservationPlanV2(
        left_binding=left,
        right_binding=right,
        column_pairs=column_pairs,
        scope=scope,
        policy=ProfilePolicyV1(exact_distinct=True),
        realization_revision_id="realization-pilot-v1",
        left_source_snapshot_id="left-snapshot-1",
        right_source_snapshot_id="right-snapshot-1",
        shortlist_position=1,
        shortlist_size=3,
    )


def _run(conn, plan=None):
    return DirectSqlExecutor(
        conn, PostgresDialect()).observe_relationship(plan or _plan(), observed_at=NOW)


def test_scalar_history_exposes_real_fanout_without_misnaming_source_frequency(
    relationship_tables,
) -> None:
    result = _run(relationship_tables)
    assert result.left.row_count == 9
    assert result.left.non_null_row_count == 8
    assert result.left.distinct_tuple_count == 3
    assert result.left.duplicate_tuple_count == 1
    assert result.left.duplicate_row_count == 5
    assert result.left.max_rows_per_tuple == 6

    assert result.right.row_count == 4
    assert result.right.distinct_tuple_count == 3
    assert result.right.duplicate_tuple_count == 1
    assert result.right.duplicate_row_count == 1
    assert result.max_right_matches_per_left_row == 2
    assert result.max_left_matches_per_right_row == 6
    assert result.joined_row_count == 13
    assert result.observed_cardinality == "many_to_many"


def test_business_date_pair_turns_history_into_many_to_one(
    relationship_tables,
) -> None:
    result = _run(
        relationship_tables,
        _plan(
            pairs=(
                RelationshipColumnPairV2("customer_id", "customer_id"),
                RelationshipColumnPairV2("business_dt", "business_dt"),
            )
        ),
    )
    assert result.right.observed_unique
    assert result.max_right_matches_per_left_row == 1
    assert result.max_left_matches_per_right_row == 3
    assert result.joined_row_count == 7
    assert result.observed_cardinality == "many_to_one"
    assert result.unmatched_left_distinct == 1
    assert result.left_orphan_rows == 1
    assert result.unmatched_right_distinct == 1
    assert result.right_orphan_rows == 1


def test_named_snapshot_predicate_turns_history_into_many_to_one(
    relationship_tables,
) -> None:
    result = _run(
        relationship_tables,
        _plan(
            predicates=(
                RelationshipFixedPredicateV1(
                    predicate_id="customer_snapshot_2026_07_01",
                    side="right",
                    column="business_dt",
                    values=("2026-07-01",),
                ),
            ),
        ),
    )
    assert result.right.observed_unique
    assert result.max_right_matches_per_left_row == 1
    assert result.observed_cardinality == "many_to_one"
    assert result.predicate_ids == ("customer_snapshot_2026_07_01",)


def test_six_source_rows_for_one_unique_target_have_join_multiplier_one(
    relationship_tables,
) -> None:
    relationship_tables.execute(
        "DELETE FROM relationship_v2.customers WHERE business_dt='2026-07-02'"
    )
    result = _run(relationship_tables)
    assert result.left.max_rows_per_tuple == 6
    assert result.max_right_matches_per_left_row == 1
    assert result.left_row_amplification_ratio == pytest.approx(7 / 8)


def test_zero_overlap_is_reported_without_inventing_a_namespace_verdict(
    relationship_tables,
) -> None:
    relationship_tables.execute(
        "UPDATE relationship_v2.customers SET customer_id='Z' || customer_id"
    )
    result = _run(relationship_tables)
    assert result.matched_left_distinct == 0
    assert result.left_distinct_containment_ratio == 0.0
    assert result.left_orphan_rows == result.left.non_null_row_count
    assert not hasattr(result, "namespace_verdict")


def test_nulls_are_excluded_from_sql_equality_but_reported_and_conservative(
    relationship_tables,
) -> None:
    relationship_tables.execute(
        "INSERT INTO relationship_v2.customers VALUES "
        "(NULL,'2026-07-03','p1'),(NULL,'2026-07-04','p1')"
    )
    result = _run(
        relationship_tables,
        _plan(
            pairs=(
                RelationshipColumnPairV2("customer_id", "customer_id"),
                RelationshipColumnPairV2("business_dt", "business_dt"),
            )
        ),
    )
    assert result.right.null_row_count == 2
    assert result.right.duplicate_row_count == 0
    assert result.uniqueness_verdict("right") == "not_unique"
    assert result.joined_row_count == 7


def test_normalization_collisions_are_measured_not_hidden(relationship_tables) -> None:
    relationship_tables.execute("TRUNCATE relationship_v2.events, relationship_v2.customers")
    relationship_tables.execute(
        "INSERT INTO relationship_v2.events VALUES ('a','d','p1')"
    )
    relationship_tables.execute(
        "INSERT INTO relationship_v2.customers VALUES "
        "(' A','d','p1'),('a ','d','p1')"
    )
    normalized = RelationshipColumnPairV2(
        "customer_id",
        "customer_id",
        normalization_id=NormalizationId.TRIM_LOWER_V1.value,
    )
    result = _run(relationship_tables, _plan(pairs=(normalized,)))
    assert result.right.distinct_tuple_count == 1
    assert result.right.duplicate_tuple_count == 1
    assert result.right.duplicate_row_count == 1
    assert result.max_right_matches_per_left_row == 2


def test_empty_tables_and_execution_failure_are_honest(relationship_tables) -> None:
    relationship_tables.execute("TRUNCATE relationship_v2.events, relationship_v2.customers")
    empty = _run(relationship_tables)
    assert empty.complete
    assert empty.left.row_count == empty.right.row_count == 0
    assert empty.uniqueness_verdict("right") == "unknown"

    relationship_tables.execute("DROP TABLE relationship_v2.customers")
    failed = _run(relationship_tables)
    assert not failed.complete
    assert failed.row_coverage.value == "partial"
    assert failed.failures


def test_result_contains_only_aggregate_evidence_not_identifier_values(
    relationship_tables,
) -> None:
    text = repr(_run(relationship_tables))
    for value in ("'A'", "'B'", "'C'", "'D'"):
        assert value not in text


def test_shortlist_rank_is_admission_provenance_not_query_identity() -> None:
    plan = _plan()
    assert replace(plan, shortlist_position=2).plan_hash == plan.plan_hash


def test_sample_without_duplicates_cannot_prove_global_uniqueness(
    relationship_tables,
) -> None:
    relationship_tables.execute(
        "DELETE FROM relationship_v2.customers WHERE business_dt='2026-07-02'"
    )
    exact = _run(relationship_tables)
    assert exact.uniqueness_verdict("right") == "unique"
    sampled = replace(exact, row_coverage=RowCoverage.SAMPLED)
    assert sampled.uniqueness_verdict("right") == "unknown"


def test_both_partition_selectors_are_enforced_and_retained(
    relationship_tables,
) -> None:
    selector = PartitionSelector("partition_dt", ("p1",))
    plan = _plan(
        left_binding=_binding("events", partitioned=True),
        right_binding=_binding("customers", partitioned=True),
        left_partitions=selector,
        right_partitions=selector,
    )
    result = _run(relationship_tables, plan)
    assert result.left.partitions_read == ("p1",)
    assert result.right.partitions_read == ("p1",)


def test_partition_scoped_uniqueness_cannot_authorize_an_unrestricted_join(
    relationship_tables,
) -> None:
    relationship_tables.execute(
        "DELETE FROM relationship_v2.customers WHERE business_dt='2026-07-02'"
    )
    selector = PartitionSelector("partition_dt", ("p1",))
    result = _run(
        relationship_tables,
        _plan(
            left_binding=_binding("events", partitioned=True),
            right_binding=_binding("customers", partitioned=True),
            left_partitions=selector,
            right_partitions=selector,
        ),
    )
    assert result.uniqueness_verdict("right") == "unique"
    refusal = join_refusal(
        result,
        referencing_table_id=result.left.physical_id,
        referencing_column="customer_id",
        referenced_table_id=result.right.physical_id,
        referenced_column="customer_id",
    )
    assert refusal is not None and refusal[0] == JOIN_UNIQUENESS_UNKNOWN

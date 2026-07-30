from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.data_agent.observation import ObservationPlanError, PartitionSelector
from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
)
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship_observation import (
    RelationshipColumnPairV2,
    RelationshipMethod,
    RelationshipObservationPlanV2,
    RelationshipObservationScopeV1,
)
from featuregen.data_agent.sql_hive import HiveDialect


def _binding(source: str, table: str) -> PhysicalDatasetBindingV1:
    return PhysicalDatasetBindingV1(
        binding_id=f"binding-{source}-{table}",
        catalog_logical_ref=f"{source}::public.{table}",
        connection_id="hiveserver2-pilot",
        identity=PhysicalObjectIdentityV1(
            catalog_source=source,
            database="bank-cluster",
            schema="pilot",
            table=table,
            object_kind="table",
        ),
        partition_columns=("business_dt",),
        business_time_column="business_dt",
    )


def _plan(
    *,
    left_selector: PartitionSelector | None = None,
    right_selector: PartitionSelector | None = None,
) -> RelationshipObservationPlanV2:
    left = _binding("ftr", "transactions")
    right = _binding("cib", "customer_history")
    pairs = (
        RelationshipColumnPairV2("cif_id", "cust_num"),
        RelationshipColumnPairV2("business_dt", "business_dt"),
    )
    selector = PartitionSelector("business_dt", ("2026-07-30",))
    scope = RelationshipObservationScopeV1(
        scope_id="hive-pilot",
        left_binding_revision_id=left.binding_revision_id,
        right_binding_revision_id=right.binding_revision_id,
        left_partitions=left_selector if left_selector is not None else selector,
        right_partitions=right_selector if right_selector is not None else selector,
        normalization_ids=tuple(pair.normalization_id for pair in pairs),
        execution_principal="hive-profiler",
        method=RelationshipMethod.EXACT,
    )
    return RelationshipObservationPlanV2(
        left_binding=left,
        right_binding=right,
        column_pairs=pairs,
        scope=scope,
        policy=ProfilePolicyV1(exact_distinct=True),
        realization_revision_id="realization-hive-v1",
        left_source_snapshot_id="ftr-partition-2026-07-30",
        right_source_snapshot_id="cib-partition-2026-07-30",
        shortlist_position=1,
        shortlist_size=4,
    )


def test_hive_relationship_sql_is_tuple_aware_bounded_and_portable() -> None:
    sql = HiveDialect().render_relationship_probe(_plan())
    assert "`pilot`.`transactions`" in sql
    assert "`pilot`.`customer_history`" in sql
    assert sql.count("`business_dt` IN ('2026-07-30')") == 2
    assert "FILTER (" not in sql.upper()
    assert "GROUP BY `k0`, `k1`" in sql
    assert "JOIN right_groups" in sql
    assert "left_n * right_n" in sql


def test_each_partitioned_endpoint_requires_its_own_selector() -> None:
    plan = _plan()
    with pytest.raises(ObservationPlanError, match="partition"):
        replace(plan, scope=replace(plan.scope, left_partitions=None))


def test_relationship_probe_reports_the_engine_operation_as_exact() -> None:
    plan = _plan()
    assert HiveDialect(flavour="spark").effective_method(plan) == "exact"
    assert HiveDialect(flavour="hive").effective_method(plan) == "exact"


def test_hive_projection_returns_metrics_not_identifier_values() -> None:
    sql = HiveDialect().render_relationship_probe(_plan())
    final_select = sql.rsplit("\nSELECT ", 1)[1].split("\nFROM ", 1)[0]
    assert "`cif_id`" not in final_select
    assert "`cust_num`" not in final_select
    assert all(
        token in final_select
        for token in ("row_count", "distinct_count", "joined_rows", "max_right_matches")
    )

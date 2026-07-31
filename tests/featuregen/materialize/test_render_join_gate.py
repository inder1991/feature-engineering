from __future__ import annotations

from datetime import datetime

import pytest
from tests.featuregen.materialize import fake_spark
from tests.featuregen.materialize.test_cross_catalog_ir import (
    CRM_EFFECTIVE_FROM,
    CRM_EFFECTIVE_TO,
    CRM_TENANT,
    _inventory,
    _realization_current,
)
from tests.featuregen.materialize.test_expression_ir import INVENTORY

from featuregen.materialize.render.nodes_join_gate import render_join_precondition_node
from featuregen.overlay.upload.bridge_realization import (
    AsOfIntervalRequirementV1,
    FixedValueReferencePredicateV1,
)


def _step(*, predicates=()):
    current = _realization_current(
        _inventory(INVENTORY),
        composite=True,
        predicates=predicates,
    )
    from featuregen.materialize.joins import PhysicalIdentity, plan_cross_catalog_join

    plan = plan_cross_catalog_join(
        current,
        from_identity=PhysicalIdentity("hdfc", "banking", "transactions"),
        to_identity=PhysicalIdentity("crm", "crm_banking", "customer_master"),
    )
    assert not isinstance(plan, Exception)
    return plan.steps[0]


def test_duplicate_target_tuple_blocks_before_computation() -> None:
    node = render_join_precondition_node(
        _step(),
        target_dataset="raw_customer",
        validated_dataset="validated_customer",
    )
    execute = fake_spark.run_rendered(node.source, node.func_name)
    with pytest.raises(RuntimeError, match="JOIN_AMPLIFICATION"):
        execute(fake_spark.DataFrame([
            {"customer_id": "C1", "tenant_id": "A"},
            {"customer_id": "C1", "tenant_id": "A"},
        ]))


def test_gate_returns_the_same_predicate_scoped_rows_the_join_will_consume() -> None:
    predicates = (
        FixedValueReferencePredicateV1("tenant", CRM_TENANT, "tenant_id"),
        AsOfIntervalRequirementV1(
            "as-of",
            CRM_EFFECTIVE_FROM,
            CRM_EFFECTIVE_TO,
            "dimension_as_of",
        ),
    )
    node = render_join_precondition_node(
        _step(predicates=predicates),
        target_dataset="raw_customer",
        validated_dataset="validated_customer",
    )
    execute = fake_spark.run_rendered(node.source, node.func_name)
    rows = execute(
        fake_spark.DataFrame([
            {
                "customer_id": "C1",
                "tenant_id": "A",
                "effective_from": datetime(2026, 1, 1),
                "effective_to": None,
            },
            {
                "customer_id": "C1",
                "tenant_id": "B",
                "effective_from": datetime(2026, 1, 1),
                "effective_to": None,
            },
        ]),
        {"tenant_id": "A", "dimension_as_of": datetime(2026, 7, 27)},
    ).rows
    assert [(row["customer_id"], row["tenant_id"]) for row in rows] == [("C1", "A")]

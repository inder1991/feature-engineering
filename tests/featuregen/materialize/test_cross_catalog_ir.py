from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from tests.featuregen.materialize import fake_spark
from tests.featuregen.materialize.test_expression_ir import (
    TXN_AMT,
    TXN_CIF,
    _col,
    _expr,
    _govern_availability,
    _govern_logical_type,
    _watermark,
)
from tests.featuregen.materialize.test_group_plan import _contract
from tests.featuregen.materialize.test_render_nodes_compute import BEFORE, BUSINESS_DT, _windowed
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _binding,
    _endpoint,
    _realization,
)

from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import ExpressionExecutionIR, compile_expression
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    TableLayout,
    VerifiedUnpartitioned,
)
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.render.nodes_compute import render_projection_node
from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
from featuregen.overlay.upload.bridge_realization import (
    AsOfIntervalRequirementV1,
    BridgeRealizationCurrentV1,
    ColumnPairV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import CurrentBridgeRealizationV1

CRM_CUSTOMERS = "crm::public.customer_master"
CRM_CIF = f"{CRM_CUSTOMERS}.customer_id"
CRM_TENANT = f"{CRM_CUSTOMERS}.tenant_id"
CRM_EFFECTIVE_FROM = f"{CRM_CUSTOMERS}.effective_from"
CRM_EFFECTIVE_TO = f"{CRM_CUSTOMERS}.effective_to"
TXN_TENANT = "hdfc::public.transactions.tenant_id"


@pytest.fixture
def catalog(db):
    for column in ("txn_amt", "txn_dt", "cif_id"):
        _col(
            db,
            "transactions",
            column,
            data_type="numeric" if column == "txn_amt" else None,
        )
    _govern_availability(db, "transactions", "txn_dt")
    _govern_logical_type(db, TXN_AMT, "numeric")
    _watermark(db)
    return db


def _inventory(base: ClusterInventoryV1) -> ClusterInventoryV1:
    transaction = base.tables["banking.transactions"]
    return ClusterInventoryV1(
        environment_id=base.environment_id,
        tables={
            **base.tables,
            "banking.transactions": replace(
                transaction,
                columns=(*transaction.columns, ("tenant_id", "string")),
            ),
            "crm_banking.customer_master": TableLayout(
                schema="crm_banking",
                table="customer_master",
                partition_columns=None,
                partition_mapping=VerifiedUnpartitioned(),
                columns=(
                    ("customer_id", "string"),
                    ("tenant_id", "string"),
                    ("effective_from", "timestamp"),
                    ("effective_to", "timestamp"),
                ),
                location="hdfs://nn/warehouse/crm_banking.db/customer_master",
                rewritten_in_place=False,
            ),
        },
        logical_schema_map={
            **base.logical_schema_map,
            CRM_CUSTOMERS: "crm_banking",
        },
        engine_versions=base.engine_versions,
        captured_at=base.captured_at,
    )


def _realization_current(
    base_inventory: ClusterInventoryV1,
    *,
    composite: bool = False,
    predicates=(),
) -> CurrentBridgeRealizationV1:
    source_binding = _binding(
        "hdfc",
        "transactions",
        database=base_inventory.environment_id,
        schema="banking",
    )
    target_binding = _binding(
        "crm",
        "customer_master",
        database=base_inventory.environment_id,
        schema="crm_banking",
    )
    source = _endpoint(
        "hdfc",
        "transactions",
        ("cif_id", "tenant_id") if composite else ("cif_id",),
        binding=source_binding,
        binding_revision_id=source_binding.binding_revision_id,
    )
    target = _endpoint(
        "crm",
        "customer_master",
        ("customer_id", "tenant_id") if composite else ("customer_id",),
        binding=target_binding,
        binding_revision_id=target_binding.binding_revision_id,
    )
    revision = replace(
        _realization(
            source,
            target,
            pairs=(
                ColumnPairV1(TXN_CIF, CRM_CIF),
                *((ColumnPairV1(TXN_TENANT, CRM_TENANT),) if composite else ()),
            ),
            predicates=predicates,
        ),
        applicability_scope=RealizationApplicabilityScopeV1(
            scope_id="production-cross-catalog",
            execution_tier=ExecutionTier.PRODUCTION,
            purposes=("feature_generation",),
            environment=base_inventory.environment_id,
        ),
    )
    return CurrentBridgeRealizationV1(
        revision,
        BridgeRealizationCurrentV1(
            revision.realization_id,
            revision.realization_revision_id,
            SafetyStatus.DETERMINISTICALLY_VALIDATED,
            LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE,
            1,
        ),
        (),
    )


def _seed_crm_catalog(db) -> None:
    for column in ("customer_id", "tenant_id", "effective_from", "effective_to"):
        db.execute(
            "INSERT INTO graph_node "
            "(catalog_source, object_ref, kind, table_name, column_name, schema_name) "
            "VALUES ('crm',%s,'column','customer_master',%s,'crm_banking')",
            (f"public.customer_master.{column}", column),
        )


def test_cross_catalog_ir_carries_both_catalogs_and_exact_realization(
    catalog,
) -> None:
    _seed_crm_catalog(catalog)
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    inventory = _inventory(INVENTORY)
    realization = _realization_current(inventory)
    result = compile_expression(
        catalog,
        expr_path="body.expr",
        expr=_expr(),
        grain_keys=(CRM_CIF,),
        roles=("feature_engineer",),
        inventory=inventory,
        bridge_realizations=(realization,),
    )
    assert isinstance(result, ExpressionExecutionIR)
    assert {ref.catalog_source for ref in result.physical_read_set} == {"hdfc", "crm"}
    assert {TXN_CIF, CRM_CIF} <= {
        ref.logical_ref for ref in result.physical_read_set}
    assert len(result.input_requirements) == 2
    (step,) = result.join_plan.steps
    assert isinstance(step, CrossCatalogJoinStepV1)
    assert step.realization_revision_id == realization.revision.realization_revision_id


def test_cross_catalog_ir_without_executable_realization_fails_closed(catalog) -> None:
    _seed_crm_catalog(catalog)
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    result = compile_expression(
        catalog,
        expr_path="body.expr",
        expr=_expr(),
        grain_keys=(CRM_CIF,),
        roles=("feature_engineer",),
        inventory=_inventory(INVENTORY),
    )
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_cross_catalog_ir_renders_and_executes_the_exact_directional_join(catalog) -> None:
    _seed_crm_catalog(catalog)
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    inventory = _inventory(INVENTORY)
    result = compile_expression(
        catalog,
        expr_path="body.expr",
        expr=_expr(),
        grain_keys=(CRM_CIF,),
        roles=("feature_engineer",),
        inventory=inventory,
        bridge_realizations=(_realization_current(inventory),),
    )
    assert isinstance(result, ExpressionExecutionIR)
    node = render_projection_node(
        result,
        _contract(ordered_keys=(CRM_CIF,)),
        feature_column="total_debit_amount_30d",
        source_dataset="raw_hdfc_transactions",
        joined_datasets={"crm::crm_banking.customer_master": "raw_crm_customer_master"},
        projection_dataset="projected",
    )
    execute = fake_spark.run_rendered(node.source, node.func_name)
    rows = execute(
        fake_spark.DataFrame([
            _windowed(BEFORE, 10) | {"cif_id": "C1"},
            _windowed(BEFORE, 20) | {"cif_id": "C404"},
        ]),
        fake_spark.DataFrame([{"customer_id": "C1"}]),
        BUSINESS_DT,
    ).rows
    assert [(row["txn_amt"], row["customer_id"]) for row in rows] == [
        (10, "C1"),
        (20, None),
    ]
    assert result.join_plan.steps[0].realization_revision_id in node.source


def test_renderer_keeps_composite_mapping_and_closed_predicates_in_one_join(catalog) -> None:
    _seed_crm_catalog(catalog)
    _col(catalog, "transactions", "tenant_id")
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    inventory = _inventory(INVENTORY)
    predicates = (
        FixedValueReferencePredicateV1(
            "tenant-scope",
            CRM_TENANT,
            "tenant_id",
        ),
        AsOfIntervalRequirementV1(
            "customer-as-of",
            CRM_EFFECTIVE_FROM,
            CRM_EFFECTIVE_TO,
            "dimension_as_of",
        ),
    )
    realization = _realization_current(
        inventory,
        composite=True,
        predicates=predicates,
    )
    result = compile_expression(
        catalog,
        expr_path="body.expr",
        expr=_expr(),
        grain_keys=(CRM_CIF,),
        roles=("feature_engineer",),
        inventory=inventory,
        bridge_realizations=(realization,),
    )
    assert isinstance(result, ExpressionExecutionIR)
    node = render_projection_node(
        result,
        _contract(ordered_keys=(CRM_CIF,)),
        feature_column="total_debit_amount_30d",
        source_dataset="raw_hdfc_transactions",
        joined_datasets={"crm::crm_banking.customer_master": "raw_crm_customer_master"},
        projection_dataset="projected",
    )
    execute = fake_spark.run_rendered(node.source, node.func_name)
    as_of = datetime(2026, 7, 27)
    rows = execute(
        fake_spark.DataFrame([
            _windowed(BEFORE, 10) | {"cif_id": "C1", "tenant_id": "A"},
            _windowed(BEFORE, 20) | {"cif_id": "C1", "tenant_id": "B"},
        ]),
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
                "effective_from": datetime(2025, 1, 1),
                "effective_to": datetime(2026, 1, 1),
            },
        ]),
        {"tenant_id": "A", "dimension_as_of": as_of},
        BUSINESS_DT,
    ).rows
    assert [(row["txn_amt"], row["customer_id"]) for row in rows] == [
        (10, "C1"),
        (20, None),
    ]
    assert node.source.count("rows.join(") == 1
    assert "__join_1__key_1" in node.source
    assert "__join_1__key_2" in node.source


def test_projection_rechecks_observed_amplification_even_after_precondition(catalog) -> None:
    _seed_crm_catalog(catalog)
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    inventory = _inventory(INVENTORY)
    result = compile_expression(
        catalog,
        expr_path="body.expr",
        expr=_expr(),
        grain_keys=(CRM_CIF,),
        roles=("feature_engineer",),
        inventory=inventory,
        bridge_realizations=(_realization_current(inventory),),
    )
    assert isinstance(result, ExpressionExecutionIR)
    node = render_projection_node(
        result,
        _contract(ordered_keys=(CRM_CIF,)),
        feature_column="total_debit_amount_30d",
        source_dataset="raw_hdfc_transactions",
        joined_datasets={"crm::crm_banking.customer_master": "validated_crm_customer"},
        projection_dataset="projected",
    )
    # The duplicate key below now trips the PRE-join uniqueness gate, so the post-join recheck
    # is pinned on the emitted source: it is NOT redundant — the hop frame is lazy and scanned
    # twice (once by the gate, once by the join), so the recheck is what catches a target table
    # that CHANGED between the two scans, and deleting it must fail this test.
    assert "rows_before_bridge_1 = rows.count()" in node.source
    assert "if rows.count() > rows_before_bridge_1:" in node.source
    execute = fake_spark.run_rendered(node.source, node.func_name)
    with pytest.raises(RuntimeError, match="JOIN_AMPLIFICATION"):
        execute(
            fake_spark.DataFrame([_windowed(BEFORE, 10) | {"cif_id": "C1"}]),
            fake_spark.DataFrame([
                {"customer_id": "C1"},
                {"customer_id": "C1"},
            ]),
            BUSINESS_DT,
        )

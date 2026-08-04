from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

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
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _binding,
    _endpoint,
    _realization,
)

from featuregen.data_agent.physical import record_binding_revision
from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.data_agent.store import record_relationship_observation
from featuregen.events.registry import event_registry
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import ExpressionExecutionIR, compile_expression
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    TableLayout,
    VerifiedUnpartitioned,
)
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.render.nodes_compute import render_projection_node
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    LinkReviewStatus,
    read_overlay_identifier_link_state,
)
from featuregen.overlay.upload.bridge_realization import (
    AsOfIntervalRequirementV1,
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    ColumnPairV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    CurrentBridgeRealizationV1,
    bridge_dependency_snapshot_id,
    executable_bridge_realizations,
    record_realization_revision,
)
from featuregen.projections.runner import run_projection

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


#: The one purpose `materialize/ir.py` reads bridges for, and the scope every seed below is admitted
#: under. Stated once so a drift between the seed and `_BRIDGE_PURPOSE` is one edit, not two.
BRIDGE_PURPOSE = "feature_generation"


def seed_executable_bridge_realization(
    db,
    inventory: ClusterInventoryV1,
    *,
    composite: bool = False,
    predicates=(),
) -> BridgeJoinRealizationRevisionV1:
    """Make :func:`_realization_current`'s realization DURABLE — through the real writers.

    Every cross-catalog test in the tree hands ``compile_ir`` a ``CurrentBridgeRealizationV1`` built
    in Python, so nothing has ever exercised the read ``chain.py`` actually performs: ``compile_ir``
    is called there with no ``bridge_realizations`` argument at all, which means an HTTP- or
    lane-driven bridged run loads its joins from the DATABASE through
    ``executable_bridge_realizations``. That reader is not a SELECT — it is
    ``revalidate_bridge_realization`` over every current pointer, and it fails closed on fifteen
    separate facts. A fixture that INSERTed rows would satisfy none of them.

    So this writes the same shape production writes, in the same order, through the same functions
    (nothing under ``overlay/upload/`` is modified — it is only called):

    1. ``record_binding_revision`` for both endpoints, because revalidation re-reads the stored
       binding revision and refuses an endpoint whose physical address nothing recorded;
    2. ``govern_bridge_fact`` — the identifier link's own overlay event stream, which is where
       ``LinkAvailability`` and the ``overlay_head_event_id`` the bridge dependency must name come
       from. A ledger row alone is a shape the platform cannot produce;
    3. ``record_realization_revision`` for the PRE-ADMISSION revision at ``UNASSESSED`` safety —
       the revision the exact observation names. It has to exist first and it has to be a different
       revision: adding the observation to ``evidence_refs`` changes the content-addressed
       ``realization_revision_id``, so an observation naming the admitted revision would be a cycle
       (``bridge_store._current_exact_evidence_ids`` says exactly this);
    4. ``record_relationship_observation`` — the complete, full-coverage, non-conflicting exact
       profile that is the only thing that can discharge ``exact_relationship_evidence_missing``;
    5. ``record_realization_revision`` again, CAS-advancing the current pointer to the admitted
       revision at ``DETERMINISTICALLY_VALIDATED`` with the observation as evidence and its
       dependency in the snapshot.

    ``dependency_snapshot_id`` is recomputed at both steps rather than carried: revalidation
    re-derives it from the dependency rows and refuses a mismatch, so a hand-written literal would
    make the seed unexecutable in a way no reader would explain.

    Returns the ADMITTED revision — the one a correct ``executable_bridge_realizations`` returns.
    Its ``realization_revision_id`` is deliberately NOT the in-memory fixture's: the evidence and
    the snapshot differ, so a test that finds this id in a compiled IR has proved the value came
    from the database and could not have been the injected object.
    """
    base = _realization_current(
        inventory, composite=composite, predicates=predicates).revision
    source, target = base.from_endpoint, base.to_endpoint
    assert source.physical_binding is not None and target.physical_binding is not None

    # The overlay event schemas are registered per-test by `tests/featuregen/overlay/conftest.py`,
    # which does not apply here — and the root harness resets the registry around every test, so
    # this cannot be hoisted to import time. `register_schema` overwrites, so it is idempotent.
    register_overlay_event_types(event_registry())

    record_binding_revision(db, source.physical_binding)
    record_binding_revision(db, target.physical_binding)
    govern_bridge_fact(
        db,
        base.bridge_fact_key,
        entity="customer",
        left_source="hdfc",
        left_ref="public.transactions.cif_id",
        right_source="crm",
        right_ref="public.customer_master.customer_id",
        status="DRAFT",
    )
    # Those are REAL governance events, so they advance the global event head — and
    # `read_operational_value` fails CLOSED on a LAGGED overlay projection (GATE 3,
    # `operational_facts.py:441`). A seed that only appended would silently degrade every governed
    # catalog read in the same test: the authoring lane would close NEEDS_REVIEW and the chain would
    # stop at RESOLVE, with nothing pointing at the bridge as the cause. `seed_verified_bridge`
    # advances the checkpoint for exactly this reason.
    while run_projection(db, OverlayProjection()) >= 500:
        pass
    degraded = db.execute(
        "SELECT aggregate, aggregate_id, reason FROM projection_degraded "
        "WHERE projection_name = 'overlay' ORDER BY poison_seq").fetchall()
    assert not degraded, f"the bridge seed degraded the overlay projection: {degraded!r}"

    link_head = read_overlay_identifier_link_state(
        db, base.bridge_fact_key).overlay_head_event_id
    assert link_head is not None

    dependencies = (
        BridgeDependencyRefV1("bridge_fact", base.bridge_fact_key, link_head),
        BridgeDependencyRefV1(
            "physical_binding",
            source.physical_binding.binding_id,
            source.physical_binding.binding_revision_id,
        ),
        BridgeDependencyRefV1(
            "physical_binding",
            target.physical_binding.binding_id,
            target.physical_binding.binding_revision_id,
        ),
    )
    candidate = replace(
        base,
        cardinality_basis=CardinalityBasis.EXACT_PROFILE,
        evidence_refs=(),
        dependency_snapshot_id=bridge_dependency_snapshot_id(dependencies),
    )
    record_realization_revision(
        db,
        candidate,
        BridgeRealizationCurrentV1(
            candidate.realization_id,
            candidate.realization_revision_id,
            SafetyStatus.UNASSESSED,
            LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE,
            1,
        ),
        dependencies=dependencies,
    )

    observation = _exact_observation(candidate)
    assert record_relationship_observation(
        db, observation, expected_pointer_version=0).became_current

    admitted_dependencies = (
        *dependencies,
        BridgeDependencyRefV1(
            "relationship_observation",
            observation.observation_revision_id,
            observation.plan_hash,
        ),
    )
    admitted = replace(
        candidate,
        evidence_refs=(
            EvidenceRefV1(
                observation.observation_revision_id,
                EvidenceKind.EXACT_PROFILE,
                observation.producer,
                content_hash=observation.plan_hash,
                observed_at=observation.observed_at,
            ),
        ),
        dependency_snapshot_id=bridge_dependency_snapshot_id(admitted_dependencies),
    )
    record_realization_revision(
        db,
        admitted,
        BridgeRealizationCurrentV1(
            admitted.realization_id,
            admitted.realization_revision_id,
            SafetyStatus.DETERMINISTICALLY_VALIDATED,
            LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE,
            2,
        ),
        dependencies=admitted_dependencies,
        expected_pointer_version=1,
    )
    return admitted


def _exact_observation(
    revision: BridgeJoinRealizationRevisionV1,
) -> RelationshipObservationV2:
    """The exact profile that structurally attests ``revision``.

    ``bridge_store._current_exact_evidence_ids`` re-checks every execution-bearing field of this
    object against the ADMITTED revision, so none of it is decoration: the ordered column tuples
    are the realization's own ``column_pairs`` (bare column names, as the observation records them),
    the binding revisions are the endpoints', the predicate ids are the closed predicates in
    declaration order, and the right-hand endpoint must be observed unique with no nulls — that
    zero fan-out is the whole claim a directional realization rests on.
    """
    columns = tuple(
        (pair.from_logical_column_ref.rsplit(".", 1)[-1],
         pair.to_logical_column_ref.rsplit(".", 1)[-1])
        for pair in revision.column_pairs
    )
    from_binding = revision.from_endpoint.physical_binding
    to_binding = revision.to_endpoint.physical_binding
    assert from_binding is not None and to_binding is not None

    def _endpoint_tuple(binding, endpoint, names) -> EndpointTupleObservationV2:
        return EndpointTupleObservationV2(
            binding.identity.table_id,
            endpoint.binding_revision_id or "",
            binding.content_hash,
            names,
            10,   # row_count
            10,   # non_null_row_count — null_row_count is the difference, and must be 0
            10,   # distinct_tuple_count — equal to row_count, so the tuple is observed unique
            0,    # duplicate_tuple_count
            0,    # duplicate_row_count
            1,    # max_rows_per_tuple
        )

    return RelationshipObservationV2(
        realization_revision_id=revision.realization_revision_id,
        plan_hash="cross-catalog-exact-plan-hash",
        scope_id=revision.applicability_scope.scope_id,
        left=_endpoint_tuple(
            from_binding, revision.from_endpoint, tuple(pair[0] for pair in columns)),
        right=_endpoint_tuple(
            to_binding, revision.to_endpoint, tuple(pair[1] for pair in columns)),
        matched_left_distinct=10,
        unmatched_left_distinct=0,
        matched_right_distinct=10,
        unmatched_right_distinct=0,
        left_orphan_rows=0,
        right_orphan_rows=0,
        joined_row_count=10,
        max_right_matches_per_left_row=1,
        max_left_matches_per_right_row=1,
        normalization_ids=("identity_v1",),
        predicate_ids=tuple(
            predicate.predicate_id for predicate in revision.predicates
            if isinstance(
                predicate,
                FixedValueReferencePredicateV1 | AsOfIntervalRequirementV1)),
        left_source_snapshot_id="hdfc-transactions-snapshot",
        right_source_snapshot_id="crm-customer-master-snapshot",
        snapshot_or_as_of="2026-07-27",
        execution_principal="profile-service",
        method="exact",
        row_coverage=RowCoverage.FULL,
        complete=True,
        observed_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
    )


def _seed_crm_catalog(db) -> None:
    for column in ("customer_id", "tenant_id", "effective_from", "effective_to"):
        db.execute(
            "INSERT INTO graph_node "
            "(catalog_source, object_ref, kind, table_name, column_name, schema_name) "
            "VALUES ('crm',%s,'column','customer_master',%s,'crm_banking')",
            (f"public.customer_master.{column}", column),
        )


def test_the_durable_seed_is_what_the_PRODUCTION_reader_returns(catalog) -> None:
    """The enabling proof for the bridged chain path (DEFERRED-WORK A.36).

    ``executable_bridge_realizations`` is the reader ``compile_ir`` uses when no realization is
    injected, and it re-derives every load-bearing fact rather than trusting the row. Asserting it
    here — not merely that ``load_current_bridge_realizations`` finds something — is what makes the
    seed a fixture the chain can genuinely consume: a seed that stored an unrevalidatable
    realization would leave the chain test failing with ``0 current executable directional
    realizations`` and no clue which of the fifteen checks it missed.
    """
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    inventory = _inventory(INVENTORY)
    admitted = seed_executable_bridge_realization(catalog, inventory)

    executable = executable_bridge_realizations(
        catalog, purpose=BRIDGE_PURPOSE, environment=inventory.environment_id)

    assert [item.revision for item in executable] == [admitted]
    assert executable[0].current.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    # and it is NOT the in-memory fixture: the evidence and dependency snapshot differ, so an IR
    # carrying this revision id can only have come from the database.
    assert admitted.realization_revision_id != \
        _realization_current(inventory).revision.realization_revision_id


def test_a_COMPOSITE_PREDICATED_realization_is_durable_and_executable_too(catalog) -> None:
    """The seed's two parameters, exercised — and the sharpest case for the evidence check.

    ``_current_exact_evidence_ids`` matches the observation's ORDERED column tuples and its
    predicate ids against the admitted revision, so a composite, predicated realization is where a
    seed that hard-coded a single ``customer_id`` pair or an empty predicate list would be silently
    dropped by ``executable_bridge_realizations`` — leaving a caller with "no executable
    realization" and nothing pointing at the fixture.
    """
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    inventory = _inventory(INVENTORY)
    admitted = seed_executable_bridge_realization(catalog, inventory, composite=True, predicates=(
        FixedValueReferencePredicateV1("tenant-scope", CRM_TENANT, "tenant_id"),
        AsOfIntervalRequirementV1("customer-as-of", CRM_EFFECTIVE_FROM, CRM_EFFECTIVE_TO,
                                  "dimension_as_of")))

    executable = executable_bridge_realizations(
        catalog, purpose=BRIDGE_PURPOSE, environment=inventory.environment_id)

    assert [item.revision for item in executable] == [admitted]
    assert len(admitted.column_pairs) == 2
    assert len(admitted.predicates) == 2


def test_the_durable_seed_is_INVISIBLE_to_another_environment(catalog) -> None:
    """Applicability is scoped, and the seed must not be a global switch: the same reader asked for
    a different environment returns nothing, which is what keeps a bridged chain test from passing
    on a realization approved for somewhere else."""
    from tests.featuregen.materialize.test_expression_ir import INVENTORY

    seed_executable_bridge_realization(catalog, _inventory(INVENTORY))

    assert executable_bridge_realizations(
        catalog, purpose=BRIDGE_PURPOSE, environment="some-other-cluster") == ()


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

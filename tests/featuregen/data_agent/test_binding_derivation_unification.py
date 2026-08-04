"""Release C Task 11, scope 0 — ONE derivation of a derived binding's identity.

THE DEFECT (codegen adversarial review 2026-07-31, "Two binding paths fork `physical_id`"; recorded
as the open reconciliation in `binding_store`'s docstring until now): the two resolvers derived the
same table's ADDRESS two ways.

* `physical.resolve_dataset_binding` -> `database = ClusterInventoryV1.environment_id`, stream
  `identifier-endpoint:<env>:<ref>`;
* `binding_store.resolve_table` -> `database = data_source_connection.database_name`, stream
  `derived-<catalog>-<table>`.

Both feed `PhysicalObjectIdentityV1.table_id` and `binding_revision_id`, and the relationship
observation store keys its CURRENT pointer on the side-specific `binding_revision_id`
(`store.current_scope_key`). So an observation recorded through one path was structurally invisible
to a reader holding the other — which is Task 11's own seam, because a crosswalk leg resolved
through the selection path has to line up with a bridge realization resolved through the inventory
path.

THE PROBE below is the review's: resolve one table both ways and demand ONE `physical_id`, ONE
`binding_revision_id`, ONE revision row — and an observation written under either path readable
under the other.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.materialize.fixtures import ENGINE_VERSIONS

from featuregen.data_agent.binding_store import (
    declare_catalog_engine,
    record_connection,
    resolve_table,
    select_table_binding,
)
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.data_agent.physical import (
    UNKNOWN_DATABASE,
    UnknownSchema,
    address_database,
    derived_binding_id,
    resolve_dataset_binding,
)
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    TableLayout,
    VerifiedUnpartitioned,
)

NOW = datetime(2026, 8, 4, 9, tzinfo=UTC)


def _connection(**over) -> DataSourceConnectionV1:
    kw = dict(connection_id="route-1", environment_id="dev", kind="hive",
              host="hiveserver2.internal", port=10000, auth_mechanism="kerberos",
              secret_ref="vault://featuregen/hive", execution_principal="svc_ro",
              allowed_schemas=frozenset({"dpl_eib"}), active=True)
    kw.update(over)
    return DataSourceConnectionV1(**kw)


def _layout(schema: str = "dpl_eib", table: str = "tran_repos") -> TableLayout:
    return TableLayout(
        schema=schema, table=table, partition_columns=None,
        partition_mapping=VerifiedUnpartitioned(),
        columns=(("cif_id", "string"),),
        location=f"hdfs://warehouse/{schema}.db/{table}", rewritten_in_place=False)


def _inventory(*, environment: str = "hadoop-pilot") -> ClusterInventoryV1:
    return ClusterInventoryV1(
        environment_id=environment,
        tables={"dpl_eib.tran_repos": _layout()},
        logical_schema_map={},
        engine_versions=ENGINE_VERSIONS,
        captured_at="2026-08-04T09:00:00Z")


@pytest.fixture
def routed(db):
    """One catalog, one declared engine, one routed connection whose row names the instance."""
    record_connection(db, _connection(), tier="edp", database_name="edp_cluster")
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  schema_name) VALUES ('ftr','public.tran_repos.cif_id','column','tran_repos','cif_id',"
        "  'dpl_eib') ON CONFLICT (catalog_source, object_ref) DO NOTHING")
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    return db


def _seed_observation(conn, *, left_binding):
    """One persisted realization over ``left_binding`` plus a complete exact observation of it."""
    from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
        _endpoint,
        _executable_pair,
        _realization,
    )

    from featuregen.data_agent.physical import record_binding_revision
    from featuregen.data_agent.relationship_observation import (
        EndpointTupleObservationV2,
        RelationshipObservationV2,
        RowCoverage,
    )
    from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
    from featuregen.overlay.upload.bridge_realization import (
        BridgeRealizationCurrentV1,
        RealizationLifecycle,
        SafetyStatus,
    )
    from featuregen.overlay.upload.bridge_store import (
        BridgeDependencyRefV1,
        record_realization_revision,
    )

    left = _endpoint("ftr", "tran_repos", ("cif_id",), binding=left_binding,
                     binding_revision_id=left_binding.binding_revision_id)
    _unused, right = _executable_pair()
    revision = _realization(left, right)
    record_binding_revision(conn, left_binding)
    assert right.physical_binding is not None
    record_binding_revision(conn, right.physical_binding)
    record_realization_revision(
        conn, revision,
        BridgeRealizationCurrentV1(
            revision.realization_id, revision.realization_revision_id,
            SafetyStatus.UNASSESSED, LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE, 1),
        dependencies=(BridgeDependencyRefV1(
            "bridge_fact", revision.bridge_fact_key, "bridge-head-1"),))

    def endpoint(binding, column):
        return EndpointTupleObservationV2(
            binding.identity.table_id, binding.binding_revision_id, binding.content_hash,
            (column,), 3, 3, 3, 0, 0, 1)

    observation = RelationshipObservationV2(
        realization_revision_id=revision.realization_revision_id,
        plan_hash="plan-hash-fork-probe",
        scope_id=revision.applicability_scope.scope_id,
        left=endpoint(left_binding, "cif_id"),
        right=endpoint(right.physical_binding, "customer_id"),
        matched_left_distinct=3, unmatched_left_distinct=0,
        matched_right_distinct=3, unmatched_right_distinct=0,
        left_orphan_rows=0, right_orphan_rows=0, joined_row_count=3,
        max_right_matches_per_left_row=1, max_left_matches_per_right_row=1,
        normalization_ids=("identity_v1",), predicate_ids=(),
        left_source_snapshot_id="left-snapshot-1", right_source_snapshot_id="right-snapshot-1",
        snapshot_or_as_of="2026-08-04", execution_principal="probe",
        method="exact", row_coverage=RowCoverage.FULL, complete=True, observed_at=NOW)
    return revision, observation


# ── the fork probe ──────────────────────────────────────────────────────────────────────────────

def test_the_two_resolvers_yield_ONE_physical_id_and_ONE_revision(routed):
    """THE probe. Before this task the same table came back as
    `ftr::edp_cluster::dpl_eib::tran_repos` from one resolver and
    `ftr::hadoop-pilot::dpl_eib::tran_repos` from the other, under two different binding streams."""
    routed_binding, _connection = resolve_table(routed, catalog_source="ftr", table="tran_repos")
    inventory_binding = resolve_dataset_binding(
        routed, _inventory(), logical_table_ref="ftr::public.tran_repos",
        connection_id="route-1")

    assert inventory_binding.identity.table_id == routed_binding.identity.table_id
    assert inventory_binding.identity.table_id == "ftr::edp_cluster::dpl_eib::tran_repos"
    assert inventory_binding.binding_id == routed_binding.binding_id == "derived-ftr-tran_repos"
    assert inventory_binding.catalog_logical_ref == routed_binding.catalog_logical_ref
    assert inventory_binding.binding_revision_id == routed_binding.binding_revision_id

    # And persisting through EITHER writer leaves exactly one revision row.
    select_table_binding(routed, catalog_source="ftr", table="tran_repos", recorded_by="probe")
    from featuregen.data_agent.physical import record_binding_revision

    record_binding_revision(routed, inventory_binding, recorded_by="probe")
    assert routed.execute(
        "SELECT count(*) FROM physical_dataset_binding_revision").fetchone()[0] == 1


def test_an_observation_written_through_either_path_is_visible_through_the_other(routed):
    """The consequence the fork had, stated as data: the observation store's current pointer is
    keyed on the side-specific binding revision, so two addresses meant two invisible histories."""
    from featuregen.data_agent.store import (
        current_relationship_observation,
        record_relationship_observation,
    )

    _binding, _conn, selection_revision = select_table_binding(
        routed, catalog_source="ftr", table="tran_repos", recorded_by="probe")
    inventory_binding = resolve_dataset_binding(
        routed, _inventory(), logical_table_ref="ftr::public.tran_repos",
        connection_id="route-1")
    assert inventory_binding.binding_revision_id == selection_revision

    # The observation is written holding the INVENTORY path's binding and read back through the
    # scope key a reader holding the SELECTION path's revision computes. Before the unification the
    # two revision ids differed, so the two scope keys differed and this returned None.
    revision, observation = _seed_observation(routed, left_binding=inventory_binding)
    outcome = record_relationship_observation(routed, observation)
    assert outcome.became_current is True

    stored = current_relationship_observation(routed, observation.current_scope_key)
    assert stored is not None
    assert stored.left.binding_revision_id == selection_revision
    assert stored.realization_revision_id == revision.realization_revision_id


# ── the derivation itself ───────────────────────────────────────────────────────────────────────

def test_the_connection_declaration_wins_over_the_callers_fallback(routed):
    assert address_database(routed, connection_id="route-1", fallback="hadoop-pilot") == (
        "edp_cluster")


def test_the_fallback_is_used_only_where_the_registry_declares_nothing(db):
    """This is what keeps the inventory path answerable on a deployment with no connection row —
    and what makes the change re-address nothing that was already stored."""
    assert address_database(db, connection_id="hive-pilot", fallback="hadoop-pilot") == (
        "hadoop-pilot")
    record_connection(db, _connection(connection_id="blank-db"), tier="edp", database_name="")
    assert address_database(db, connection_id="blank-db", fallback="hadoop-pilot") == (
        "hadoop-pilot")


def test_the_connection_id_is_the_last_resort_and_a_blank_address_refuses(db):
    assert address_database(db, connection_id="route-9") == "route-9"
    with pytest.raises(UnknownSchema) as raised:
        address_database(db, connection_id="   ")
    assert raised.value.code == UNKNOWN_DATABASE


def test_the_derived_stream_name_is_normalized_and_schema_free():
    """(catalog, table) is the grammar a flat logical ref speaks; ambiguity inside one catalog is
    refused at the seams that persist rather than papered over with a wider key."""
    assert derived_binding_id(catalog_source="FTR", table="Tran_Repos") == "derived-ftr-tran_repos"


def test_an_explicit_binding_still_wins_and_keeps_its_own_stream_name(routed):
    """An operator's per-table declaration is the exception mechanism, not a third derivation."""
    from featuregen.data_agent.binding_store import record_binding
    from featuregen.data_agent.physical import (
        PhysicalDatasetBindingV1,
        PhysicalObjectIdentityV1,
    )

    record_binding(routed, PhysicalDatasetBindingV1(
        binding_id="b-operator-declared", catalog_logical_ref="ftr::dpl_eib.tran_repos",
        connection_id="route-1",
        identity=PhysicalObjectIdentityV1(
            catalog_source="ftr", database="snapshot_2026_06", schema="dpl_eib",
            table="tran_repos", object_kind="table")))
    binding, _connection = resolve_table(routed, catalog_source="ftr", table="tran_repos")
    assert binding.binding_id == "b-operator-declared"
    assert binding.identity.database == "snapshot_2026_06"

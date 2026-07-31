from __future__ import annotations

from dataclasses import replace

import pytest
from tests.featuregen.materialize.fixtures import ENGINE_VERSIONS
from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    TableLayout,
    VerifiedUnpartitioned,
)
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import record_field_decision
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    record_field_evidence,
)
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    ConceptAuthority,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    TypeBasis,
    resolve_and_record_endpoint_binding,
)
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import BridgeProposalError, propose_bridge
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
from featuregen.projections.runner import run_projection


def _endpoint(source: str = "core") -> IdentifierEndpointV1:
    table = "customer_master"
    column = "customer_id"
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=(
            IdentifierColumnMemberV1(
                logical_column_ref=normalize_ref(source, "public", table, column),
                data_type_family="integer",
                type_basis=TypeBasis.ATTESTED,
            ),
        ),
        entity_id="customer",
        concept="customer_id",
        concept_authority=ConceptAuthority.UNKNOWN,
    )


def _layout(schema: str = "banking", table: str = "customer_master") -> TableLayout:
    return TableLayout(
        schema=schema,
        table=table,
        partition_columns=None,
        partition_mapping=VerifiedUnpartitioned(),
        columns=(("customer_id", "bigint"),),
        location=f"hdfs://warehouse/{schema}.db/{table}",
        rewritten_in_place=False,
    )


def _inventory(*layouts: TableLayout, environment: str = "hadoop-pilot",
               schema_map: dict[str, str] | None = None) -> ClusterInventoryV1:
    return ClusterInventoryV1(
        environment_id=environment,
        tables={f"{layout.schema}.{layout.table}": layout for layout in layouts},
        logical_schema_map=schema_map or {},
        engine_versions=ENGINE_VERSIONS,
        captured_at="2026-07-30T10:00:00Z",
    )


def _graph_column(db, *, source: str = "core", schema: str | None = "banking",
                  table: str = "customer_master", column: str = "customer_id") -> None:
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
        " schema_name, data_type, concept) VALUES (%s,%s,'column',%s,%s,%s,'integer','customer_id')",
        (source, f"public.{table}.{column}", table, column, schema),
    )


def test_flat_logical_endpoint_resolves_to_a_revision_pinned_physical_binding(db):
    _graph_column(db)
    endpoint = _endpoint()
    bound = resolve_and_record_endpoint_binding(
        db,
        _inventory(_layout()),
        endpoint,
        connection_id="hive-pilot",
        purposes=("relationship_probe",),
        recorded_by="test",
    )

    assert isinstance(bound, IdentifierEndpointV1)
    assert bound.logical_table_ref == "core::public.customer_master"
    assert bound.members[0].logical_column_ref == "core::public.customer_master.customer_id"
    assert bound.physical_table_id == "core::hadoop-pilot::banking::customer_master"
    assert bound.members[0].physical_column_id == (
        "core::hadoop-pilot::banking::customer_master::customer_id")
    assert bound.executable is True
    assert bound.binding_revision_id == bound.physical_binding.binding_revision_id
    assert (
        bound.execution_identity_payload()["binding"]["binding_content_hash"]
        == bound.physical_binding.content_hash
    )
    assert db.execute(
        "SELECT count(*) FROM physical_dataset_binding_revision "
        "WHERE binding_revision_id = %s",
        (bound.binding_revision_id,),
    ).fetchone()[0] == 1


def test_public_is_never_used_as_the_physical_schema_when_resolution_is_missing(db):
    _graph_column(db, schema=None)
    result = resolve_and_record_endpoint_binding(
        db,
        _inventory(),
        _endpoint(),
        connection_id="hive-pilot",
        purposes=("relationship_probe",),
    )

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED
    assert db.execute(
        "SELECT count(*) FROM physical_dataset_binding_revision").fetchone()[0] == 0


def test_ambiguous_physical_schema_refuses_without_changing_logical_identity(db):
    _graph_column(db, schema="banking")
    # A second column on the same flat table attests a contradictory physical schema.
    _graph_column(db, schema="legacy", column="legacy_id")
    result = resolve_and_record_endpoint_binding(
        db,
        _inventory(_layout("banking"), _layout("legacy")),
        _endpoint(),
        connection_id="hive-pilot",
        purposes=("relationship_probe",),
    )

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AMBIGUOUS_TABLE_NAME
    assert _endpoint().logical_table_ref == "core::public.customer_master"


def test_schema_qualified_replacement_bridge_endpoint_is_rejected():
    endpoint = _endpoint()
    with pytest.raises(BridgeContractError, match="flat public namespace"):
        replace(endpoint, logical_table_ref="core::banking.customer_master")


def test_physical_binding_must_attest_every_tuple_member(db):
    _graph_column(db)
    result = resolve_and_record_endpoint_binding(
        db,
        _inventory(replace(_layout(), columns=(("some_other_column", "bigint"),))),
        _endpoint(),
        connection_id="hive-pilot",
        purposes=("relationship_probe",),
    )

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF


def test_proposal_rechecks_endpoint_existence_instead_of_trusting_the_candidate(db):
    _two_catalog_customer(db)
    candidate = derive_bridge_candidates(db)[0]
    db.execute(
        "DELETE FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (
            candidate.right_ref.catalog_source,
            f"public.{candidate.right_ref.table}.{candidate.right_ref.column}",
        ),
    )

    with pytest.raises(BridgeProposalError, match="does not exist"):
        propose_bridge(db, candidate, actor=_ENRICH_ACTOR)


def test_bridge_fact_and_drift_dependencies_keep_the_flat_logical_namespace(db):
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    candidate = derive_bridge_candidates(db)[0]
    key = propose_bridge(db, candidate, actor=_ENRICH_ACTOR)
    while run_projection(db, OverlayProjection()) >= 500:
        pass

    dependencies = set(db.execute(
        "SELECT catalog_source, ref_object FROM overlay_fact_dependency "
        "WHERE fact_key = %s",
        (key,),
    ).fetchall())
    assert dependencies == {
        ("core", "public.customer_master"),
        ("core", "public.customer_master.customer_id"),
        ("crm", "public.customers"),
        ("crm", "public.customers.customer_id"),
    }
    assert all("banking" not in ref for _source, ref in dependencies)


def test_proposal_rejects_a_non_column_endpoint(db):
    _two_catalog_customer(db)
    candidate = derive_bridge_candidates(db)[0]
    invalid = replace(
        candidate,
        right_ref=replace(candidate.right_ref, object_kind="table", column=None),
    )

    with pytest.raises(BridgeProposalError, match="identifier column"):
        propose_bridge(db, invalid, actor=_ENRICH_ACTOR)


def _mark_llm_concept_authority(db, source: str, table: str, column: str) -> None:
    logical_ref = normalize_ref(source, "public", table, column)
    evidence_id = record_field_evidence(
        db,
        logical_ref=logical_ref,
        field_name="concept",
        proposed_value="customer_id",
        producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED,
        producer_ref="llm-test-run",
        source_snapshot_id="snapshot-1",
        input_hash=field_input_hash(
            logical_ref=logical_ref,
            field_name="concept",
            material={"table": table, "column": column},
        ),
    )
    decision_id = record_field_decision(
        db,
        logical_ref=logical_ref,
        field_name="concept",
        event_type="resolved",
        selected_evidence_ids=(evidence_id,),
        evidence_set_hash=canonical_hash([evidence_id]),
        display_value_hash=canonical_hash("customer_id"),
        load_bearing_value_hash=None,
        conflict_status="recommendation_only",
        reason_codes=("llm_recommendation",),
        field_policy_version="test",
        resolver_version="test",
        actor_ref=None,
        supersedes_event_id=None,
    )
    db.execute(
        "UPDATE graph_node SET concept_decision_id = %s "
        "WHERE catalog_source = %s AND object_ref = %s",
        (decision_id, source, f"public.{table}.{column}"),
    )


def test_llm_identifier_classification_can_propose_without_human_review(db):
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    _mark_llm_concept_authority(db, "core", "customer_master", "customer_id")
    _mark_llm_concept_authority(db, "crm", "customers", "customer_id")

    candidate = derive_bridge_candidates(db)[0]
    assert candidate.left_concept_authority == "llm"
    assert candidate.right_concept_authority == "llm"
    key = propose_bridge(db, candidate, actor=_ENRICH_ACTOR)

    assert key
    evidence = db.execute(
        "SELECT evidence_json FROM entity_bridge_candidate_evidence "
        "WHERE candidate_id = %s",
        (candidate.candidate_id,),
    ).fetchone()[0]
    assert evidence["left_concept_authority"] == "llm"
    assert evidence["right_concept_authority"] == "llm"


def test_candidate_cannot_spoof_a_stronger_concept_authority(db):
    _two_catalog_customer(db)
    candidate = derive_bridge_candidates(db)[0]
    spoofed = replace(candidate, left_concept_authority="human")

    with pytest.raises(BridgeProposalError, match="concept authority is stale"):
        propose_bridge(db, spoofed, actor=_ENRICH_ACTOR)

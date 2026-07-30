from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen._helpers import mint_test_service_identity
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
)
from featuregen.overlay import facts, store
from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    ConceptAuthority,
    EvidenceKind,
    EvidenceRefV1,
    FoldedLinkStatus,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    KeyMemberRole,
    LinkAvailability,
    LinkReviewStatus,
    LinkUnavailableReason,
    NamespaceVerdict,
    PopulationRelation,
    TupleKeyRole,
    TypeBasis,
    read_identifier_link_availability,
)
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
    eligible_for_production,
    eligible_for_sandbox,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _binding(
    source: str,
    table: str,
    *,
    database: str = "hive-primary",
    schema: str = "banking",
    binding_id: str | None = None,
) -> PhysicalDatasetBindingV1:
    return PhysicalDatasetBindingV1(
        binding_id=binding_id or f"binding-{source}-{database}-{table}",
        catalog_logical_ref=normalize_ref(source, "public", table),
        connection_id=f"conn-{database}",
        identity=PhysicalObjectIdentityV1(
            catalog_source=source,
            database=database,
            schema=schema,
            table=table,
            object_kind="table",
        ),
    )


def _endpoint(
    source: str,
    table: str,
    columns: tuple[str, ...] = ("customer_id",),
    *,
    binding: PhysicalDatasetBindingV1 | None = None,
    binding_revision_id: str | None = None,
    entity: str = "customer",
    authority: ConceptAuthority = ConceptAuthority.DETERMINISTIC,
) -> IdentifierEndpointV1:
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(
            IdentifierColumnMemberV1(
                normalize_ref(source, "public", table, column),
                "text",
                TypeBasis.ATTESTED,
                KeyMemberRole.PRIMARY if index == 0 else KeyMemberRole.PARTITION,
            )
            for index, column in enumerate(columns)
        ),
        entity_id=entity,
        concept="customer_id",
        concept_authority=authority,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
        physical_binding=binding,
        binding_revision_id=binding_revision_id,
    )


def _assessment(
    left: IdentifierEndpointV1,
    right: IdentifierEndpointV1,
    **changes,
) -> IdentifierLinkAssessmentV1:
    values = {
        "left_endpoint": left,
        "right_endpoint": right,
        "namespace_verdict": NamespaceVerdict.POSSIBLE,
        "governed_population_relation": PopulationRelation.UNKNOWN,
        "assessment_version": "assessment-v1",
        "bridge_fact_key": "bridge-fact-1",
    }
    values.update(changes)
    return IdentifierLinkAssessmentV1(**values)


def _scope() -> RealizationApplicabilityScopeV1:
    return RealizationApplicabilityScopeV1(
        scope_id="pilot-scope",
        execution_tier=ExecutionTier.SANDBOX,
        purposes=("feature_generation",),
        environment="pilot",
    )


def _realization(
    from_endpoint: IdentifierEndpointV1,
    to_endpoint: IdentifierEndpointV1,
    *,
    pairs: tuple[ColumnPairV1, ...] | None = None,
    cardinality: DirectionalCardinalityVerdictV1 | None = None,
    evidence: tuple[EvidenceRefV1, ...] = (),
    predicates=(),
) -> BridgeJoinRealizationRevisionV1:
    pair = ColumnPairV1(
        from_endpoint.members[0].logical_column_ref,
        to_endpoint.members[0].logical_column_ref,
    )
    return BridgeJoinRealizationRevisionV1(
        bridge_fact_key="bridge-fact-1",
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        column_pairs=pairs or (pair,),
        predicates=predicates,
        applicability_scope=_scope(),
        cardinality=cardinality or DirectionalCardinalityVerdictV1(
            Cardinality.MANY_TO_ONE),
        cardinality_basis=CardinalityBasis.GOVERNED_KEY,
        evidence_refs=evidence,
        dependency_snapshot_id="dependency-snapshot-1",
        derivation_version="derive-v1",
        admission_policy_version="admit-v1",
    )


def _executable_pair(
    *,
    left_database: str = "hive-primary",
    right_database: str = "hive-primary",
    columns: tuple[str, ...] = ("customer_id",),
) -> tuple[IdentifierEndpointV1, IdentifierEndpointV1]:
    left_binding = _binding("cib", "customers", database=left_database)
    right_binding = _binding("ftr", "transactions", database=right_database)
    return (
        _endpoint(
            "cib", "customers", columns,
            binding=left_binding, binding_revision_id="binding-revision-left-1"),
        _endpoint(
            "ftr", "transactions", columns,
            binding=right_binding, binding_revision_id="binding-revision-right-1"),
    )


def test_reversing_link_endpoints_preserves_candidate_identity():
    left = _endpoint("cib", "customers")
    right = _endpoint("ftr", "transactions")
    forward = _assessment(left, right)
    reverse = _assessment(right, left)
    assert forward.candidate_id == reverse.candidate_id
    assert forward.candidate_revision_id == reverse.candidate_revision_id
    assert forward.left_endpoint.logical_table_ref == "cib::public.customers"


def test_entity_and_concept_conclusions_change_revision_not_candidate_identity():
    left = _endpoint("cib", "customers")
    right = _endpoint("ftr", "transactions")
    customer = _assessment(left, right)
    account = _assessment(
        replace(left, entity_id="account", concept="account_id"),
        replace(right, entity_id="account", concept="account_id"),
    )
    assert customer.candidate_id == account.candidate_id
    assert customer.candidate_revision_id != account.candidate_revision_id


def test_discovery_endpoint_may_lack_binding_but_executable_endpoint_may_not():
    discovery = _endpoint("cib", "customers")
    assert discovery.executable is False
    with pytest.raises(BridgeContractError, match="resolved binding revision"):
        _realization(discovery, _endpoint("ftr", "transactions"))


def test_empty_and_duplicate_tuple_members_are_rejected():
    with pytest.raises(BridgeContractError, match="must not be empty"):
        IdentifierEndpointV1("cib::public.customers", ())
    member = IdentifierColumnMemberV1(
        "cib::public.customers.customer_id", "text", TypeBasis.ATTESTED)
    with pytest.raises(BridgeContractError, match="duplicates"):
        IdentifierEndpointV1("cib::public.customers", (member, member))


def test_non_flat_logical_ref_is_rejected_instead_of_becoming_a_physical_address():
    with pytest.raises(BridgeContractError, match="flat public namespace"):
        IdentifierColumnMemberV1(
            "cib::banking.customers.customer_id", "text", TypeBasis.ATTESTED)


def test_reversing_direction_changes_realization_identity_and_cardinality_direction():
    left, right = _executable_pair()
    forward = _realization(left, right)
    reverse = _realization(
        right,
        left,
        cardinality=forward.cardinality.inverted(),
    )
    assert forward.realization_id != reverse.realization_id
    assert forward.cardinality.value is Cardinality.MANY_TO_ONE
    assert reverse.cardinality.value is Cardinality.ONE_TO_MANY


def test_adding_business_date_to_mapping_changes_realization_identity():
    left, right = _executable_pair(columns=("customer_id", "business_dt"))
    base = _realization(left, right)
    with_business_date = _realization(
        left,
        right,
        pairs=(
            ColumnPairV1(left.members[0].logical_column_ref, right.members[0].logical_column_ref),
            ColumnPairV1(left.members[1].logical_column_ref, right.members[1].logical_column_ref),
        ),
    )
    assert base.realization_id != with_business_date.realization_id


def test_changing_cardinality_keeps_realization_id_and_changes_revision_id():
    left, right = _executable_pair()
    n_to_one = _realization(left, right)
    one_to_one = _realization(
        left,
        right,
        cardinality=DirectionalCardinalityVerdictV1(Cardinality.ONE_TO_ONE),
    )
    assert n_to_one.realization_id == one_to_one.realization_id
    assert n_to_one.realization_revision_id != one_to_one.realization_revision_id


def test_n_to_one_and_one_to_n_never_hash_as_the_same_revision():
    left, right = _executable_pair()
    n_to_one = _realization(
        left, right,
        cardinality=DirectionalCardinalityVerdictV1(Cardinality.MANY_TO_ONE))
    one_to_n = _realization(
        left, right,
        cardinality=DirectionalCardinalityVerdictV1(Cardinality.ONE_TO_MANY))
    assert n_to_one.realization_revision_id != one_to_n.realization_revision_id


def test_live_evidence_timestamp_is_outside_both_identities():
    left, right = _executable_pair()
    first = EvidenceRefV1(
        "profile-1", EvidenceKind.EXACT_PROFILE, "data-profiler", observed_at=NOW)
    later = replace(first, observed_at=NOW + timedelta(days=1))
    a = _realization(left, right, evidence=(first,))
    b = _realization(left, right, evidence=(later,))
    assert a.realization_id == b.realization_id
    assert a.realization_revision_id == b.realization_revision_id


def test_same_named_physical_table_in_another_hive_database_never_aliases():
    left_a, right = _executable_pair(left_database="hive-primary")
    left_b = replace(
        left_a,
        physical_binding=_binding(
            "cib", "customers", database="hive-disaster-recovery",
            binding_id="binding-cib-dr"),
        binding_revision_id="binding-revision-left-dr-1",
    )
    primary = _realization(left_a, right)
    disaster_recovery = _realization(left_b, right)
    assert primary.realization_id != disaster_recovery.realization_id
    assert left_a.logical_identity_payload() == left_b.logical_identity_payload()


def test_binding_revision_changes_realization_but_not_symmetric_candidate_identity():
    left, right = _executable_pair()
    rebound = replace(left, binding_revision_id="binding-revision-left-2")
    old_assessment = _assessment(left, right)
    new_assessment = _assessment(rebound, right)
    assert old_assessment.candidate_id == new_assessment.candidate_id
    assert old_assessment.candidate_revision_id != new_assessment.candidate_revision_id
    assert _realization(left, right).realization_id != _realization(rebound, right).realization_id


def test_parallel_member_and_type_arrays_are_structurally_impossible():
    endpoint_fields = {item.name for item in fields(IdentifierEndpointV1)}
    assert "members" in endpoint_fields
    assert endpoint_fields.isdisjoint({"member_refs", "member_types", "from_columns", "to_columns"})


def test_free_form_sql_and_duplicate_equality_predicates_are_rejected():
    left, right = _executable_pair(columns=("customer_id", "business_dt"))
    with pytest.raises(BridgeContractError, match="free-form SQL"):
        _realization(left, right, predicates=("x = y",))
    pair = ColumnPairV1(
        left.members[0].logical_column_ref, right.members[0].logical_column_ref)
    duplicate = AdditionalKeyRequirementV1(
        pair.from_logical_column_ref, pair.to_logical_column_ref, "missing_key")
    with pytest.raises(BridgeContractError, match="only in column_pairs"):
        _realization(left, right, pairs=(pair,), predicates=(duplicate,))


def test_unresolved_additional_key_blocks_sandbox_but_review_does_not():
    left, right = _executable_pair(columns=("customer_id", "business_dt"))
    unresolved = AdditionalKeyRequirementV1(
        left.members[1].logical_column_ref,
        right.members[1].logical_column_ref,
        "business_date_mapping_required",
    )
    revision = _realization(left, right, predicates=(unresolved,))
    current = BridgeRealizationCurrentV1(
        revision.realization_id,
        revision.realization_revision_id,
        SafetyStatus.UNASSESSED,
        LinkReviewStatus.HUMAN_VERIFIED,
        RealizationLifecycle.ACTIVE,
        1,
    )
    assert eligible_for_sandbox(revision, current) is False
    assert eligible_for_production(revision, current) is False


def test_unreviewed_deterministically_validated_realization_is_production_eligible():
    left, right = _executable_pair()
    revision = _realization(left, right)
    current = BridgeRealizationCurrentV1(
        revision.realization_id,
        revision.realization_revision_id,
        SafetyStatus.DETERMINISTICALLY_VALIDATED,
        LinkReviewStatus.UNREVIEWED,
        RealizationLifecycle.ACTIVE,
        1,
    )
    assert eligible_for_sandbox(revision, current) is True
    assert eligible_for_production(revision, current) is True


def _governed_availability(db, status: str):
    key = f"bridge-{status.lower()}"
    govern_bridge_fact(
        db,
        key,
        entity="customer",
        left_source="cib",
        left_ref="public.customers.customer_id",
        right_source="ftr",
        right_ref="public.transactions.customer_id",
        status=status,
    )
    return read_identifier_link_availability(
        db, bridge_fact_key=key, candidate_revision_id="candidate-revision-1")


@pytest.mark.parametrize(
    ("status", "folded"),
    [
        ("DRAFT", FoldedLinkStatus.DRAFT),
        ("PARTIALLY_CONFIRMED", FoldedLinkStatus.PARTIALLY_CONFIRMED),
        ("VERIFIED", FoldedLinkStatus.VERIFIED),
    ],
)
def test_available_overlay_states_share_one_mapping(db, status, folded):
    availability = _governed_availability(db, status)
    assert availability.availability is LinkAvailability.AVAILABLE
    assert availability.folded_status is folded
    assert availability.unavailable_reason is None


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("REVERIFY", LinkUnavailableReason.REVERIFY),
        ("STALE", LinkUnavailableReason.STALE),
        ("REJECTED", LinkUnavailableReason.REJECTED),
    ],
)
def test_terminal_or_lapsed_states_are_unavailable(db, status, reason):
    availability = _governed_availability(db, status)
    assert availability.availability is LinkAvailability.UNAVAILABLE
    assert availability.unavailable_reason is reason


def test_human_confirmation_changes_review_not_availability(db):
    draft = _governed_availability(db, "DRAFT")
    verified = _governed_availability(db, "VERIFIED")
    assert draft.availability is verified.availability is LinkAvailability.AVAILABLE
    assert draft.review_status is LinkReviewStatus.UNREVIEWED
    assert verified.review_status is LinkReviewStatus.HUMAN_VERIFIED


def test_source_declared_verified_is_available_but_unreviewed(db):
    service = mint_test_service_identity(
        subject="service:catalog-ingest",
        role_claims=("ingestion-service",),
        attestation="test-workload",
    )
    key = "bridge-source-declared"
    value = {
        "entity_id": "customer",
        "left_ref": {
            "catalog_source": "cib", "object_kind": "column", "schema": "public",
            "table": "customers", "column": "customer_id",
        },
        "right_ref": {
            "catalog_source": "ftr", "object_kind": "column", "schema": "public",
            "table": "transactions", "column": "customer_id",
        },
    }
    proposed = store.append_overlay_event(
        db,
        fact_key=key,
        type=facts.OVERLAY_FACT_PROPOSED,
        actor=service,
        payload={
            "catalog_object_ref": value["left_ref"],
            "object_ref": "public.customers.customer_id",
            "fact_type": "entity_bridge",
            "proposed_value": value,
            "proposal_fingerprint": "source-declared-fingerprint",
            "proposed_by": service.subject,
        },
    )
    store.append_overlay_event(
        db,
        fact_key=key,
        type=facts.OVERLAY_FACT_CONFIRMED,
        actor=service,
        payload={
            "value": value,
            "authority_basis": facts.AUTHORITY_SOURCE_DECLARED,
            "origin_type": "connector",
            "role_claims": list(service.role_claims),
            "confirms_event_id": proposed.event_id,
        },
    )
    availability = read_identifier_link_availability(
        db, bridge_fact_key=key, candidate_revision_id="candidate-revision-1")
    assert availability.availability is LinkAvailability.AVAILABLE
    assert availability.review_status is LinkReviewStatus.UNREVIEWED


def test_missing_or_unreadable_stream_fails_closed(db, monkeypatch):
    missing = read_identifier_link_availability(
        db, bridge_fact_key="missing", candidate_revision_id="candidate-revision-1")
    assert missing.availability is LinkAvailability.UNAVAILABLE
    assert missing.unavailable_reason is LinkUnavailableReason.UNREADABLE

    monkeypatch.setattr(
        "featuregen.overlay.store.load_fact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )
    unreadable = read_identifier_link_availability(
        db, bridge_fact_key="broken", candidate_revision_id="candidate-revision-1")
    assert unreadable.availability is LinkAvailability.UNAVAILABLE
    assert unreadable.unavailable_reason is LinkUnavailableReason.UNREADABLE

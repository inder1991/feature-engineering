"""A4c: the provisional directional-realization producer + R11's preview assessment.

Each of the producer's eight service obligations has a test here, the preview assessment's
verdict table is exercised row by row, and the production reader is proved untouched (its own
suite in ``test_bridge_store.py`` pins the split's byte-identical behavior)."""
from __future__ import annotations

from dataclasses import replace

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.data_agent.binding_store import record_binding, record_connection
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
    record_binding_revision,
)
from featuregen.overlay.upload.bridge_assessment import (
    AvailableIdentifierLinkV1,
    BridgeContractError,
    ConceptAuthority,
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    NamespaceVerdict,
    PopulationRelation,
    TupleKeyRole,
    TypeBasis,
    read_identifier_link_availability,
)
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    LinkReviewStatus,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_realization_proposal import (
    DEPENDENCY_SNAPSHOT_MISMATCH,
    DIRECTIONAL_CARDINALITY_FANS_OUT,
    DIRECTIONAL_MAPPING_INCOMPLETE,
    ENDPOINT_COLUMN_NOT_READABLE,
    IDENTIFIER_LINK_NOT_AVAILABLE,
    REALIZATION_DEMOTED,
    REALIZATION_ENVIRONMENT_MISMATCH,
    REALIZATION_PIN_SUPERSEDED,
    REALIZATION_REVISION_NOT_FOUND,
    SOURCE_BINDING_REVISION_MISSING,
    UNMATCHED_ROW_CONTRADICTION,
    PreviewAssessmentVerdictV1,
    ProvisionalRealizationRefused,
    assess_realization_for_preview,
    produce_provisional_realization,
    refusing_normalization_policy,
    validate_unmatched_row_coherence,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    append_realization_revision,
    bridge_dependency_snapshot_id,
    demote_realization_revision,
    executable_bridge_realizations,
    load_current_bridge_realizations,
    record_realization_revision,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    ContractDefect,
    DrivingTimeRoleV1,
    IntervalBoundaryPolicyV1,
    KnowledgeTimeBasisV2,
    LogicalTemporalJoinSemanticsV1,
    StaticLinkMeaningV1,
    UnmatchedRowMeaningV1,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import (
    ALLOCATION_POLICY_REQUIRED,
    CoverageDenominatorV1,
    CoverageNumeratorV1,
    JoinKeyNormalizationPolicy,
    JoinOrientationV1,
    JoinValidationPolicyRevisionV1,
    NullKeyBehaviorV1,
    SnapshotSelectionRuleV1,
    UnmatchedRowBehaviorV1,
)
from featuregen.overlay.upload.semantic_eligibility_reasons import (
    DIRECTIONAL_CARDINALITY_UNPROVEN,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

FACT = "bridge-fact-a4c"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
def _seed_graph(db, *, columns: tuple[str, ...] = ("customer_id",)) -> None:
    for source, table in (("cib", "customers"), ("ftr", "transactions")):
        rows = [
            (CanonicalRow(source, table, column, "varchar(150)" if source == "cib" else "string"),
             "customer_id")
            for column in columns
        ]
        build_graph(db, source, [r for r, _ in rows],
                    concepts={content_hash(r): c for r, c in rows})


def _connection() -> DataSourceConnectionV1:
    return DataSourceConnectionV1(
        connection_id="conn-hive-primary",
        environment_id="pilot",
        kind="hive",
        host="hive.local",
        port=10000,
        auth_mechanism="kerberos",
        secret_ref="vault://secrets/hive-primary",
        execution_principal="svc-reader",
        allowed_schemas=frozenset({"banking"}),
        active=True,
    )


def _binding(source: str, table: str) -> PhysicalDatasetBindingV1:
    return PhysicalDatasetBindingV1(
        binding_id=f"binding-{source}-hive-primary-{table}",
        catalog_logical_ref=normalize_ref(source, "public", table),
        connection_id="conn-hive-primary",
        identity=PhysicalObjectIdentityV1(
            catalog_source=source, database="hive-primary", schema="banking",
            table=table, object_kind="table"),
    )


def _seed_bindings(db) -> None:
    record_connection(db, _connection())
    for source, table in (("cib", "customers"), ("ftr", "transactions")):
        record_binding(db, _binding(source, table))


def _endpoint(
    source: str,
    table: str,
    columns: tuple[str, ...] = ("customer_id",),
    *,
    key_role: TupleKeyRole = TupleKeyRole.UNKNOWN,
    authority: ConceptAuthority = ConceptAuthority.LLM,
) -> IdentifierEndpointV1:
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(
            IdentifierColumnMemberV1(
                normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED)
            for column in columns
        ),
        entity_id="customer",
        concept="customer_id",
        concept_authority=authority,
        tuple_key_role=key_role,
    )


_LLM_EVIDENCE = EvidenceRefV1(
    evidence_id="llm-proposal-1",
    kind=EvidenceKind.LLM_RECOMMENDATION,
    producer="link-proposer-llm",
)


def _link(
    db,
    *,
    columns: tuple[str, ...] = ("customer_id",),
    to_key_role: TupleKeyRole = TupleKeyRole.UNKNOWN,
    to_authority: ConceptAuthority = ConceptAuthority.LLM,
) -> AvailableIdentifierLinkV1:
    """A governed DRAFT (AI-proposed, unconfirmed) identifier link, readable as available."""
    govern_bridge_fact(
        db, FACT, entity="customer",
        left_source="cib", left_ref=f"public.customers.{columns[0]}",
        right_source="ftr", right_ref=f"public.transactions.{columns[0]}",
        status="DRAFT")
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=_endpoint(
            "cib", "customers", columns, key_role=to_key_role, authority=to_authority),
        right_endpoint=_endpoint("ftr", "transactions", columns),
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1",
        bridge_fact_key=FACT,
        evidence_refs=(_LLM_EVIDENCE,),
    )
    availability = read_identifier_link_availability(
        db, bridge_fact_key=FACT,
        candidate_revision_id=assessment.candidate_revision_id)
    return AvailableIdentifierLinkV1(assessment, availability)


def _pairs(columns: tuple[str, ...] = ("customer_id",)) -> tuple[tuple[str, str], ...]:
    """The transaction→customer traversal mapping, in declared composite order."""
    return tuple(
        (f"ftr::public.transactions.{column}", f"cib::public.customers.{column}")
        for column in columns
    )


def _produce(db, link, *, columns=("customer_id",), roles=(), **overrides):
    values = dict(
        from_logical_table_ref="ftr::public.transactions",
        ordered_member_pairs=_pairs(columns),
        environment="pilot",
        roles=roles,
    )
    values.update(overrides)
    return produce_provisional_realization(db, link, **values)


def _guard_policy(**overrides) -> JoinValidationPolicyRevisionV1:
    values = dict(
        null_key_behavior=NullKeyBehaviorV1.EXCLUDE_ROW,
        unmatched_row_behavior=UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL,
        coverage_numerator=CoverageNumeratorV1.MATCHED_LEFT_ROWS,
        coverage_denominator=CoverageDenominatorV1.NON_NULL_KEY_LEFT_ROWS,
        minimum_coverage_ratio=0.9,
        orientation=JoinOrientationV1.LEFT_DRIVING,
        max_matches_per_left_row=1,
        snapshot_selection_rule=SnapshotSelectionRuleV1.LATEST_AT_OR_BEFORE_CUTOFF,
        applies_to_final_grain_aggregate=True,
        fan_out_control_operator=None,
        declared_by="user:owner",
        declared_at="2026-08-25T00:00:00+00:00",
    )
    values.update(overrides)
    return JoinValidationPolicyRevisionV1(**values)


def _semantics(
    meaning: UnmatchedRowMeaningV1 = UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
) -> LogicalTemporalJoinSemanticsV1:
    return LogicalTemporalJoinSemanticsV1(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=meaning,
        static_link_meaning=StaticLinkMeaningV1.APPLIES_FOR_ALL_TIME,
    )


def _assess(db, produced, **overrides):
    values = dict(
        pinned_realization_revision_id=produced.revision.realization_revision_id,
        pinned_dependency_snapshot_id=produced.revision.dependency_snapshot_id,
        environment_id="pilot",
    )
    values.update(overrides)
    return assess_realization_for_preview(db, **values)


def _stored_fanout_revision(db, *, cardinality: Cardinality, predicates=()):
    """A stored revision the PRODUCER would never mint (known fan-out / unresolved key), built
    through the append half so the assessment's refusal rows are reachable."""
    left, right = _executable_pair(
        columns=("customer_id", "region_code") if predicates else ("customer_id",))
    assert left.physical_binding is not None and right.physical_binding is not None
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    govern_bridge_fact(
        db, "bridge-fact-1", entity="customer",
        left_source="cib", left_ref="public.customers.customer_id",
        right_source="ftr", right_ref="public.transactions.customer_id",
        status="DRAFT")
    from featuregen.overlay.upload.bridge_assessment import (
        read_overlay_identifier_link_state,
    )
    head = read_overlay_identifier_link_state(db, "bridge-fact-1").overlay_head_event_id
    assert head is not None
    base = _realization(
        left, right,
        cardinality=DirectionalCardinalityVerdictV1(cardinality),
        predicates=predicates)
    dependencies = (
        BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", head),
        BridgeDependencyRefV1(
            "physical_binding",
            left.physical_binding.binding_id, left.physical_binding.binding_revision_id),
        BridgeDependencyRefV1(
            "physical_binding",
            right.physical_binding.binding_id, right.physical_binding.binding_revision_id),
    )
    revision = replace(
        base,
        cardinality_basis=CardinalityBasis.EXACT_PROFILE,
        dependency_snapshot_id=bridge_dependency_snapshot_id(dependencies),
    )
    append_realization_revision(db, revision, dependencies=dependencies)
    return revision


# ── the producer's eight obligations ─────────────────────────────────────────────────────────────
def test_complete_mapping_produces_a_provisional_sandbox_realization(db) -> None:
    """Obligations 1+5+7 on the happy path: every member resolved, exact persisted source
    revisions bound, the EXISTING store types reused through the append half."""
    _seed_graph(db)
    _seed_bindings(db)
    produced = _produce(db, _link(db))
    revision = produced.revision

    scope = revision.applicability_scope
    assert scope.execution_tier is ExecutionTier.SANDBOX
    assert scope.purposes == ("feature_generation",)
    assert scope.environment == "pilot"
    assert produced.current.safety_status is SafetyStatus.UNASSESSED
    assert produced.current.lifecycle is RealizationLifecycle.ACTIVE
    assert revision.from_endpoint.logical_table_ref == "ftr::public.transactions"
    assert revision.from_endpoint.binding_revision_id == \
        _binding("ftr", "transactions").binding_revision_id
    assert revision.to_endpoint.binding_revision_id == \
        _binding("cib", "customers").binding_revision_id

    stored = db.execute(
        "SELECT count(*) FROM bridge_join_realization_revision "
        "WHERE realization_revision_id = %s",
        (revision.realization_revision_id,)).fetchone()[0]
    assert stored == 1
    dependency_kinds = [k for (k,) in db.execute(
        "SELECT dependency_kind FROM bridge_realization_dependency "
        "WHERE realization_revision_id = %s ORDER BY dependency_kind",
        (revision.realization_revision_id,)).fetchall()]
    assert dependency_kinds == [
        "bridge_fact", "candidate_revision", "physical_binding", "physical_binding"]


def test_candidate_generation_never_touches_the_shared_current_pointer(db) -> None:
    """R11's core: the append half only — no CAS publish, no global state to compete over."""
    _seed_graph(db)
    _seed_bindings(db)
    _produce(db, _link(db))
    assert db.execute(
        "SELECT count(*) FROM bridge_join_realization_current").fetchone()[0] == 0


def test_idempotent_on_semantic_content(db) -> None:
    """Obligation 8: same inputs → same revision identity, no duplicate row."""
    _seed_graph(db)
    _seed_bindings(db)
    link = _link(db)
    first = _produce(db, link)
    second = _produce(db, link)
    assert first.revision.realization_revision_id == second.revision.realization_revision_id
    assert db.execute(
        "SELECT count(*) FROM bridge_join_realization_revision "
        "WHERE realization_id = %s",
        (first.revision.realization_id,)).fetchone()[0] == 1


def test_composite_key_order_is_preserved_and_is_identity(db) -> None:
    """Obligation 2: the declared pair order survives into the revision, and reordering the
    composite is a DIFFERENT realization."""
    columns = ("customer_id", "region_code")
    _seed_graph(db, columns=columns)
    _seed_bindings(db)
    link = _link(db, columns=columns)
    declared = _produce(db, link, columns=columns)
    reordered = _produce(
        db, link, ordered_member_pairs=tuple(reversed(_pairs(columns))))
    assert [pair.from_logical_column_ref for pair in declared.revision.column_pairs] == [
        "ftr::public.transactions.customer_id", "ftr::public.transactions.region_code"]
    assert declared.revision.realization_id != reordered.revision.realization_id


def test_missing_member_mapping_refuses_by_name(db) -> None:
    """Obligation 3 (missing): a composite member left unresolved blocks production."""
    columns = ("customer_id", "region_code")
    _seed_graph(db, columns=columns)
    _seed_bindings(db)
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, _link(db, columns=columns),
                 ordered_member_pairs=_pairs(("customer_id",)))
    assert refusal.value.code == DIRECTIONAL_MAPPING_INCOMPLETE


def test_ambiguous_member_mapping_refuses_by_name(db) -> None:
    """Obligation 3 (ambiguous): one member mapped twice is not a choice the server may make."""
    _seed_graph(db)
    _seed_bindings(db)
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, _link(db),
                 ordered_member_pairs=_pairs(("customer_id",)) * 2)
    assert refusal.value.code == DIRECTIONAL_MAPPING_INCOMPLETE


def test_mapping_naming_a_non_member_column_refuses(db) -> None:
    _seed_graph(db)
    _seed_bindings(db)
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, _link(db), ordered_member_pairs=(
            ("ftr::public.transactions.customer_id", "cib::public.customers.other_col"),))
    assert refusal.value.code == DIRECTIONAL_MAPPING_INCOMPLETE


def test_hidden_endpoint_column_refuses_and_the_granting_role_lifts_it(db) -> None:
    """Obligation 4: the shipped read-scope rule decides, fail closed."""
    _seed_graph(db)
    _seed_bindings(db)
    link = _link(db)
    db.execute(
        "UPDATE graph_node SET sensitivity='pii' "
        "WHERE catalog_source='cib' AND object_ref='public.customers.customer_id'")
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, link, roles=())
    assert refusal.value.code == ENDPOINT_COLUMN_NOT_READABLE
    assert _produce(db, link, roles=("pii_reader",)).revision is not None


def test_ungoverned_endpoint_column_refuses(db) -> None:
    """Obligation 4's other half: a column the catalog does not describe fails closed."""
    _seed_bindings(db)  # graph deliberately NOT seeded
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, _link(db))
    assert refusal.value.code == ENDPOINT_COLUMN_NOT_READABLE


def test_missing_source_binding_refuses_with_honest_absence(db) -> None:
    """Obligation 5: physical_dataset_binding_revision is EMPTY live — the producer refuses,
    it never fabricates a binding."""
    _seed_graph(db)
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, _link(db))
    assert refusal.value.code == SOURCE_BINDING_REVISION_MISSING


def test_binding_without_a_persisted_revision_refuses(db) -> None:
    """A flat address row whose immutable revision was never recorded is a dead reference —
    exactly the state the named refusal exists for."""
    _seed_graph(db)
    record_connection(db, _connection())
    for source, table in (("cib", "customers"), ("ftr", "transactions")):
        binding = _binding(source, table)
        identity = binding.identity
        db.execute(
            "INSERT INTO physical_dataset_binding (binding_id, catalog_source, "
            "catalog_logical_ref, connection_id, database_name, schema_name, table_name, "
            "object_kind) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (binding.binding_id, identity.catalog_source, binding.catalog_logical_ref,
             binding.connection_id, identity.database, identity.schema, identity.table,
             identity.object_kind))
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, _link(db))
    assert refusal.value.code == SOURCE_BINDING_REVISION_MISSING


def test_rejected_link_refuses_production(db) -> None:
    _seed_graph(db)
    _seed_bindings(db)
    link = _link(db)
    govern_bridge_fact(
        db, FACT, entity="customer",
        left_source="cib", left_ref="public.customers.customer_id",
        right_source="ftr", right_ref="public.transactions.customer_id",
        status="REJECTED")
    with pytest.raises(ProvisionalRealizationRefused) as refusal:
        _produce(db, link)
    assert refusal.value.code == IDENTIFIER_LINK_NOT_AVAILABLE


def test_the_ai_proposed_evidence_is_recorded_verbatim(db) -> None:
    """Obligation 6: the proposal's provenance rides the revision — recorded, never upgraded."""
    _seed_graph(db)
    _seed_bindings(db)
    produced = _produce(db, _link(db))
    assert produced.revision.evidence_refs == (_LLM_EVIDENCE,)
    assert produced.revision.strongest_evidence_label == "llm_only"


def test_cardinality_is_unknown_unless_deterministically_proven(db) -> None:
    _seed_graph(db)
    _seed_bindings(db)
    ai_asserted = _produce(
        db, _link(db, to_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
                  to_authority=ConceptAuthority.LLM))
    assert not ai_asserted.revision.cardinality.known
    assert ai_asserted.revision.cardinality_basis is CardinalityBasis.NONE


def test_governed_unique_target_key_proves_n_to_1(db) -> None:
    _seed_graph(db)
    _seed_bindings(db)
    proven = _produce(
        db, _link(db, to_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
                  to_authority=ConceptAuthority.DETERMINISTIC))
    assert proven.revision.cardinality.value is Cardinality.MANY_TO_ONE
    assert proven.revision.cardinality_basis is CardinalityBasis.GOVERNED_KEY


def test_normalization_policy_is_explicit_governed_or_refusing(db) -> None:
    _seed_graph(db)
    _seed_bindings(db)
    link = _link(db)
    fail_closed = _produce(db, link).key_normalization
    assert fail_closed == refusing_normalization_policy()
    assert fail_closed.declared_type_coercions == ()
    governed = replace(
        refusing_normalization_policy(),
        declared_type_coercions=(("varchar(150)", "string"),))
    assert isinstance(governed, JoinKeyNormalizationPolicy)
    carried = _produce(db, link, key_normalization=governed).key_normalization
    assert carried.declared_type_coercions == (("varchar(150)", "string"),)


def test_direction_must_name_a_link_endpoint(db) -> None:
    _seed_graph(db)
    _seed_bindings(db)
    with pytest.raises(BridgeContractError, match="neither link endpoint"):
        _produce(db, _link(db), from_logical_table_ref="cib::public.other_table")


def test_produced_realization_never_enters_the_production_reader(db) -> None:
    """The plan's third A4c test: provisional output is invisible to the production path —
    no current pointer exists at all, and the reader's own gates are untouched."""
    _seed_graph(db)
    _seed_bindings(db)
    _produce(db, _link(db))
    assert load_current_bridge_realizations(db) == ()
    assert executable_bridge_realizations(
        db, purpose="feature_generation", environment="pilot") == ()


# ── the preview assessment (R11) ─────────────────────────────────────────────────────────────────
def _produced(db, **link_kwargs):
    _seed_graph(db)
    _seed_bindings(db)
    return _produce(db, _link(db, **link_kwargs))


def test_unknown_cardinality_with_pinned_guard_policy_is_provisional_with_guards(db) -> None:
    produced = _produced(db)
    policy = _guard_policy()
    result = _assess(
        db, produced,
        join_validation_policy_revision_id=policy.revision_id,
        join_validation_policy=policy)
    assert result.verdict is PreviewAssessmentVerdictV1.PROVISIONAL_WITH_GUARDS
    assert result.reason_codes == ()
    assert result.realization == produced.revision


def test_unknown_cardinality_without_guard_policy_refuses(db) -> None:
    produced = _produced(db)
    result = _assess(db, produced)
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert DIRECTIONAL_CARDINALITY_UNPROVEN in result.reason_codes


def test_proven_n_to_1_yields_the_full_preview_verdict(db) -> None:
    produced = _produced(
        db, to_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
        to_authority=ConceptAuthority.DETERMINISTIC)
    result = _assess(db, produced)
    assert result.verdict is PreviewAssessmentVerdictV1.FULL_PREVIEW
    assert result.reason_codes == ()


def test_known_m_to_n_refuses_allocation_policy_required_for_final_grain(db) -> None:
    revision = _stored_fanout_revision(db, cardinality=Cardinality.MANY_TO_MANY)
    result = assess_realization_for_preview(
        db,
        pinned_realization_revision_id=revision.realization_revision_id,
        pinned_dependency_snapshot_id=revision.dependency_snapshot_id,
        environment_id="pilot")
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert ALLOCATION_POLICY_REQUIRED in result.reason_codes


def test_known_fanout_scoped_away_from_final_grain_still_refuses_by_its_own_name(db) -> None:
    revision = _stored_fanout_revision(db, cardinality=Cardinality.ONE_TO_MANY)
    policy = _guard_policy(applies_to_final_grain_aggregate=False, max_matches_per_left_row=5)
    result = assess_realization_for_preview(
        db,
        pinned_realization_revision_id=revision.realization_revision_id,
        pinned_dependency_snapshot_id=revision.dependency_snapshot_id,
        environment_id="pilot",
        join_validation_policy_revision_id=policy.revision_id,
        join_validation_policy=policy)
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert DIRECTIONAL_CARDINALITY_FANS_OUT in result.reason_codes


def test_unresolved_additional_key_refuses_as_incomplete_mapping(db) -> None:
    revision = _stored_fanout_revision(
        db, cardinality=Cardinality.MANY_TO_ONE,
        predicates=(AdditionalKeyRequirementV1(
            "cib::public.customers.region_code",
            "ftr::public.transactions.region_code",
            "second_member_unmapped"),))
    result = assess_realization_for_preview(
        db,
        pinned_realization_revision_id=revision.realization_revision_id,
        pinned_dependency_snapshot_id=revision.dependency_snapshot_id,
        environment_id="pilot")
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert DIRECTIONAL_MAPPING_INCOMPLETE in result.reason_codes


def test_a_staled_link_refuses_its_pinned_realization(db) -> None:
    """The withdrawn/revoked staleness row lands here: the assessment re-reads the lifecycle."""
    produced = _produced(db)
    govern_bridge_fact(
        db, FACT, entity="customer",
        left_source="cib", left_ref="public.customers.customer_id",
        right_source="ftr", right_ref="public.transactions.customer_id",
        status="STALE")
    result = _assess(db, produced)
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert IDENTIFIER_LINK_NOT_AVAILABLE in result.reason_codes


def test_a_rejected_link_refuses_its_pinned_realization(db) -> None:
    produced = _produced(db)
    govern_bridge_fact(
        db, FACT, entity="customer",
        left_source="cib", left_ref="public.customers.customer_id",
        right_source="ftr", right_ref="public.transactions.customer_id",
        status="REJECTED")
    result = _assess(db, produced)
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert IDENTIFIER_LINK_NOT_AVAILABLE in result.reason_codes


def test_unknown_pin_refuses_not_found(db) -> None:
    result = assess_realization_for_preview(
        db,
        pinned_realization_revision_id="no-such-revision",
        pinned_dependency_snapshot_id="no-such-snapshot",
        environment_id="pilot")
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert result.reason_codes == (REALIZATION_REVISION_NOT_FOUND,)
    assert result.realization is None


def test_wrong_dependency_snapshot_pin_refuses(db) -> None:
    produced = _produced(db)
    result = _assess(db, produced, pinned_dependency_snapshot_id="brds_somethingelse")
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert DEPENDENCY_SNAPSHOT_MISMATCH in result.reason_codes


def test_environment_mismatch_refuses(db) -> None:
    produced = _produced(db)
    result = _assess(db, produced, environment_id="uat")
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert REALIZATION_ENVIRONMENT_MISMATCH in result.reason_codes


def test_a_published_pointer_naming_a_newer_revision_supersedes_the_pin(db) -> None:
    """The staleness law: superseding realization → refuse old pin; adoption is the path."""
    produced = _produced(db)
    newer = replace(produced.revision, derivation_version="provisional-newer-v2")
    assert newer.realization_id == produced.revision.realization_id
    assert newer.realization_revision_id != produced.revision.realization_revision_id
    record_realization_revision(
        db, newer,
        BridgeRealizationCurrentV1(
            newer.realization_id, newer.realization_revision_id,
            SafetyStatus.UNASSESSED, LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE, 1),
        dependencies=produced.dependencies)
    result = _assess(db, produced)
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert REALIZATION_PIN_SUPERSEDED in result.reason_codes


def test_a_demoted_published_revision_refuses_its_pin(db) -> None:
    produced = _produced(db)
    record_realization_revision(
        db, produced.revision, produced.current, dependencies=produced.dependencies)
    demote_realization_revision(db, produced.revision.realization_revision_id)
    result = _assess(db, produced)
    assert result.verdict is PreviewAssessmentVerdictV1.REFUSED
    assert REALIZATION_DEMOTED in result.reason_codes


def test_guard_policy_pin_must_name_the_supplied_revision(db) -> None:
    produced = _produced(db)
    policy = _guard_policy()
    with pytest.raises(ContractDefect, match="never a look-alike"):
        _assess(
            db, produced,
            join_validation_policy_revision_id="jvp_" + "0" * 64,
            join_validation_policy=policy)
    with pytest.raises(ContractDefect, match="come together"):
        _assess(db, produced, join_validation_policy=policy)


# ── the unmatched-row coherence validation (step-3 carry-forward) ────────────────────────────────
def test_contradictory_unmatched_row_pairs_refuse_by_name() -> None:
    for meaning, behavior in (
        (UnmatchedRowMeaningV1.EXCLUDE_DRIVING_ROW, UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL),
        (UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE, UnmatchedRowBehaviorV1.EXCLUDE),
    ):
        with pytest.raises(ContractDefect, match=UNMATCHED_ROW_CONTRADICTION) as defect:
            validate_unmatched_row_coherence(meaning, behavior)
        assert defect.value.code == UNMATCHED_ROW_CONTRADICTION


def test_coherent_unmatched_row_pairs_pass() -> None:
    validate_unmatched_row_coherence(
        UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL)
    validate_unmatched_row_coherence(
        UnmatchedRowMeaningV1.EXCLUDE_DRIVING_ROW, UnmatchedRowBehaviorV1.EXCLUDE)
    validate_unmatched_row_coherence(
        UnmatchedRowMeaningV1.REFUSE, UnmatchedRowBehaviorV1.REFUSE)


def test_assessment_runs_the_coherence_validation_when_it_holds_both(db) -> None:
    produced = _produced(db)
    policy = _guard_policy(unmatched_row_behavior=UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL)
    with pytest.raises(ContractDefect, match=UNMATCHED_ROW_CONTRADICTION):
        _assess(
            db, produced,
            join_validation_policy_revision_id=policy.revision_id,
            join_validation_policy=policy,
            logical_temporal_semantics=_semantics(UnmatchedRowMeaningV1.EXCLUDE_DRIVING_ROW))
    coherent = _assess(
        db, produced,
        join_validation_policy_revision_id=policy.revision_id,
        join_validation_policy=policy,
        logical_temporal_semantics=_semantics(
            UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE))
    assert coherent.verdict is PreviewAssessmentVerdictV1.PROVISIONAL_WITH_GUARDS


# ── the production reader is untouched ───────────────────────────────────────────────────────────
def test_even_a_published_provisional_revision_stays_out_of_the_production_reader(db) -> None:
    """Publishing the pointer (a later act, not A4c's) STILL does not reach production: the
    reader's own gates — production tier, deterministic safety, known cardinality — hold."""
    produced = _produced(db)
    record_realization_revision(
        db, produced.revision, produced.current, dependencies=produced.dependencies)
    assert executable_bridge_realizations(
        db, purpose="feature_generation", environment="pilot") == ()

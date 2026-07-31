from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _assessment,
    _executable_pair,
    _realization,
)
from tests.featuregen.overlay.upload.test_bridge_cardinality import _key

from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.overlay.upload.bridge_admission import (
    BridgeAdmissionPolicyV1,
    BridgeDisplayTier,
    admitted_realization_revision,
    evaluate_bridge_admission,
)
from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    EvidenceKind,
    EvidenceRefV1,
    NamespaceVerdict,
    PopulationRelation,
)
from featuregen.overlay.upload.bridge_cardinality import infer_metadata_cardinality
from featuregen.overlay.upload.bridge_realization import SafetyStatus
from featuregen.overlay.upload.planner.multisource_contracts import (
    GrainAuthorityProvenance,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
POLICY = BridgeAdmissionPolicyV1()


def _observation(
    revision,
    *,
    matched_left: int = 80,
    unmatched_left: int = 0,
    matched_right: int = 80,
    unmatched_right: int = 0,
    right_rows: int = 80,
    right_distinct: int = 80,
    right_duplicates: int = 0,
    right_max_per_tuple: int = 1,
    max_right_matches: int = 1,
    left_partitions: tuple[str, ...] = (),
    right_partitions: tuple[str, ...] = (),
) -> RelationshipObservationV2:
    assert revision.from_endpoint.binding_revision_id is not None
    assert revision.to_endpoint.binding_revision_id is not None
    assert revision.from_endpoint.physical_binding is not None
    assert revision.to_endpoint.physical_binding is not None
    left = EndpointTupleObservationV2(
        revision.from_endpoint.physical_binding.identity.table_id,
        revision.from_endpoint.binding_revision_id,
        revision.from_endpoint.physical_binding.content_hash,
        ("customer_id",),
        100,
        100,
        80,
        20,
        20,
        3,
        left_partitions,
    )
    right = EndpointTupleObservationV2(
        revision.to_endpoint.physical_binding.identity.table_id,
        revision.to_endpoint.binding_revision_id,
        revision.to_endpoint.physical_binding.content_hash,
        ("customer_id",),
        right_rows,
        right_rows,
        right_distinct,
        1 if right_duplicates else 0,
        right_duplicates,
        right_max_per_tuple,
        right_partitions,
    )
    return RelationshipObservationV2(
        realization_revision_id=revision.realization_revision_id,
        plan_hash="probe-plan-v1",
        scope_id=revision.applicability_scope.scope_id,
        left=left,
        right=right,
        matched_left_distinct=matched_left,
        unmatched_left_distinct=unmatched_left,
        matched_right_distinct=matched_right,
        unmatched_right_distinct=unmatched_right,
        left_orphan_rows=unmatched_left,
        right_orphan_rows=unmatched_right,
        joined_row_count=100,
        max_right_matches_per_left_row=max_right_matches,
        max_left_matches_per_right_row=3,
        normalization_ids=("identity_v1",),
        predicate_ids=(),
        left_source_snapshot_id="left-snapshot-1",
        right_source_snapshot_id="right-snapshot-1",
        snapshot_or_as_of="2026-07-30",
        execution_principal="relationship-profiler",
        method="exact",
        row_coverage=RowCoverage.FULL,
        complete=True,
        observed_at=NOW,
    )


def _pair_and_revision():
    left, right = _executable_pair()
    return left, right, _realization(left, right)


def test_exact_data_evidence_validates_without_human_review() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(
        left,
        right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
    )
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=_observation(revision),
        source_snapshots_current=True,
    )
    assert decision.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert decision.cardinality.value is Cardinality.MANY_TO_ONE
    assert decision.display_tier is BridgeDisplayTier.STRONG_PROPOSED
    assert decision.production_executable


def test_source_unique_target_validates_static_cardinality_without_human() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(
        left,
        right,
        namespace_verdict=NamespaceVerdict.SAME,
        evidence_refs=(
            EvidenceRefV1(
                "source-namespace-1",
                EvidenceKind.SOURCE_CONSTRAINT,
                "catalog-ingest",
            ),
        ),
    )
    metadata = infer_metadata_cardinality(
        left,
        right,
        from_complete_key=_key(
            "cib", "customers", "different_source_key", unique=False),
        to_complete_key=_key(
            "ftr",
            "transactions",
            "customer_id",
            authority=GrainAuthorityProvenance.source_declared,
        ),
    )
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        metadata_cardinality=metadata,
    )
    assert decision.static_cardinality_validated
    assert decision.cardinality.value is Cardinality.MANY_TO_ONE
    # Static keyness proves no target fan-out. The exact relationship probe still owns
    # containment/current-data safety, so the production predicate remains closed here.
    assert decision.safety_status is SafetyStatus.UNASSESSED
    assert decision.display_tier is BridgeDisplayTier.STRONG_PROPOSED


def test_draft_or_nonunique_target_cannot_validate_static_cardinality() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(left, right, namespace_verdict=NamespaceVerdict.SAME)
    draft = infer_metadata_cardinality(
        left,
        right,
        from_complete_key=None,
        to_complete_key=None,
    )
    draft_decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        metadata_cardinality=draft,
    )
    assert not draft_decision.static_cardinality_validated
    assert not draft_decision.cardinality.known

    nonunique = infer_metadata_cardinality(
        left,
        right,
        from_complete_key=_key("cib", "customers", "customer_id", unique=False),
        to_complete_key=_key("ftr", "transactions", "customer_id", unique=False),
    )
    unsafe = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        metadata_cardinality=nonunique,
    )
    assert unsafe.safety_status is SafetyStatus.UNSAFE
    assert "governed_target_key_not_unique" in unsafe.reason_codes


def test_llm_only_candidate_can_pass_exact_probe_without_human() -> None:
    left, right, revision = _pair_and_revision()
    llm_ref = EvidenceRefV1(
        "llm-result-1",
        EvidenceKind.LLM_RECOMMENDATION,
        "bridge_critic:1",
    )
    assessment = _assessment(
        replace(left, concept_authority=ConceptAuthority.LLM),
        replace(right, concept_authority=ConceptAuthority.LLM),
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        evidence_refs=(llm_ref,),
        explanation_codes=("llm_only",),
    )
    assert assessment.strongest_evidence_label == "llm_only"
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=_observation(revision),
        source_snapshots_current=True,
    )
    assert decision.production_executable
    assert decision.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED


def test_confident_llm_or_human_cannot_override_observed_fanout() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(
        left,
        right,
        namespace_verdict=NamespaceVerdict.SAME,
        evidence_refs=(
            EvidenceRefV1(
                "human-namespace-1",
                EvidenceKind.HUMAN_ATTESTATION,
                "governance-ui",
            ),
            EvidenceRefV1(
                "confident-llm-1",
                EvidenceKind.LLM_RECOMMENDATION,
                "bridge_critic:1",
            ),
        ),
    )
    fanout = _observation(
        revision,
        right_rows=81,
        right_distinct=80,
        right_duplicates=1,
        right_max_per_tuple=2,
        max_right_matches=2,
    )
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=fanout,
        source_snapshots_current=True,
    )
    assert decision.safety_status is SafetyStatus.UNSAFE
    assert decision.display_tier is BridgeDisplayTier.REJECTED
    assert "observed_target_duplicate_or_fanout" in decision.reason_codes


def test_partition_proof_cannot_validate_unrestricted_realization() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(left, right)
    scoped = _observation(
        revision,
        left_partitions=("business_dt=2026-07-30",),
        right_partitions=("business_dt=2026-07-30",),
    )
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=scoped,
        source_snapshots_current=True,
    )
    assert not decision.production_executable
    assert (
        "partition_scoped_evidence_cannot_validate_unrestricted_realization"
        in decision.reason_codes
    )


def test_wrong_tuple_or_noncurrent_snapshots_cannot_authorize() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(left, right)
    current_data = _observation(revision)
    stale = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=current_data,
    )
    assert stale.display_tier is BridgeDisplayTier.STALE
    assert "source_snapshot_not_current" in stale.reason_codes

    wrong_tuple = replace(
        current_data,
        left=replace(current_data.left, columns=("some_other_id",)),
    )
    mismatched = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=wrong_tuple,
        source_snapshots_current=True,
    )
    assert not mismatched.production_executable
    assert "observation_tuple_mismatch" in mismatched.reason_codes


def test_llm_subset_hypothesis_cannot_lower_unknown_population_threshold() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(
        left,
        right,
        governed_population_relation=PopulationRelation.UNKNOWN,
        population_hypothesis=PopulationRelation.LEFT_SUBSET.value,
    )
    low_containment = _observation(
        revision,
        matched_left=60,
        unmatched_left=20,
        matched_right=60,
        unmatched_right=20,
    )
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=low_containment,
        source_snapshots_current=True,
    )
    assert not decision.production_executable
    assert "left_containment_below_policy" in decision.reason_codes


def test_human_review_alone_does_not_make_unassessed_link_executable() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(
        left,
        right,
        namespace_verdict=NamespaceVerdict.SAME,
        evidence_refs=(
            EvidenceRefV1(
                "human-review-1",
                EvidenceKind.HUMAN_ATTESTATION,
                "governance-ui",
            ),
        ),
    )
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
    )
    assert decision.safety_status is SafetyStatus.UNASSESSED
    assert not decision.production_executable


def test_admission_creates_new_conclusion_revision_not_new_join_identity() -> None:
    left, right, revision = _pair_and_revision()
    assessment = _assessment(left, right)
    decision = evaluate_bridge_admission(
        assessment,
        revision,
        policy=POLICY,
        now=NOW,
        observation=_observation(revision),
        source_snapshots_current=True,
    )
    admitted = admitted_realization_revision(revision, decision)
    assert admitted.realization_id == revision.realization_id
    assert admitted.realization_revision_id != revision.realization_revision_id
    assert admitted.admission_policy_version == POLICY.policy_version
    assert admitted.cardinality_basis.value == "exact_profile"

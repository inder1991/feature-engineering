"""Deterministic admission policy for directional identifier-link realizations.

This module deliberately has no LLM client.  Semantic recommendations may make a candidate worth
probing, but only governed metadata and scope-matched observations can establish execution safety.
Human review is an independent display/accountability axis and is never consulted here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from featuregen.data_agent.relationship_observation import (
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    IdentifierLinkAssessmentV1,
    NamespaceVerdict,
    PopulationRelation,
    strongest_evidence_label,
)
from featuregen.overlay.upload.bridge_cardinality import (
    EndpointKeyness,
    MetadataCardinalityAssessmentV1,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    CardinalityBasis,
    DirectionalCardinalityVerdictV1,
    SafetyStatus,
)
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.planner.multisource_contracts import (
    GrainAuthorityProvenance,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

BRIDGE_ADMISSION_POLICY_CONTRACT_VERSION = "1.0.0"


class BridgeDisplayTier(StrEnum):
    DISCOVERED = "discovered"
    STRONG_PROPOSED = "strong_proposed"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class PopulationThresholdV1:
    minimum_left_distinct_containment: float
    minimum_right_distinct_containment: float
    maximum_left_row_orphan_ratio: float
    maximum_right_row_orphan_ratio: float

    def __post_init__(self) -> None:
        for value in (
            self.minimum_left_distinct_containment,
            self.minimum_right_distinct_containment,
            self.maximum_left_row_orphan_ratio,
            self.maximum_right_row_orphan_ratio,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("population admission thresholds must be in [0,1]")


@dataclass(frozen=True, slots=True)
class BridgeAdmissionPolicyV1:
    """Versioned automatic-safety policy.

    Full exact observations are not statistical samples, so the default minimum is one non-empty
    row per endpoint. Deployments may set a higher operational floor without changing the contract.
    """

    minimum_observed_rows_per_endpoint: int = 1
    minimum_observed_partitions_per_endpoint: int = 0
    maximum_evidence_age: timedelta = timedelta(days=30)
    maximum_left_null_ratio: float = 0.05
    maximum_right_null_ratio: float = 0.0
    maximum_left_row_amplification: float = 1.0
    allowed_normalization_ids: tuple[str, ...] = ("identity_v1",)
    allowed_predicate_ids: tuple[str, ...] = ()
    same_population: PopulationThresholdV1 = PopulationThresholdV1(0.95, 0.95, 0.05, 0.05)
    left_subset_population: PopulationThresholdV1 = PopulationThresholdV1(
        0.95, 0.0, 0.05, 1.0)
    right_subset_population: PopulationThresholdV1 = PopulationThresholdV1(
        0.0, 0.95, 1.0, 0.05)
    partial_overlap_population: PopulationThresholdV1 = PopulationThresholdV1(
        0.05, 0.05, 0.95, 0.95)
    conservative_unknown_population: PopulationThresholdV1 = PopulationThresholdV1(
        0.95, 0.0, 0.05, 1.0)

    def __post_init__(self) -> None:
        if self.minimum_observed_rows_per_endpoint < 1:
            raise ValueError("minimum observed rows must be positive")
        if self.minimum_observed_partitions_per_endpoint < 0:
            raise ValueError("minimum observed partitions must not be negative")
        if self.maximum_evidence_age <= timedelta(0):
            raise ValueError("maximum evidence age must be positive")
        for value in (
            self.maximum_left_null_ratio,
            self.maximum_right_null_ratio,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("null-ratio thresholds must be in [0,1]")
        if self.maximum_left_row_amplification < 1.0:
            raise ValueError("row amplification threshold must be at least one")
        if len(set(self.allowed_normalization_ids)) != len(self.allowed_normalization_ids):
            raise ValueError("allowed normalization ids must not contain duplicates")
        if len(set(self.allowed_predicate_ids)) != len(self.allowed_predicate_ids):
            raise ValueError("allowed predicate ids must not contain duplicates")

    @property
    def policy_version(self) -> str:
        return "bridge-admission-v1:" + canonical_hash(self.identity_payload())[:16]

    def identity_payload(self) -> dict[str, object]:
        def threshold(value: PopulationThresholdV1) -> dict[str, float]:
            return {
                "minimum_left_distinct_containment":
                    value.minimum_left_distinct_containment,
                "minimum_right_distinct_containment":
                    value.minimum_right_distinct_containment,
                "maximum_left_row_orphan_ratio": value.maximum_left_row_orphan_ratio,
                "maximum_right_row_orphan_ratio": value.maximum_right_row_orphan_ratio,
            }

        return {
            "contract_version": BRIDGE_ADMISSION_POLICY_CONTRACT_VERSION,
            "minimum_observed_rows_per_endpoint":
                self.minimum_observed_rows_per_endpoint,
            "minimum_observed_partitions_per_endpoint":
                self.minimum_observed_partitions_per_endpoint,
            "maximum_evidence_age_seconds": int(self.maximum_evidence_age.total_seconds()),
            "maximum_left_null_ratio": self.maximum_left_null_ratio,
            "maximum_right_null_ratio": self.maximum_right_null_ratio,
            "maximum_left_row_amplification": self.maximum_left_row_amplification,
            "allowed_normalization_ids": list(self.allowed_normalization_ids),
            "allowed_predicate_ids": list(self.allowed_predicate_ids),
            "population_thresholds": {
                PopulationRelation.SAME.value: threshold(self.same_population),
                PopulationRelation.LEFT_SUBSET.value:
                    threshold(self.left_subset_population),
                PopulationRelation.RIGHT_SUBSET.value:
                    threshold(self.right_subset_population),
                PopulationRelation.PARTIAL_OVERLAP.value:
                    threshold(self.partial_overlap_population),
                PopulationRelation.UNKNOWN.value:
                    threshold(self.conservative_unknown_population),
            },
        }

    def threshold_for(self, relation: PopulationRelation) -> PopulationThresholdV1 | None:
        return {
            PopulationRelation.SAME: self.same_population,
            PopulationRelation.LEFT_SUBSET: self.left_subset_population,
            PopulationRelation.RIGHT_SUBSET: self.right_subset_population,
            PopulationRelation.PARTIAL_OVERLAP: self.partial_overlap_population,
            PopulationRelation.UNKNOWN: self.conservative_unknown_population,
            PopulationRelation.DISJOINT: None,
        }[relation]


@dataclass(frozen=True, slots=True)
class BridgeAdmissionDecisionV1:
    safety_status: SafetyStatus
    display_tier: BridgeDisplayTier
    cardinality: DirectionalCardinalityVerdictV1
    cardinality_basis: CardinalityBasis
    static_cardinality_validated: bool
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[EvidenceRefV1, ...]
    evidence_label: str | None
    policy_version: str

    @property
    def production_executable(self) -> bool:
        return self.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED


def _display_evidence_label(evidence: tuple[EvidenceRefV1, ...]) -> str | None:
    if evidence and all(
        item.kind is EvidenceKind.LLM_RECOMMENDATION for item in evidence
    ):
        return "llm_only"
    return strongest_evidence_label(evidence)


def _dedupe_evidence(
    *groups: tuple[EvidenceRefV1, ...],
) -> tuple[EvidenceRefV1, ...]:
    return tuple(sorted(
        {
            (ref.evidence_id, ref.kind.value, ref.producer): ref
            for group in groups
            for ref in group
        }.values(),
        key=lambda ref: (ref.evidence_id, ref.kind.value, ref.producer),
    ))


def _observation_evidence(observation: RelationshipObservationV2) -> EvidenceRefV1:
    kind = (
        EvidenceKind.EXACT_PROFILE
        if observation.method == "exact"
        else EvidenceKind.APPROXIMATE_PROFILE
    )
    return EvidenceRefV1(
        evidence_id=observation.observation_revision_id,
        kind=kind,
        producer=observation.producer,
        content_hash=observation.plan_hash,
        observed_at=observation.observed_at,
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _representation_compatible(
    revision: BridgeJoinRealizationRevisionV1,
    observation: RelationshipObservationV2 | None,
) -> bool:
    from_by_ref = {
        member.logical_column_ref: member for member in revision.from_endpoint.members}
    to_by_ref = {
        member.logical_column_ref: member for member in revision.to_endpoint.members}
    for pair in revision.column_pairs:
        left_family = from_by_ref[pair.from_logical_column_ref].data_type_family.strip().lower()
        right_family = to_by_ref[pair.to_logical_column_ref].data_type_family.strip().lower()
        if left_family in {"", "unknown", "other"} or left_family != right_family:
            return False
    if observation is not None and len(observation.normalization_ids) != len(
        revision.column_pairs
    ):
        return False
    return True


def _observation_matches_realization(
    observation: RelationshipObservationV2,
    revision: BridgeJoinRealizationRevisionV1,
) -> bool:
    expected_left_columns = tuple(
        parse_ref(pair.from_logical_column_ref)[3]
        for pair in revision.column_pairs
    )
    expected_right_columns = tuple(
        parse_ref(pair.to_logical_column_ref)[3]
        for pair in revision.column_pairs
    )
    return (
        observation.realization_revision_id == revision.realization_revision_id
        and observation.scope_id == revision.applicability_scope.scope_id
        and observation.left.binding_revision_id
        == revision.from_endpoint.binding_revision_id
        and observation.right.binding_revision_id
        == revision.to_endpoint.binding_revision_id
        and observation.left.columns == expected_left_columns
        and observation.right.columns == expected_right_columns
    )


def _static_cardinality(
    metadata: MetadataCardinalityAssessmentV1 | None,
) -> tuple[DirectionalCardinalityVerdictV1, CardinalityBasis, bool, bool]:
    """Return cardinality, basis, validated, hard target-nonunique conflict."""
    if metadata is None:
        return DirectionalCardinalityVerdictV1.unknown(), CardinalityBasis.NONE, False, False
    target = metadata.to_key
    if target.keyness is EndpointKeyness.COMPLETE_NON_UNIQUE_GRAIN:
        return metadata.cardinality, metadata.cardinality_basis, False, True
    key = target.complete_key
    authoritative_unique = (
        target.keyness is EndpointKeyness.COMPLETE_UNIQUE_KEY
        and key is not None
        and key.is_unique
        and key.authority_provenance in {
            GrainAuthorityProvenance.catalog_authoritative,
            GrainAuthorityProvenance.source_declared,
            GrainAuthorityProvenance.human_confirmed,
        }
    )
    if not authoritative_unique:
        return DirectionalCardinalityVerdictV1.unknown(), CardinalityBasis.NONE, False, False
    source_unique = metadata.from_key.unique_verdict is True
    cardinality = DirectionalCardinalityVerdictV1(
        Cardinality.ONE_TO_ONE if source_unique else Cardinality.MANY_TO_ONE)
    return cardinality, CardinalityBasis.GOVERNED_KEY, True, False


def _decision(
    *,
    policy: BridgeAdmissionPolicyV1,
    safety: SafetyStatus,
    tier: BridgeDisplayTier,
    cardinality: DirectionalCardinalityVerdictV1,
    basis: CardinalityBasis,
    static_validated: bool,
    reasons: list[str],
    evidence: tuple[EvidenceRefV1, ...],
) -> BridgeAdmissionDecisionV1:
    reasons = sorted(set(reasons))
    return BridgeAdmissionDecisionV1(
        safety,
        tier,
        cardinality,
        basis,
        static_validated,
        tuple(reasons),
        evidence,
        _display_evidence_label(evidence),
        policy.policy_version,
    )


def evaluate_bridge_admission(
    assessment: IdentifierLinkAssessmentV1,
    revision: BridgeJoinRealizationRevisionV1,
    *,
    policy: BridgeAdmissionPolicyV1,
    now: datetime,
    metadata_cardinality: MetadataCardinalityAssessmentV1 | None = None,
    observation: RelationshipObservationV2 | None = None,
    conflicting_observation: RelationshipObservationV2 | None = None,
    link_available: bool = True,
    bindings_current: bool = True,
    source_snapshots_current: bool = False,
    expected_normalization_ids: tuple[str, ...] | None = None,
    expected_predicate_ids: tuple[str, ...] = (),
) -> BridgeAdmissionDecisionV1:
    """Fuse governed metadata and scoped observations with hard conflicts taking precedence."""
    evidence = assessment.evidence_refs
    static_cardinality, static_basis, static_validated, target_nonunique = (
        _static_cardinality(metadata_cardinality)
    )
    if observation is not None:
        evidence = _dedupe_evidence(evidence, (_observation_evidence(observation),))
    observation_matches = (
        observation is not None
        and _observation_matches_realization(observation, revision)
    )

    hard_reasons = list(assessment.hard_conflicts)
    assessment_endpoints = {
        canonical_hash(endpoint.logical_identity_payload())
        for endpoint in (assessment.left_endpoint, assessment.right_endpoint)
    }
    realization_endpoints = {
        canonical_hash(endpoint.logical_identity_payload())
        for endpoint in (revision.from_endpoint, revision.to_endpoint)
    }
    if (
        assessment.bridge_fact_key != revision.bridge_fact_key
        or assessment_endpoints != realization_endpoints
    ):
        hard_reasons.append("realization_not_backed_by_assessment")
    if metadata_cardinality is not None and (
        metadata_cardinality.from_key.endpoint.logical_identity_payload()
        != revision.from_endpoint.logical_identity_payload()
        or metadata_cardinality.to_key.endpoint.logical_identity_payload()
        != revision.to_endpoint.logical_identity_payload()
    ):
        hard_reasons.append("metadata_cardinality_not_for_realization")
    if assessment.namespace_verdict is NamespaceVerdict.DIFFERENT:
        hard_reasons.append("different_identifier_namespace")
    if target_nonunique:
        hard_reasons.append("governed_target_key_not_unique")
    if conflicting_observation is not None:
        evidence = _dedupe_evidence(
            evidence, (_observation_evidence(conflicting_observation),))
        if _observation_matches_realization(conflicting_observation, revision):
            hard_reasons.append("current_observed_target_duplicate_or_fanout")
    if observation_matches and observation is not None and (
        observation.right.duplicate_row_count > 0
        or observation.right.null_row_count > 0
        or observation.max_right_matches_per_left_row > 1
    ):
        hard_reasons.append("observed_target_duplicate_or_fanout")
    if hard_reasons:
        return _decision(
            policy=policy,
            safety=SafetyStatus.UNSAFE,
            tier=BridgeDisplayTier.REJECTED,
            cardinality=static_cardinality,
            basis=static_basis,
            static_validated=static_validated,
            reasons=hard_reasons,
            evidence=evidence,
        )

    if (
        not link_available
        or not bindings_current
        or (observation is not None and not source_snapshots_current)
    ):
        stale_reasons = []
        if not link_available:
            stale_reasons.append("identifier_link_not_currently_available")
        if not bindings_current:
            stale_reasons.append("physical_binding_revision_not_current")
        if observation is not None and not source_snapshots_current:
            stale_reasons.append("source_snapshot_not_current")
        return _decision(
            policy=policy,
            safety=SafetyStatus.UNASSESSED,
            tier=BridgeDisplayTier.STALE,
            cardinality=static_cardinality,
            basis=static_basis,
            static_validated=static_validated,
            reasons=stale_reasons,
            evidence=evidence,
        )

    if not _representation_compatible(revision, observation):
        return _decision(
            policy=policy,
            safety=SafetyStatus.UNASSESSED,
            tier=BridgeDisplayTier.DISCOVERED,
            cardinality=static_cardinality,
            basis=static_basis,
            static_validated=static_validated,
            reasons=["representation_not_deterministically_compatible"],
            evidence=evidence,
        )

    if assessment.namespace_verdict is NamespaceVerdict.UNKNOWN:
        return _decision(
            policy=policy,
            safety=SafetyStatus.UNASSESSED,
            tier=BridgeDisplayTier.DISCOVERED,
            cardinality=static_cardinality,
            basis=static_basis,
            static_validated=static_validated,
            reasons=["identifier_namespace_not_supported"],
            evidence=evidence,
        )

    if observation is None:
        static_namespace_supported = (
            assessment.namespace_verdict is NamespaceVerdict.SAME
            and any(
                ref.kind in {
                    EvidenceKind.SOURCE_CONSTRAINT,
                    EvidenceKind.GOVERNED_FACT,
                }
                for ref in assessment.evidence_refs
            )
        )
        missing_reasons = (
            ["exact_relationship_observation_required"]
            if static_validated and static_namespace_supported
            else ["source_or_governed_namespace_evidence_required"]
            if static_validated
            else ["target_uniqueness_not_proved"]
        )
        tier = (
            BridgeDisplayTier.STRONG_PROPOSED
            if static_validated and static_namespace_supported
            else BridgeDisplayTier.DISCOVERED
        )
        return _decision(
            policy=policy,
            safety=SafetyStatus.UNASSESSED,
            tier=tier,
            cardinality=static_cardinality,
            basis=static_basis,
            static_validated=static_validated,
            reasons=missing_reasons,
            evidence=evidence,
        )

    reasons: list[str] = []
    if observation.realization_revision_id != revision.realization_revision_id:
        reasons.append("observation_realization_revision_mismatch")
    if observation.scope_id != revision.applicability_scope.scope_id:
        reasons.append("observation_scope_mismatch")
    if (
        observation.left.binding_revision_id
        != revision.from_endpoint.binding_revision_id
        or observation.right.binding_revision_id
        != revision.to_endpoint.binding_revision_id
    ):
        reasons.append("observation_binding_revision_mismatch")
    if not observation_matches:
        reasons.append("observation_tuple_mismatch")
    if not observation.complete:
        reasons.append("observation_incomplete")
    if observation.method != "exact":
        reasons.append("observation_not_exact")
    if observation.row_coverage is not RowCoverage.FULL:
        reasons.append("observation_not_full_coverage")
    if now - observation.observed_at > policy.maximum_evidence_age:
        reasons.append("observation_too_old")
    if min(observation.left.row_count, observation.right.row_count) < (
        policy.minimum_observed_rows_per_endpoint
    ):
        reasons.append("observation_below_minimum_rows")
    if min(
        len(observation.left.partitions_read),
        len(observation.right.partitions_read),
    ) < policy.minimum_observed_partitions_per_endpoint:
        reasons.append("observation_below_minimum_partitions")
    if (
        revision.applicability_scope.partition_scope_ref is None
        and (observation.left.partitions_read or observation.right.partitions_read)
    ):
        reasons.append("partition_scoped_evidence_cannot_validate_unrestricted_realization")
    if (
        revision.applicability_scope.partition_scope_ref is not None
        and (not observation.left.partitions_read or not observation.right.partitions_read)
    ):
        reasons.append("realization_scope_requires_partition_evidence")
    expected_normalizations = (
        ("identity_v1",) * len(revision.column_pairs)
        if expected_normalization_ids is None
        else tuple(expected_normalization_ids)
    )
    if tuple(observation.normalization_ids) != expected_normalizations:
        reasons.append("normalization_scope_mismatch")
    if any(
        item not in policy.allowed_normalization_ids
        for item in observation.normalization_ids
    ):
        # No model-proposed free-form transform can pass this closed vocabulary.
        reasons.append("normalization_not_admitted")
    if tuple(observation.predicate_ids) != tuple(expected_predicate_ids):
        reasons.append("predicate_scope_mismatch")
    if revision.predicates and not expected_predicate_ids:
        reasons.append("realization_predicate_evidence_mapping_required")
    if any(item not in policy.allowed_predicate_ids for item in observation.predicate_ids):
        reasons.append("predicate_not_admitted")

    left_null_ratio = _ratio(
        observation.left.null_row_count, observation.left.row_count)
    right_null_ratio = _ratio(
        observation.right.null_row_count, observation.right.row_count)
    if left_null_ratio > policy.maximum_left_null_ratio:
        reasons.append("left_null_ratio_exceeds_policy")
    if right_null_ratio > policy.maximum_right_null_ratio:
        reasons.append("right_null_ratio_exceeds_policy")
    if observation.left_row_amplification_ratio > policy.maximum_left_row_amplification:
        reasons.append("left_row_amplification_exceeds_policy")

    threshold = policy.threshold_for(assessment.governed_population_relation)
    if threshold is None:
        reasons.append("governed_populations_are_disjoint")
    else:
        # The advisory LLM population_hypothesis is intentionally never consulted here.
        if (
            observation.left_distinct_containment_ratio
            < threshold.minimum_left_distinct_containment
        ):
            reasons.append("left_containment_below_policy")
        if (
            observation.right_distinct_containment_ratio
            < threshold.minimum_right_distinct_containment
        ):
            reasons.append("right_containment_below_policy")
        if observation.left_row_orphan_ratio > threshold.maximum_left_row_orphan_ratio:
            reasons.append("left_orphan_ratio_exceeds_policy")
        if observation.right_row_orphan_ratio > threshold.maximum_right_row_orphan_ratio:
            reasons.append("right_orphan_ratio_exceeds_policy")

    cardinality = DirectionalCardinalityVerdictV1(
        Cardinality(observation.observed_cardinality))
    if cardinality.value in {Cardinality.ONE_TO_MANY, Cardinality.MANY_TO_MANY}:
        reasons.append("directional_join_fanout")
    if reasons:
        return _decision(
            policy=policy,
            safety=SafetyStatus.UNASSESSED,
            tier=(
                BridgeDisplayTier.STRONG_PROPOSED
                if static_validated
                else BridgeDisplayTier.DISCOVERED
            ),
            cardinality=cardinality,
            basis=CardinalityBasis.EXACT_PROFILE,
            static_validated=static_validated,
            reasons=reasons,
            evidence=evidence,
        )
    return _decision(
        policy=policy,
        safety=SafetyStatus.DETERMINISTICALLY_VALIDATED,
        tier=BridgeDisplayTier.STRONG_PROPOSED,
        cardinality=cardinality,
        basis=CardinalityBasis.EXACT_PROFILE,
        static_validated=static_validated,
        reasons=["deterministic_relationship_policy_satisfied"],
        evidence=evidence,
    )


def admitted_realization_revision(
    revision: BridgeJoinRealizationRevisionV1,
    decision: BridgeAdmissionDecisionV1,
) -> BridgeJoinRealizationRevisionV1:
    """Create the immutable conclusion revision selected by one admission decision."""
    return replace(
        revision,
        cardinality=decision.cardinality,
        cardinality_basis=decision.cardinality_basis,
        evidence_refs=decision.evidence_refs,
        admission_policy_version=decision.policy_version,
    )

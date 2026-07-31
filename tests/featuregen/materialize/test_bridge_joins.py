from __future__ import annotations

from dataclasses import replace

from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.joins import (
    CrossCatalogJoinStepV1,
    JoinPlan,
    PhysicalIdentity,
    plan_cross_catalog_join,
)
from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import CurrentBridgeRealizationV1
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality


def _current(
    cardinality: Cardinality | None = Cardinality.MANY_TO_ONE,
    *,
    reviewed: bool = False,
) -> CurrentBridgeRealizationV1:
    left, right = _executable_pair()
    revision = replace(
        _realization(left, right),
        applicability_scope=RealizationApplicabilityScopeV1(
            scope_id="production-customer",
            execution_tier=ExecutionTier.PRODUCTION,
            purposes=("feature_generation",),
            environment="pilot",
        ),
        cardinality=DirectionalCardinalityVerdictV1(cardinality),
    )
    current = BridgeRealizationCurrentV1(
        revision.realization_id,
        revision.realization_revision_id,
        SafetyStatus.DETERMINISTICALLY_VALIDATED,
        (
            LinkReviewStatus.HUMAN_VERIFIED
            if reviewed
            else LinkReviewStatus.UNREVIEWED
        ),
        RealizationLifecycle.ACTIVE,
        1,
    )
    return CurrentBridgeRealizationV1(revision, current, ())


def _identities(realization: CurrentBridgeRealizationV1):
    left = realization.revision.from_endpoint.physical_binding
    right = realization.revision.to_endpoint.physical_binding
    assert left is not None and right is not None
    return (
        PhysicalIdentity(
            left.identity.catalog_source,
            left.identity.schema,
            left.identity.table,
        ),
        PhysicalIdentity(
            right.identity.catalog_source,
            right.identity.schema,
            right.identity.table,
        ),
    )


def test_directional_many_to_one_realization_builds_cross_catalog_plan() -> None:
    realization = _current()
    left, right = _identities(realization)
    result = plan_cross_catalog_join(
        realization,
        from_identity=left,
        to_identity=right,
        roles=("feature_reader",),
    )
    assert isinstance(result, JoinPlan)
    assert result.outcome_kind == "DIRECTIONAL_REALIZATION_OPERATIONAL"
    assert result.fans_out is False
    assert len(result.steps) == 1
    step = result.steps[0]
    assert isinstance(step, CrossCatalogJoinStepV1)
    assert step.from_catalog_source == "cib"
    assert step.to_catalog_source == "ftr"
    assert step.cardinality == "N:1"
    assert step.realization_revision_id == realization.revision.realization_revision_id


def test_unknown_directional_cardinality_refuses() -> None:
    realization = _current(None)
    left, right = _identities(realization)
    result = plan_cross_catalog_join(
        realization, from_identity=left, to_identity=right)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_reverse_fanout_direction_refuses() -> None:
    realization = _current(Cardinality.ONE_TO_MANY)
    left, right = _identities(realization)
    result = plan_cross_catalog_join(
        realization, from_identity=left, to_identity=right)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_human_review_does_not_change_execution_eligibility() -> None:
    unreviewed = _current(reviewed=False)
    reviewed = _current(reviewed=True)
    left, right = _identities(unreviewed)
    first = plan_cross_catalog_join(
        unreviewed, from_identity=left, to_identity=right)
    second = plan_cross_catalog_join(
        reviewed, from_identity=left, to_identity=right)
    assert isinstance(first, JoinPlan)
    assert isinstance(second, JoinPlan)
    assert first.steps == second.steps

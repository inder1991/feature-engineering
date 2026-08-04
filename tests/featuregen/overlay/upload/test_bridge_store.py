from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)
from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

from featuregen.contracts.envelopes import Command
from featuregen.data_agent.physical import record_binding_revision
from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.data_agent.store import record_relationship_observation
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.ir import (
    AuthorizedCompilation,
    BridgeExecutionAuthorization,
    _executable_realizations_for_tier,
    authorize_execution_realizations,
)
from featuregen.materialize.joins import (
    CrossCatalogJoinStepV1,
    PhysicalIdentity,
    plan_cross_catalog_join,
)
from featuregen.overlay.bridge_realization_commands import (
    decide_bridge_realization_safety,
    review_bridge_realization,
)
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    LinkReviewStatus,
    read_overlay_identifier_link_state,
)
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    BridgeStoreConflict,
    assessment_from_json,
    assessment_to_json,
    bridge_dependency_snapshot_id,
    demote_realizations_for_bridge,
    demote_realizations_for_dependency,
    executable_bridge_realizations,
    load_current_bridge_realizations,
    load_current_candidate_assessments,
    record_candidate_assessment,
    record_realization_revision,
    revalidate_bridge_realization,
)
from featuregen.overlay.upload.contract.invalidation import (
    _MISSING,
    _catalog_state_signature,
    bridge_realization_marker,
)


def _assessment(db):
    _two_catalog_customer(db)
    assessment = derive_bridge_candidates(db)[0].assessment
    assert assessment is not None
    return replace(assessment, bridge_fact_key="bridge-fact-key")


def test_assessment_json_round_trip_preserves_revision_identity(db) -> None:
    assessment = _assessment(db)
    restored = assessment_from_json(assessment_to_json(assessment))
    assert restored == assessment
    assert restored.candidate_id == assessment.candidate_id
    assert restored.candidate_revision_id == assessment.candidate_revision_id


def test_candidate_revision_and_current_pointer_are_separate(db) -> None:
    first = _assessment(db)
    current1 = record_candidate_assessment(db, first, expected_pointer_version=0)
    second = replace(
        first,
        explanation_codes=(*first.explanation_codes, "stronger_type_evidence"),
    )
    current2 = record_candidate_assessment(
        db, second, expected_pointer_version=current1.pointer_version)

    assert current2.pointer_version == 2
    assert current2.candidate_revision_id == second.candidate_revision_id
    assert db.execute(
        "SELECT count(*) FROM governed_candidate_revision WHERE candidate_id = %s",
        (first.candidate_id,),
    ).fetchone()[0] == 2
    assert load_current_candidate_assessments(db) == (second,)


def test_older_completion_cannot_overwrite_newer_current_pointer(db) -> None:
    first = _assessment(db)
    current1 = record_candidate_assessment(db, first, expected_pointer_version=0)
    second = replace(first, explanation_codes=("second",))
    record_candidate_assessment(
        db, second, expected_pointer_version=current1.pointer_version)
    late_older_work = replace(first, explanation_codes=("late_older_work",))

    with pytest.raises(BridgeStoreConflict):
        record_candidate_assessment(
            db,
            late_older_work,
            expected_pointer_version=current1.pointer_version,
        )

    current = db.execute(
        "SELECT candidate_revision_id, pointer_version "
        "FROM governed_candidate_current WHERE candidate_id = %s",
        (first.candidate_id,),
    ).fetchone()
    assert current == (second.candidate_revision_id, 2)


def test_re_recording_same_revision_is_idempotent(db) -> None:
    assessment = _assessment(db)
    first = record_candidate_assessment(db, assessment, expected_pointer_version=0)
    second = record_candidate_assessment(
        db, assessment, expected_pointer_version=first.pointer_version)
    assert second == first
    assert db.execute(
        "SELECT count(*) FROM governed_candidate_revision WHERE candidate_id = %s",
        (assessment.candidate_id,),
    ).fetchone()[0] == 1


def _stored_realization(db, *, safety=SafetyStatus.DETERMINISTICALLY_VALIDATED):
    left, right = _executable_pair()
    revision = _realization(left, right)
    current = BridgeRealizationCurrentV1(
        revision.realization_id,
        revision.realization_revision_id,
        safety,
        LinkReviewStatus.UNREVIEWED,
        RealizationLifecycle.ACTIVE,
        1,
    )
    record_realization_revision(
        db,
        revision,
        current,
        dependencies=(
            BridgeDependencyRefV1(
                "grain_fact", "grain-fact-cib", "grain-revision-1"),
            BridgeDependencyRefV1(
                "bridge_fact", revision.bridge_fact_key, "bridge-head-1"),
        ),
    )
    return revision


def _stored_production_realization(
    db, *, execution_tier: ExecutionTier = ExecutionTier.PRODUCTION
):
    """A fully backed, deterministically executable realization, stored.

    ``execution_tier`` is the realization's APPLICABILITY scope — whether this join is approved for
    production or only for sandbox data. It is a parameter so that the sandbox case is reachable at
    all; every existing caller omits it and gets the production scope it always got.
    """
    left, right = _executable_pair()
    scope = RealizationApplicabilityScopeV1(
        scope_id=f"{execution_tier.value}-customer-link",
        execution_tier=execution_tier,
        purposes=("feature_generation",),
        environment="pilot",
    )
    candidate_revision = replace(
        _realization(left, right),
        applicability_scope=scope,
        cardinality_basis=CardinalityBasis.EXACT_PROFILE,
        evidence_refs=(),
    )
    assert left.physical_binding is not None
    assert right.physical_binding is not None
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    govern_bridge_fact(
        db,
        candidate_revision.bridge_fact_key,
        entity="customer",
        left_source="cib",
        left_ref="public.customers.customer_id",
        right_source="ftr",
        right_ref="public.transactions.customer_id",
        status="DRAFT",
    )
    link_head = read_overlay_identifier_link_state(
        db, candidate_revision.bridge_fact_key).overlay_head_event_id
    assert link_head is not None
    candidate_dependencies = (
        BridgeDependencyRefV1(
            "bridge_fact", candidate_revision.bridge_fact_key, link_head),
        BridgeDependencyRefV1(
            "physical_binding",
            left.physical_binding.binding_id,
            left.physical_binding.binding_revision_id,
        ),
        BridgeDependencyRefV1(
            "physical_binding",
            right.physical_binding.binding_id,
            right.physical_binding.binding_revision_id,
        ),
    )
    candidate_revision = replace(
        candidate_revision,
        dependency_snapshot_id=bridge_dependency_snapshot_id(candidate_dependencies),
    )
    record_realization_revision(
        db,
        candidate_revision,
        BridgeRealizationCurrentV1(
            candidate_revision.realization_id,
            candidate_revision.realization_revision_id,
            SafetyStatus.UNASSESSED,
            LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE,
            1,
        ),
        dependencies=candidate_dependencies,
    )
    observation = RelationshipObservationV2(
        realization_revision_id=candidate_revision.realization_revision_id,
        plan_hash="exact-plan-hash",
        scope_id=scope.scope_id,
        left=EndpointTupleObservationV2(
            left.physical_binding.identity.table_id,
            left.binding_revision_id or "",
            left.physical_binding.content_hash,
            ("customer_id",),
            10,
            10,
            10,
            0,
            0,
            1,
        ),
        right=EndpointTupleObservationV2(
            right.physical_binding.identity.table_id,
            right.binding_revision_id or "",
            right.physical_binding.content_hash,
            ("customer_id",),
            10,
            10,
            10,
            0,
            0,
            1,
        ),
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
        predicate_ids=(),
        left_source_snapshot_id="left-snapshot",
        right_source_snapshot_id="right-snapshot",
        snapshot_or_as_of="2026-07-30",
        execution_principal="profile-service",
        method="exact",
        row_coverage=RowCoverage.FULL,
        complete=True,
        observed_at=datetime(2026, 7, 30, 10, tzinfo=UTC),
    )
    assert record_relationship_observation(
        db, observation, expected_pointer_version=0).became_current
    admitted_dependencies = (
        *candidate_dependencies,
        BridgeDependencyRefV1(
            "relationship_observation",
            observation.observation_revision_id,
            observation.plan_hash,
        ),
    )
    admitted_revision = replace(
        candidate_revision,
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
        admitted_revision,
        BridgeRealizationCurrentV1(
            admitted_revision.realization_id,
            admitted_revision.realization_revision_id,
            SafetyStatus.DETERMINISTICALLY_VALIDATED,
            LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE,
            2,
        ),
        dependencies=admitted_dependencies,
        expected_pointer_version=1,
    )
    return admitted_revision


def test_typed_realization_reader_reproduces_stored_identities(db) -> None:
    expected = _stored_production_realization(db)
    loaded = load_current_bridge_realizations(db)
    assert len(loaded) == 1
    assert loaded[0].revision == expected
    assert loaded[0].revision.realization_revision_id == expected.realization_revision_id


def test_unreviewed_link_is_production_executable_from_deterministic_realization(db) -> None:
    expected = _stored_production_realization(db)
    executable = executable_bridge_realizations(
        db,
        purpose="feature_generation",
        environment="pilot",
    )
    assert tuple(item.revision.realization_revision_id for item in executable) == (
        expected.realization_revision_id,
    )
    assert executable[0].current.review_status is LinkReviewStatus.UNREVIEWED


def test_stale_link_fails_final_realization_revalidation(db) -> None:
    expected = _stored_production_realization(db)
    govern_bridge_fact(
        db,
        expected.bridge_fact_key,
        entity="customer",
        left_source="cib",
        left_ref="public.customers.customer_id",
        right_source="ftr",
        right_ref="public.transactions.customer_id",
        status="STALE",
    )
    loaded = load_current_bridge_realizations(db)[0]
    result = revalidate_bridge_realization(
        db, loaded, purpose="feature_generation", environment="pilot")
    assert not result.executable
    assert "identifier_link_not_available" in result.reason_codes
    assert executable_bridge_realizations(
        db, purpose="feature_generation", environment="pilot") == ()


def test_withdrawn_candidate_fails_final_realization_revalidation(db) -> None:
    assessment = replace(_assessment(db), bridge_fact_key="bridge-fact-1")
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    _stored_production_realization(db)
    db.execute(
        "UPDATE governed_candidate_current SET lifecycle='withdrawn' "
        "WHERE candidate_id=%s",
        (assessment.candidate_id,),
    )

    loaded = load_current_bridge_realizations(db)[0]
    result = revalidate_bridge_realization(
        db, loaded, purpose="feature_generation", environment="pilot")

    assert not result.executable
    assert "identifier_link_candidate_withdrawn" in result.reason_codes
    assert executable_bridge_realizations(
        db, purpose="feature_generation", environment="pilot") == ()


def test_stale_link_between_compilation_and_run_preparation_refuses(db) -> None:
    revision = _stored_production_realization(db)
    current = load_current_bridge_realizations(db)[0]
    left_binding = revision.from_endpoint.physical_binding
    right_binding = revision.to_endpoint.physical_binding
    assert left_binding is not None and right_binding is not None
    planned = plan_cross_catalog_join(
        current,
        from_identity=PhysicalIdentity(
            left_binding.identity.catalog_source,
            left_binding.identity.schema,
            left_binding.identity.table,
        ),
        to_identity=PhysicalIdentity(
            right_binding.identity.catalog_source,
            right_binding.identity.schema,
            right_binding.identity.table,
        ),
    )
    assert not isinstance(planned, MaterializationRefused)
    assert isinstance(planned.steps[0], CrossCatalogJoinStepV1)

    # The final authorizer deliberately consumes only Gate 2's token fields that affect execution.
    # A tiny carrier keeps this test about the compile/run race rather than rebuilding a formula.
    fake_ir = SimpleNamespace(
        expressions=(SimpleNamespace(join_plan=planned),),
        identity_payload=lambda: {"feature": "cross-catalog-test"},
    )
    authorized = AuthorizedCompilation(
        irs=(fake_ir,),  # type: ignore[arg-type]
        spine=None,  # type: ignore[arg-type]
        authorized_refs=(),
        roles_used=(),
    )
    before = authorize_execution_realizations(
        db, authorized, environment_id="pilot")
    assert isinstance(before, BridgeExecutionAuthorization)

    govern_bridge_fact(
        db,
        revision.bridge_fact_key,
        entity="customer",
        left_source="cib",
        left_ref="public.customers.customer_id",
        right_source="ftr",
        right_ref="public.transactions.customer_id",
        status="STALE",
    )
    after = authorize_execution_realizations(
        db, authorized, environment_id="pilot")
    assert isinstance(after, MaterializationRefused)
    assert after.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_contract_dependency_tracks_exact_realization_safety_not_human_review(
    db,
    human_actor,
) -> None:
    expected = _stored_production_realization(db)
    marker = bridge_realization_marker(
        expected.realization_revision_id,
        expected.dependency_snapshot_id,
    )
    before = _catalog_state_signature(db, "cib", marker)
    assert before != _MISSING

    reviewed = review_bridge_realization(
        db,
        Command(
            "review_bridge_realization",
            "bridge_realization",
            expected.realization_id,
            {
                "realization_revision_id": expected.realization_revision_id,
                "expected_pointer_version": 2,
                "approved": True,
            },
            human_actor,
            "review-exact-realization",
        ),
    )
    assert reviewed.accepted
    assert _catalog_state_signature(db, "cib", marker) == before

    govern_bridge_fact(
        db,
        expected.bridge_fact_key,
        entity="customer",
        left_source="cib",
        left_ref="public.customers.customer_id",
        right_source="ftr",
        right_ref="public.transactions.customer_id",
        status="STALE",
    )
    assert _catalog_state_signature(db, "cib", marker) == _MISSING


def test_bridge_lifecycle_demotion_withdraws_current_realization(db) -> None:
    revision = _stored_realization(db)
    assert demote_realizations_for_bridge(
        db, revision.bridge_fact_key, lifecycle="stale") == 1
    assert db.execute(
        "SELECT lifecycle, pointer_version FROM bridge_join_realization_current "
        "WHERE realization_id = %s",
        (revision.realization_id,),
    ).fetchone() == ("stale", 2)


def test_dependency_change_withdraws_current_realization(db) -> None:
    revision = _stored_realization(db)
    assert demote_realizations_for_dependency(
        db, "grain_fact", "grain-fact-cib") == 1
    assert db.execute(
        "SELECT lifecycle FROM bridge_join_realization_current "
        "WHERE realization_id = %s",
        (revision.realization_id,),
    ).fetchone()[0] == "stale"


def test_safety_and_human_review_are_independent_decision_commands(
    db,
    service_actor,
    human_actor,
) -> None:
    revision = _stored_realization(db, safety=SafetyStatus.UNASSESSED)
    safety = decide_bridge_realization_safety(
        db,
        Command(
            "decide_bridge_realization_safety",
            "bridge_realization",
            revision.realization_id,
            {
                "realization_revision_id": revision.realization_revision_id,
                "expected_pointer_version": 1,
                "safe": True,
                "evidence": {"probe_revision": "probe-1"},
            },
            service_actor,
            "safety-decision-1",
        ),
    )
    assert safety.accepted
    assert db.execute(
        "SELECT safety_status, review_status, pointer_version "
        "FROM bridge_join_realization_current WHERE realization_id=%s",
        (revision.realization_id,),
    ).fetchone() == ("deterministically_validated", "unreviewed", 2)

    review = review_bridge_realization(
        db,
        Command(
            "review_bridge_realization",
            "bridge_realization",
            revision.realization_id,
            {
                "realization_revision_id": revision.realization_revision_id,
                "expected_pointer_version": 2,
                "approved": True,
                "evidence": {"note": "reviewed"},
            },
            human_actor,
            "review-decision-1",
        ),
    )
    assert review.accepted
    assert db.execute(
        "SELECT safety_status, review_status, pointer_version "
        "FROM bridge_join_realization_current WHERE realization_id=%s",
        (revision.realization_id,),
    ).fetchone() == ("deterministically_validated", "human_verified", 3)
    assert db.execute(
        "SELECT decision_axis, decision_value "
        "FROM bridge_realization_decision_event ORDER BY occurred_at, decision_event_id"
    ).fetchall() == [
        ("deterministic_safety", "deterministically_validated"),
        ("human_review", "human_verified"),
    ]


def test_realization_decision_is_cas_bound_to_current_revision(
    db,
    service_actor,
) -> None:
    revision = _stored_realization(db, safety=SafetyStatus.UNASSESSED)
    first = decide_bridge_realization_safety(
        db,
        Command(
            "decide_bridge_realization_safety",
            "bridge_realization",
            revision.realization_id,
            {
                "realization_revision_id": revision.realization_revision_id,
                "expected_pointer_version": 1,
                "safe": True,
            },
            service_actor,
            "safety-current",
        ),
    )
    assert first.accepted
    stale = decide_bridge_realization_safety(
        db,
        Command(
            "decide_bridge_realization_safety",
            "bridge_realization",
            revision.realization_id,
            {
                "realization_revision_id": revision.realization_revision_id,
                "expected_pointer_version": 1,
                "safe": False,
            },
            service_actor,
            "safety-stale",
        ),
    )
    assert not stale.accepted
    assert "stale" in (stale.denied_reason or "")


# ── the realization APPLICABILITY tier reaches materialize's two entry points (Phase G, P2) ──────
#
# `ExecutionTier` scopes whether a directional realization is approved for SANDBOX or PRODUCTION
# data. It is NOT a run execution tier: plan §3.4 decided Phase G introduces none, because the
# sandbox namespace is baked into `sandbox_execution_hash` and a production namespace would fork
# execution identity. Nothing below names a namespace or an execution hash — only which
# realizations materialize can SEE, and authorize for a run.
#
# Before the fix both entry points asserted PRODUCTION by omission: `compile_ir` read through
# `executable_bridge_realizations`, which pins the tier internally, and
# `authorize_execution_realizations` let `revalidate_bridge_realization`'s PRODUCTION default stand.
# A SANDBOX-scoped realization was therefore unusable, with no parameter anywhere to change it.


def test_a_SANDBOX_scoped_realization_is_INVISIBLE_at_the_production_tier(db) -> None:
    """The behaviour that is CORRECT and has to survive the fix."""
    _stored_production_realization(db, execution_tier=ExecutionTier.SANDBOX)
    assert executable_bridge_realizations(
        db, purpose="feature_generation", environment="pilot") == ()
    assert _executable_realizations_for_tier(
        db, environment_id="pilot", execution_tier=ExecutionTier.PRODUCTION) == ()


def test_a_SANDBOX_scoped_realization_is_READABLE_when_the_caller_asks_for_SANDBOX(db) -> None:
    """The defect itself: with no parameter, this realization could never be reached at all."""
    expected = _stored_production_realization(db, execution_tier=ExecutionTier.SANDBOX)
    reachable = _executable_realizations_for_tier(
        db, environment_id="pilot", execution_tier=ExecutionTier.SANDBOX)
    assert tuple(item.revision.realization_revision_id for item in reachable) == (
        expected.realization_revision_id,)


def test_the_PRODUCTION_tier_reads_EXACTLY_what_the_typed_production_reader_reads(db) -> None:
    """The production path is unchanged, proved against the reader itself rather than a remembered
    list — `executable_bridge_realizations` remains the production path."""
    expected = _stored_production_realization(db)
    through_the_tier = _executable_realizations_for_tier(
        db, environment_id="pilot", execution_tier=ExecutionTier.PRODUCTION)
    assert through_the_tier == executable_bridge_realizations(
        db, purpose="feature_generation", environment="pilot")
    assert tuple(item.revision.realization_revision_id for item in through_the_tier) == (
        expected.realization_revision_id,)


def test_a_PRODUCTION_scoped_realization_is_not_borrowed_by_a_SANDBOX_caller(db) -> None:
    """Passing the tier through must not become "any tier will do" in either direction."""
    _stored_production_realization(db)
    assert _executable_realizations_for_tier(
        db, environment_id="pilot", execution_tier=ExecutionTier.SANDBOX) == ()


def _cross_catalog_authorized(db, revision) -> AuthorizedCompilation:
    """Gate 2's token carrying one real cross-catalog step against ``revision``.

    The same tiny carrier ``test_stale_link_between_compilation_and_run_preparation_refuses`` uses:
    the final authorizer consumes only the token fields that affect execution, so this keeps the
    test about the tier rather than about rebuilding a formula.
    """
    current = load_current_bridge_realizations(db)[0]
    left = revision.from_endpoint.physical_binding
    right = revision.to_endpoint.physical_binding
    assert left is not None and right is not None
    planned = plan_cross_catalog_join(
        current,
        from_identity=PhysicalIdentity(
            left.identity.catalog_source, left.identity.schema, left.identity.table),
        to_identity=PhysicalIdentity(
            right.identity.catalog_source, right.identity.schema, right.identity.table))
    assert not isinstance(planned, MaterializationRefused)
    assert isinstance(planned.steps[0], CrossCatalogJoinStepV1)
    fake_ir = SimpleNamespace(
        expressions=(SimpleNamespace(join_plan=planned),),
        identity_payload=lambda: {"feature": "cross-catalog-tier-test"})
    return AuthorizedCompilation(
        irs=(fake_ir,),      # type: ignore[arg-type]
        spine=None,          # type: ignore[arg-type]
        authorized_refs=(), roles_used=())


def test_final_revalidation_refuses_a_SANDBOX_realization_at_the_PRODUCTION_default(db) -> None:
    revision = _stored_production_realization(db, execution_tier=ExecutionTier.SANDBOX)
    authorized = _cross_catalog_authorized(db, revision)
    refused = authorize_execution_realizations(db, authorized, environment_id="pilot")
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN
    assert "realization_execution_tier_mismatch" in refused.detail


def test_final_revalidation_AUTHORIZES_the_same_realization_at_the_SANDBOX_tier(db) -> None:
    revision = _stored_production_realization(db, execution_tier=ExecutionTier.SANDBOX)
    authorized = _cross_catalog_authorized(db, revision)
    granted = authorize_execution_realizations(
        db, authorized, environment_id="pilot", execution_tier=ExecutionTier.SANDBOX)
    assert isinstance(granted, BridgeExecutionAuthorization)
    assert granted.realization_dependencies == (
        (revision.realization_revision_id, revision.dependency_snapshot_id),)


def test_final_revalidation_refuses_a_PRODUCTION_realization_asked_for_as_SANDBOX(db) -> None:
    revision = _stored_production_realization(db)
    authorized = _cross_catalog_authorized(db, revision)
    assert isinstance(
        authorize_execution_realizations(db, authorized, environment_id="pilot"),
        BridgeExecutionAuthorization)
    refused = authorize_execution_realizations(
        db, authorized, environment_id="pilot", execution_tier=ExecutionTier.SANDBOX)
    assert isinstance(refused, MaterializationRefused)
    assert "realization_execution_tier_mismatch" in refused.detail

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.data_agent.physical import record_binding_revision
from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationV2,
    RowCoverage,
    observation_from_json,
    observation_to_json,
)
from featuregen.data_agent.store import (
    current_relationship_observation,
    latest_relationship_conflict,
    record_relationship_observation,
)
from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    demote_realizations_for_bridge,
    record_realization_revision,
)

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _seed_realization(db):
    left, right = _executable_pair()
    revision = _realization(left, right)
    assert left.physical_binding is not None
    assert right.physical_binding is not None
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    record_realization_revision(
        db,
        revision,
        BridgeRealizationCurrentV1(
            revision.realization_id,
            revision.realization_revision_id,
            SafetyStatus.UNASSESSED,
            LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE,
            1,
        ),
        dependencies=(
            BridgeDependencyRefV1(
                "bridge_fact", revision.bridge_fact_key, "bridge-head-1"),
        ),
    )
    return revision, left.physical_binding, right.physical_binding


def _observation(db) -> RelationshipObservationV2:
    revision, left_binding, right_binding = _seed_realization(db)
    left = EndpointTupleObservationV2(
        left_binding.identity.table_id,
        left_binding.binding_revision_id,
        left_binding.content_hash,
        ("customer_id",),
        10,
        10,
        3,
        2,
        7,
        6,
    )
    right = EndpointTupleObservationV2(
        right_binding.identity.table_id,
        right_binding.binding_revision_id,
        right_binding.content_hash,
        ("customer_id",),
        3,
        3,
        3,
        0,
        0,
        1,
    )
    return RelationshipObservationV2(
        realization_revision_id=revision.realization_revision_id,
        plan_hash="plan-hash-1",
        scope_id=revision.applicability_scope.scope_id,
        left=left,
        right=right,
        matched_left_distinct=3,
        unmatched_left_distinct=0,
        matched_right_distinct=3,
        unmatched_right_distinct=0,
        left_orphan_rows=0,
        right_orphan_rows=0,
        joined_row_count=10,
        max_right_matches_per_left_row=1,
        max_left_matches_per_right_row=6,
        normalization_ids=("identity_v1",),
        predicate_ids=(),
        left_source_snapshot_id="left-snapshot-1",
        right_source_snapshot_id="right-snapshot-1",
        snapshot_or_as_of="2026-07-30",
        execution_principal="profile-service",
        method="exact",
        row_coverage=RowCoverage.FULL,
        complete=True,
        observed_at=NOW,
    )


def test_relationship_observation_json_round_trip_preserves_identity(db) -> None:
    observation = _observation(db)
    restored = observation_from_json(observation_to_json(observation))
    assert restored == observation
    assert restored.observation_revision_id == observation.observation_revision_id


def test_complete_exact_observation_becomes_current(db) -> None:
    observation = _observation(db)
    current = record_relationship_observation(
        db, observation, expected_pointer_version=0)
    assert current.became_current and current.pointer_version == 1
    assert db.execute(
        "SELECT producer, strength, left_source_snapshot_id, right_source_snapshot_id "
        "FROM relationship_observation_revision WHERE observation_revision_id=%s",
        (observation.observation_revision_id,),
    ).fetchone() == (
        "profiler",
        "supported",
        "left-snapshot-1",
        "right-snapshot-1",
    )
    assert current_relationship_observation(
        db, observation.current_scope_key) == observation


def test_newer_partial_conflict_is_history_but_does_not_displace_exact(db) -> None:
    exact = _observation(db)
    record_relationship_observation(db, exact, expected_pointer_version=0)
    partial = replace(
        exact,
        right=replace(
            exact.right,
            row_count=4,
            non_null_row_count=4,
            distinct_tuple_count=3,
            duplicate_row_count=1,
            max_rows_per_tuple=2,
        ),
        joined_row_count=16,
        max_right_matches_per_left_row=2,
        complete=False,
        row_coverage=RowCoverage.PARTIAL,
        observed_at=NOW + timedelta(hours=1),
        failures=("sample stopped early",),
    )
    result = record_relationship_observation(
        db, partial, expected_pointer_version=1)
    assert not result.became_current
    assert result.reason == "observed_target_duplicate_or_fanout"
    assert db.execute(
        "SELECT observation_revision_id FROM relationship_observation_current "
        "WHERE current_scope_key=%s",
        (exact.current_scope_key,),
    ).fetchone()[0] == exact.observation_revision_id
    assert current_relationship_observation(
        db, exact.current_scope_key) is None
    assert latest_relationship_conflict(
        db, exact.realization_revision_id) == partial


def test_wrong_tuple_cannot_demote_the_named_realization(db) -> None:
    observation = _observation(db)
    wrong_tuple_with_fanout = replace(
        observation,
        left=replace(observation.left, columns=("wrong_customer_id",)),
        right=replace(
            observation.right,
            row_count=4,
            non_null_row_count=4,
            distinct_tuple_count=3,
            duplicate_tuple_count=1,
            duplicate_row_count=1,
            max_rows_per_tuple=2,
        ),
        max_right_matches_per_left_row=2,
    )
    result = record_relationship_observation(
        db, wrong_tuple_with_fanout, expected_pointer_version=0)
    assert not result.became_current
    assert result.reason == "observation_not_for_realization"
    assert db.execute(
        "SELECT lifecycle FROM bridge_join_realization_current "
        "WHERE realization_revision_id=%s",
        (observation.realization_revision_id,),
    ).fetchone()[0] == "active"
    assert latest_relationship_conflict(
        db, observation.realization_revision_id) is None


def test_observation_for_a_stale_realization_cannot_become_current(db) -> None:
    observation = _observation(db)
    assert demote_realizations_for_bridge(
        db, "bridge-fact-1", lifecycle="stale") == 1
    result = record_relationship_observation(
        db, observation, expected_pointer_version=0)
    assert not result.became_current
    assert result.reason == "realization_revision_not_current"
    assert current_relationship_observation(
        db, observation.current_scope_key) is None


def test_stale_observation_completion_cannot_overwrite_newer_pointer(db) -> None:
    first = _observation(db)
    pointer1 = record_relationship_observation(
        db, first, expected_pointer_version=0)
    second = replace(
        first,
        left_source_snapshot_id="left-snapshot-2",
        right_source_snapshot_id="right-snapshot-2",
        observed_at=NOW + timedelta(hours=2),
    )
    pointer2 = record_relationship_observation(
        db, second, expected_pointer_version=pointer1.pointer_version)
    assert pointer2.became_current and pointer2.pointer_version == 2

    late_completion = replace(
        first,
        left_source_snapshot_id="left-snapshot-late",
        right_source_snapshot_id="right-snapshot-late",
        observed_at=NOW + timedelta(hours=3),
    )
    stale = record_relationship_observation(
        db, late_completion, expected_pointer_version=pointer1.pointer_version)
    assert not stale.became_current
    assert stale.reason == "stale_current_pointer"
    assert current_relationship_observation(
        db, first.current_scope_key) == second

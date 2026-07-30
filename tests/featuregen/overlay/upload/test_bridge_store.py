from __future__ import annotations

from dataclasses import replace

import pytest
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)
from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

from featuregen.contracts.envelopes import Command
from featuregen.overlay.bridge_realization_commands import (
    decide_bridge_realization_safety,
    review_bridge_realization,
)
from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    BridgeStoreConflict,
    assessment_from_json,
    assessment_to_json,
    demote_realizations_for_bridge,
    demote_realizations_for_dependency,
    load_current_candidate_assessments,
    record_candidate_assessment,
    record_realization_revision,
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

"""Learning events — Release 3's first feedback loop.

Until now a question that could not be answered simply failed, and the evidence that the ontology
has a specific actionable gap was discarded. This records it.

The boundary under test throughout: **a technical failure is not ontology evidence.** A Hive
timeout must never become "customer relationship missing", or the ontology fills with candidates
manufactured by an outage.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.data_agent.learning import (
    AnalysisLearningEventV1,
    LearningError,
    LearningStage,
    RequiredAction,
    open_gaps,
    record_gap,
    resolve_gap,
)

_NOW = datetime(2026, 7, 28, tzinfo=UTC)
_SNAPSHOT = "snap-001"


def _gap(**over) -> AnalysisLearningEventV1:
    kw = dict(analysis_request_id="req-1", stage=LearningStage.PLANNING,
              code="DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED",
              subject_refs=("ftr::dpl_eib::customer_segment_history",),
              required_action=RequiredAction.CONFIRM_BUSINESS_POLICY,
              dependency_snapshot_id=_SNAPSHOT)
    kw.update(over)
    return AnalysisLearningEventV1(**kw)


# ── deduplication and demand ─────────────────────────────────────────────────────────────────────

def test_the_same_request_and_gap_do_not_duplicate(db):
    """Re-running an unchanged blocked question is not new information."""
    first = record_gap(db, _gap(), now=_NOW)
    second = record_gap(db, _gap(), now=_NOW)
    assert first == second
    assert len(open_gaps(db)) == 1


def test_two_questions_blocked_by_one_gap_increment_demand_without_duplicating_it(db):
    """ONE thing to decide, TWO questions waiting — which is exactly the prioritisation signal."""
    record_gap(db, _gap(analysis_request_id="req-1"), now=_NOW)
    record_gap(db, _gap(analysis_request_id="req-2"), now=_NOW)
    gaps = open_gaps(db)
    assert len(gaps) == 1
    assert gaps[0].blocked_requests == 2


def test_gaps_are_ordered_by_how_many_questions_they_block(db):
    record_gap(db, _gap(analysis_request_id="req-1"), now=_NOW)
    record_gap(db, _gap(analysis_request_id="req-2"), now=_NOW)
    record_gap(db, _gap(analysis_request_id="req-3", code="POPULATION_AS_OF_UNRESOLVED",
                        required_action=RequiredAction.CONFIRM_POPULATION), now=_NOW)
    gaps = open_gaps(db)
    assert [g.blocked_requests for g in gaps] == [2, 1]


def test_different_subjects_are_different_gaps(db):
    record_gap(db, _gap(subject_refs=("a",)), now=_NOW)
    record_gap(db, _gap(subject_refs=("b",)), now=_NOW)
    assert len(open_gaps(db)) == 2


def test_a_new_dependency_snapshot_causes_re_evaluation(db):
    """The gap may have been resolved since, so it must be recorded again rather than swallowed by
    the deduplication."""
    first = record_gap(db, _gap(dependency_snapshot_id="snap-001"), now=_NOW)
    second = record_gap(db, _gap(dependency_snapshot_id="snap-002"), now=_NOW)
    assert first != second


def test_re_evaluation_does_not_FRAGMENT_the_gap(db):
    """A new snapshot produces a new EVENT but the SAME gap — it is still one thing to decide.

    Found by mutation: putting the snapshot into `gap_key` broke no test, yet it would fragment
    identity so demand never accumulates across snapshots and resolving under one snapshot would
    leave the gap open under another."""
    record_gap(db, _gap(dependency_snapshot_id="snap-001"), now=_NOW)
    record_gap(db, _gap(dependency_snapshot_id="snap-002"), now=_NOW)
    gaps = open_gaps(db)
    assert len(gaps) == 1, "one thing to decide, however many times it was re-evaluated"
    assert gaps[0].blocked_requests == 1, "still ONE question waiting on it"


def test_resolving_after_re_evaluation_clears_the_gap_under_every_snapshot(db):
    """The consequence of the above: one decision closes it, not one decision per snapshot."""
    first = record_gap(db, _gap(dependency_snapshot_id="snap-001"), now=_NOW)
    record_gap(db, _gap(dependency_snapshot_id="snap-002"), now=_NOW)
    resolve_gap(db, first, decision="report_cutoff", actor="owner", now=_NOW)
    assert open_gaps(db) == ()


# ── the boundary: technical failures are not ontology evidence ───────────────────────────────────

@pytest.mark.parametrize("technical", [
    "HIVE_CONNECTION_FAILED", "SPARK_JOB_FAILED", "GENERATED_PROJECT_INVALID", "TIMEOUT",
])
def test_a_technical_failure_cannot_become_an_ontology_gap(technical):
    """THE boundary. An outage must not manufacture ontology candidates."""
    with pytest.raises(LearningError, match="not an actionable ontology gap"):
        _gap(code=technical)


def test_a_gap_with_no_subject_is_refused():
    """It could be neither actioned nor deduplicated."""
    with pytest.raises(LearningError, match="subject"):
        _gap(subject_refs=())


def test_a_successful_plan_records_nothing(db):
    """The control: no gap, no event. Otherwise every run would pollute the loop."""
    assert open_gaps(db) == ()


# ── what a gap must carry to be actionable ───────────────────────────────────────────────────────

def test_an_unverified_relationship_records_its_exact_fact_key(db):
    record_gap(db, _gap(code="RELATIONSHIP_UNVERIFIED",
                        subject_refs=("fact:abc123",),
                        required_action=RequiredAction.CONFIRM_RELATIONSHIP), now=_NOW)
    assert open_gaps(db)[0].subject_refs == ("fact:abc123",)


def test_a_missing_binding_records_the_logical_table_and_the_action(db):
    record_gap(db, _gap(code="PHYSICAL_BINDING_MISSING",
                        subject_refs=("ftr::dpl_eib.tran_repos",),
                        required_action=RequiredAction.BIND_PHYSICAL_SOURCE), now=_NOW)
    gap = open_gaps(db)[0]
    assert gap.subject_refs == ("ftr::dpl_eib.tran_repos",)
    assert gap.required_action == "bind_physical_source"


@pytest.mark.parametrize("code,expected", [
    ("REVERSAL_AS_OF_UNRESOLVED", ("reversed_by_cutoff", "reversed_at_any_time")),
    ("DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED",
     ("report_cutoff", "period_end_per_period", "transaction_event_time", "current_value")),
    ("POPULATION_AS_OF_UNRESOLVED",
     ("membership_at_cutoff", "membership_today", "ever_a_member")),
])
def test_a_closed_choice_gap_records_its_vocabulary(db, code, expected):
    """The reviewer must not have to rediscover what the options were."""
    record_gap(db, _gap(code=code), now=_NOW)
    assert open_gaps(db)[0].choices == expected


# ── resolution preserves history ─────────────────────────────────────────────────────────────────

def test_resolving_a_gap_preserves_the_original_and_writes_a_link(db):
    """The original is never updated or deleted: "what did we not know when that decision was made?"
    must stay answerable."""
    event_id = record_gap(db, _gap(), now=_NOW)
    resolution = resolve_gap(db, event_id, decision="report_cutoff", actor="risk-owner", now=_NOW)
    assert resolution != event_id
    original = db.execute(
        "SELECT kind, decision FROM analysis_learning_event WHERE event_id = %s",
        (event_id,)).fetchone()
    assert original == ("gap", None), "the original must be untouched"
    link = db.execute(
        "SELECT resolves_event_id, decision, decided_by FROM analysis_learning_event "
        "WHERE event_id = %s", (resolution,)).fetchone()
    assert link == (event_id, "report_cutoff", "risk-owner")


def test_a_resolved_gap_leaves_the_open_list(db):
    event_id = record_gap(db, _gap(), now=_NOW)
    assert len(open_gaps(db)) == 1
    resolve_gap(db, event_id, decision="report_cutoff", actor="owner", now=_NOW)
    assert open_gaps(db) == ()


def test_resolving_one_request_clears_the_gap_for_every_request_waiting_on_it(db):
    """It is ONE decision. Two questions were blocked by it; deciding once unblocks both."""
    first = record_gap(db, _gap(analysis_request_id="req-1"), now=_NOW)
    record_gap(db, _gap(analysis_request_id="req-2"), now=_NOW)
    resolve_gap(db, first, decision="report_cutoff", actor="owner", now=_NOW)
    assert open_gaps(db) == ()


def test_a_decision_outside_the_closed_vocabulary_is_refused(db):
    event_id = record_gap(db, _gap(), now=_NOW)
    with pytest.raises(LearningError, match="is not one of"):
        resolve_gap(db, event_id, decision="whatever_seems_right", actor="owner", now=_NOW)


def test_resolving_an_unknown_event_is_refused(db):
    with pytest.raises(LearningError, match="no learning event"):
        resolve_gap(db, "lrn-nope", decision="report_cutoff", actor="owner", now=_NOW)


def test_re_running_after_resolution_records_a_genuinely_DIFFERENT_remaining_gap(db):
    """The loop closing: the first gap is decided, the question runs again under a new snapshot and
    is blocked by something else — which is progress, and is visible as such."""
    first = record_gap(db, _gap(), now=_NOW)
    resolve_gap(db, first, decision="report_cutoff", actor="owner", now=_NOW)
    record_gap(db, _gap(dependency_snapshot_id="snap-002",
                        code="JOIN_CARDINALITY_UNKNOWN",
                        subject_refs=("fact:join-1",),
                        required_action=RequiredAction.PROFILE_DATA), now=_NOW)
    remaining = open_gaps(db)
    assert len(remaining) == 1 and remaining[0].code == "JOIN_CARDINALITY_UNKNOWN"

"""Release C Task 13 — which crosswalk refusals reach the decision queue, and which never may.

The plan's rule, tested from both sides: a missing business decision becomes an open gap; fan-out,
duplicates and overlap are data-quality or safety refusals and must NOT auto-file as ontology gaps.

The classification is asserted TOTAL against ``CrosswalkAdmissionReason`` itself, so a reason added
later cannot silently default into (or out of) the queue.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB_TABLE,
    FTR_TABLE,
    MAP,
    MAP_TABLE,
    binding,
    definition,
    leg,
    mapping_observation,
)

from featuregen.data_agent.learning import GAP_CODES, RequiredAction, open_gaps
from featuregen.overlay.upload.bridge_realization import (
    ExecutionTier,
    RealizationApplicabilityScopeV1,
)
from featuregen.overlay.upload.crosswalk_admission import (
    CrosswalkAdmissionPolicyV1,
    CrosswalkAdmissionReason,
    evaluate_crosswalk_admission,
)
from featuregen.overlay.upload.crosswalk_learning import (
    CROSSWALK_DECISION_GAPS,
    CROSSWALK_REASON_FAMILY,
    CrosswalkReasonFamily,
    crosswalk_learning_events,
    reason_families,
    record_crosswalk_gaps,
    stable_crosswalk_request_id,
)
from featuregen.overlay.upload.crosswalk_observation import (
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    CrosswalkObservationCaveat,
    compose_crosswalk_observation,
)

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
SNAPSHOT = "snap-crosswalk-1"
DEFINITION = definition()
POLICY = CrosswalkAdmissionPolicyV1()

CIB = binding("cib", CIB_TABLE)
FTR = binding("ftr", FTR_TABLE, schema="dpl_ftr")
MAP_BINDING = binding("cib", MAP_TABLE)

SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-scope", execution_tier=ExecutionTier.PRODUCTION,
    purposes=("feature_materialization",), environment="prod")


def observation(**over):
    source_leg = leg(which=SOURCE_TO_MAPPING, endpoint_binding=CIB, mapping_binding=MAP_BINDING,
                     endpoint_column="acct_no", mapping_column="acct_no")
    target_leg = leg(which=MAPPING_TO_TARGET, endpoint_binding=FTR, mapping_binding=MAP_BINDING,
                     endpoint_column="counter_party_acct_no", mapping_column="ext_acct_ref",
                     cross_catalog=True)
    kwargs = dict(
        crosswalk_definition_revision_id=DEFINITION.revision_id,
        source_leg=source_leg, target_leg=target_leg,
        mapping=mapping_observation(MAP_BINDING),
        scope_id=SCOPE.scope_id,
        matched_source_distinct=3, unmatched_source_distinct=1,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3,
        source_to_target_max_matches=1, target_to_source_max_matches=1,
        observed_at=NOW,
        # The BASE measurement is fully governed — a declared mapping row policy and the selection
        # hash it resolved to. Without them `compose_crosswalk_observation` raises the
        # `MAPPING_TEMPORAL_POLICY_ABSENT` caveat itself, every case below would carry the temporal
        # decision, and the "files nothing" negatives would be passing for the wrong reason.
        mapping_temporal_policy_revision_id="dtp_" + "9" * 64,
        mapping_row_selection_hash="b" * 64)
    kwargs.update(over)
    return compose_crosswalk_observation(**kwargs)


def decide(**over):
    kwargs = dict(
        crosswalk_definition_revision_id=DEFINITION.revision_id,
        scope=SCOPE, policy=POLICY, now=NOW)
    kwargs.update(over)
    return evaluate_crosswalk_admission(**kwargs)


def events(decision):
    return crosswalk_learning_events(
        decision, definition=DEFINITION, dependency_snapshot_id=SNAPSHOT)


# ── the classification is CLOSED and TOTAL ──────────────────────────────────────────────────────

def test_every_admission_reason_is_classified_into_exactly_one_family() -> None:
    """Adding a reason without classifying it fails HERE rather than shipping unclassified."""
    refusals = {
        reason.value for reason in CrosswalkAdmissionReason
        if reason is not CrosswalkAdmissionReason.POLICY_SATISFIED}
    assert set(CROSSWALK_REASON_FAMILY) == refusals, (
        "the family table and the refusal vocabulary have drifted: "
        f"unclassified={sorted(refusals - set(CROSSWALK_REASON_FAMILY))}, "
        f"unknown={sorted(set(CROSSWALK_REASON_FAMILY) - refusals)}")
    assert all(isinstance(f, CrosswalkReasonFamily) for f in CROSSWALK_REASON_FAMILY.values())


def test_the_positive_verdict_is_not_a_family_member() -> None:
    """`POLICY_SATISFIED` is the ANSWER, not a reason a user is owed an explanation for."""
    assert CrosswalkAdmissionReason.POLICY_SATISFIED.value not in CROSSWALK_REASON_FAMILY


def test_only_the_undecided_family_may_file_a_gap() -> None:
    for code in CROSSWALK_DECISION_GAPS:
        assert CROSSWALK_REASON_FAMILY[code] is CrosswalkReasonFamily.UNDECIDED, (
            f"{code} files a learning gap and is not in the `undecided` family: a gap nobody can "
            f"close would sit in the reviewer queue forever")


def test_every_gap_code_is_an_actionable_functional_gap() -> None:
    """The learning contract refuses anything else at construction; this catches it at the table."""
    for gap_code, action in CROSSWALK_DECISION_GAPS.values():
        assert gap_code in GAP_CODES
        assert isinstance(action, RequiredAction)


def test_reason_families_omits_what_it_does_not_know_rather_than_guessing() -> None:
    resolved = reason_families((
        CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value, "SOMETHING_NOBODY_DECLARED"))
    assert resolved == {
        CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value:
            CrosswalkReasonFamily.STRUCTURALLY_UNSUITABLE}


# ── missing business decisions DO reach the queue ───────────────────────────────────────────────

def test_an_undeclared_mapping_row_policy_files_a_temporal_decision_gap() -> None:
    decision = decide(observation=observation(
        mapping_temporal_policy_revision_id=None, mapping_row_selection_hash=None))

    filed = events(decision)

    assert [e.code for e in filed] == ["TEMPORAL_MODEL_UNRESOLVED"]
    assert filed[0].required_action is RequiredAction.DECLARE_TEMPORAL_POLICY
    # The SUBJECT is the mapping dataset — that is the screen the decision is made on, and every
    # crosswalk through this table is waiting on the same one.
    assert filed[0].subject_refs == (MAP,)
    assert filed[0].stage.value == "planning"


def test_the_two_temporal_reasons_are_ONE_decision_and_therefore_one_gap() -> None:
    """A reviewer who declares the row policy closes both; two rows would double-count demand."""
    decision = decide(observation=observation(
        mapping_temporal_policy_revision_id=None, mapping_row_selection_hash=None,
        caveats=(CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value,
                 CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value)))

    codes = {r for r in decision.reason_codes}
    assert CrosswalkAdmissionReason.MAPPING_ROWS_NOT_TEMPORALLY_FILTERED.value in codes
    assert CrosswalkAdmissionReason.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value in codes
    assert len(events(decision)) == 1


# ── fan-out, duplicates and overlap NEVER do ────────────────────────────────────────────────────

def test_a_measured_fanout_files_no_ontology_gap() -> None:
    """The defect the whole release exists to prevent is not a decision anybody can make."""
    decision = decide(observation=observation(target_to_source_max_matches=2))

    reverse = decision.reverse
    assert CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value in reverse.reason_codes
    assert events(decision) == (), (
        "a measured fan-out reached the decision queue: no reviewer can decide the data away, and "
        "the row would imply that approving something makes the crosswalk safe")


def test_two_active_mappings_file_no_ontology_gap() -> None:
    decision = decide(observation=observation(), active_mapping_count=2)

    assert (CrosswalkAdmissionReason.MULTIPLE_ACTIVE_MAPPINGS.value
            in decision.reason_codes)
    assert events(decision) == ()


def test_null_mapping_rows_file_no_ontology_gap() -> None:
    decision = decide(observation=observation(
        mapping=mapping_observation(MAP_BINDING, non_null_row_count=1)))

    assert CrosswalkAdmissionReason.MAPPING_NULL_ROWS.value in decision.reason_codes
    assert events(decision) == ()


def test_an_unmeasured_crosswalk_files_no_gap_and_is_not_a_failure() -> None:
    """"Nobody has profiled this yet" is a job, not a judgement — and never a queue row."""
    decision = decide(observation=None)

    assert CrosswalkAdmissionReason.NOT_MEASURED.value in decision.reason_codes
    assert (CROSSWALK_REASON_FAMILY[CrosswalkAdmissionReason.NOT_MEASURED.value]
            is CrosswalkReasonFamily.NEEDS_DATA_CHECK)
    assert events(decision) == ()


def test_a_clean_crosswalk_files_nothing() -> None:
    decision = decide(observation=observation())

    assert decision.forward.production_admissible is True
    assert events(decision) == ()


# ── the producer: stable ids and real deduplication against the 1034 index ──────────────────────

def test_the_request_id_is_derived_from_the_crosswalk_and_the_snapshot() -> None:
    first = stable_crosswalk_request_id(DEFINITION.revision_id, dependency_snapshot_id=SNAPSHOT)
    again = stable_crosswalk_request_id(DEFINITION.revision_id, dependency_snapshot_id=SNAPSHOT)
    moved = stable_crosswalk_request_id(DEFINITION.revision_id, dependency_snapshot_id="snap-2")
    assert first == again and first != moved
    assert first.startswith("areq-")


def test_recording_the_same_owed_decision_twice_is_one_open_gap(db) -> None:
    decision = decide(observation=observation(
        mapping_temporal_policy_revision_id=None, mapping_row_selection_hash=None))

    first = record_crosswalk_gaps(
        db, decision, definition=DEFINITION, dependency_snapshot_id=SNAPSHOT, now=NOW)
    second = record_crosswalk_gaps(
        db, decision, definition=DEFINITION, dependency_snapshot_id=SNAPSHOT,
        now=NOW + timedelta(hours=3))

    assert first and first == second, "the ON CONFLICT did not match the 1034 partial index"
    gaps = open_gaps(db)
    matching = [g for g in gaps if g.code == "TEMPORAL_MODEL_UNRESOLVED"]
    assert len(matching) == 1
    assert matching[0].blocked_requests == 1


def test_a_new_dependency_snapshot_is_new_information(db) -> None:
    """The gap may have been resolved since; a new catalog state must be re-evaluated."""
    decision = decide(observation=observation(
        mapping_temporal_policy_revision_id=None, mapping_row_selection_hash=None))

    record_crosswalk_gaps(
        db, decision, definition=DEFINITION, dependency_snapshot_id=SNAPSHOT, now=NOW)
    record_crosswalk_gaps(
        db, decision, definition=DEFINITION, dependency_snapshot_id="snap-later", now=NOW)

    matching = [g for g in open_gaps(db) if g.code == "TEMPORAL_MODEL_UNRESOLVED"]
    assert len(matching) == 1, "one thing to decide, however many snapshots reached it"
    assert matching[0].blocked_requests == 2


def test_a_refused_crosswalk_writes_no_row_at_all(db) -> None:
    """The negative, at the STORE — not merely at the event builder."""
    decision = decide(observation=observation(target_to_source_max_matches=2),
                      active_mapping_count=2)

    filed = record_crosswalk_gaps(
        db, decision, definition=DEFINITION, dependency_snapshot_id=SNAPSHOT, now=NOW)

    assert filed == ()
    assert db.execute("SELECT count(*) FROM analysis_learning_event").fetchone()[0] == 0


@pytest.mark.parametrize("family", list(CrosswalkReasonFamily))
def test_no_family_is_spelled_as_a_failure(family: CrosswalkReasonFamily) -> None:
    """The words themselves. A family named `blocked`/`failed`/`rejected` is the no-blocked defect."""
    assert not any(word in family.value
                   for word in ("blocked", "fail", "reject", "error", "denied"))

"""The step-5 aggregate's store: lifecycle rules five tables learned one incident at a time.

Every test seeds UNIQUE ids (the test_retirement_scope lesson: shared fixed ids turn a file run
into an order-dependent flake the moment anything escapes rollback).
"""
from __future__ import annotations

import pytest
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.overlay.upload.code_generation_job_store import (
    InvalidJobMove,
    JobMemberSpecV1,
    JobMovedUnderneath,
    JobStatusV1,
    MemberStateV1,
    advance_job,
    claim_due_job,
    create_job,
    read_events,
    read_job,
    read_job_actions,
    read_members,
    release_job,
    update_member,
)

_DECL_IDENTITY = {"spine": "s", "cadence": "daily"}
_PARAMS = {"engine_id": "kedro-pyspark", "physical_type_policy": "strict",
           "empty_values": {}, "target_mode": "exploration"}


def _seed(conn, tag: str) -> tuple[str, list[JobMemberSpecV1]]:
    considered = f"crev-{tag}"
    seed_run_chain(conn, run_id=f"cgjs-{tag}", considered_revision_id=considered)
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s,'int-cgj','exploration','h') ON CONFLICT DO NOTHING", (f"trr-{tag}",))
    members = []
    for position, option in enumerate(("opt-a", "opt-b")):
        selection = f"sel-{tag}-{position}"
        conn.execute(
            "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
            "considered_revision_id, option_id, decision_id, planning_request_hash, "
            "binding_plan_hash, content_hash) VALUES (%s,%s,%s,%s,%s,'sha256:ask','sha256:plan',"
            "%s) ON CONFLICT DO NOTHING",
            (selection, f"trr-{tag}", considered, option, f"dec-{selection}", f"ch-{selection}"))
        members.append(JobMemberSpecV1(
            position=position, selection_revision_id=selection,
            considered_revision_id=considered, option_id=option,
            formula_strategy="llm_authored"))
    return considered, members


def _create(conn, tag: str, **overrides) -> str:
    considered, members = _seed(conn, tag)
    job_id, created = create_job(
        conn, job_id=f"cgj-{tag}", considered_revision_id=considered,
        target_reading_revision_id=f"trr-{tag}", environment_id="hdfc-local",
        logical_group_name=f"grp-{tag}", declaration={"decl": tag},
        declaration_identity=_DECL_IDENTITY, execution_parameters=_PARAMS,
        members=overrides.get("members", members), requested_by="user:sam",
        requested_at="2026-08-23T00:00:00Z")
    assert created is overrides.get("expect_created", True)
    return job_id


# ══ creation, idempotency, and the live-scope guard ═════════════════════════════════════════════
def test_A_JOB_IS_RECORDED_WHOLE_members_actions_and_first_event(db):
    job_id = _create(db, "whole")

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.REQUESTED
    members = read_members(db, job_id)
    assert [m.position for m in members] == [0, 1]
    assert all(m.member_state is MemberStateV1.SELECTED for m in members)
    # ▲ One row PER ACTION the journey performs (§0.1.3), PENDING until its stage runs.
    assert [(a["action"], a["state"]) for a in read_job_actions(db, job_id)] == [
        ("AUTHOR_FORMULA", "PENDING"), ("GENERATE_PREVIEW", "PENDING")]
    assert [e["stage"] for e in read_events(db, job_id)] == ["REQUESTED"]


def test_THE_SAME_CLICK_IS_THE_SAME_JOB(db):
    considered, members = _seed(db, "dup")
    first, created_first = create_job(
        db, job_id="cgj-dup-1", considered_revision_id=considered,
        target_reading_revision_id="trr-dup", environment_id="hdfc-local",
        logical_group_name="grp-dup", declaration={}, declaration_identity=_DECL_IDENTITY,
        execution_parameters=_PARAMS, members=members, requested_by="user:sam",
        requested_at="2026-08-23T00:00:00Z")
    second, created_second = create_job(
        db, job_id="cgj-dup-2", considered_revision_id=considered,
        target_reading_revision_id="trr-dup", environment_id="hdfc-local",
        logical_group_name="grp-dup", declaration={}, declaration_identity=_DECL_IDENTITY,
        execution_parameters=_PARAMS, members=members, requested_by="user:sam",
        requested_at="2026-08-23T00:01:00Z")

    assert (created_first, created_second) == (True, False)
    assert second == first, "the double-click answer is the LIVE job, not a parallel build"


def test_A_FAILED_JOB_RELEASES_THE_IDENTITY_SLOT(db):
    """1107's money-guard lesson, applied: a failed job is not an answer, and recovery must not
    need a content change nobody wants."""
    considered, members = _seed(db, "slot")
    first, _ = create_job(
        db, job_id="cgj-slot-1", considered_revision_id=considered,
        target_reading_revision_id="trr-slot", environment_id="hdfc-local",
        logical_group_name="grp-slot", declaration={}, declaration_identity=_DECL_IDENTITY,
        execution_parameters=_PARAMS, members=members, requested_by="user:sam",
        requested_at="2026-08-23T00:00:00Z")
    advance_job(db, first, JobStatusV1.REQUESTED, JobStatusV1.FAILED,
                terminal_detail={"failure": "crash"})

    retry, created = create_job(
        db, job_id="cgj-slot-2", considered_revision_id=considered,
        target_reading_revision_id="trr-slot", environment_id="hdfc-local",
        logical_group_name="grp-slot", declaration={}, declaration_identity=_DECL_IDENTITY,
        execution_parameters=_PARAMS, members=members, requested_by="user:sam",
        requested_at="2026-08-23T00:01:00Z")
    assert created is True and retry == "cgj-slot-2"


def test_no_members_and_duplicate_selections_are_refused_BY_NAME(db):
    considered, members = _seed(db, "refuse")
    with pytest.raises(ValueError, match="build of nothing"):
        create_job(db, job_id="cgj-r0", considered_revision_id=considered,
                   target_reading_revision_id="trr-refuse", environment_id="e",
                   logical_group_name="g", declaration={}, declaration_identity={},
                   execution_parameters={}, members=(), requested_by="u", requested_at="2026-08-23T00:00:00Z")
    twice = (members[0], JobMemberSpecV1(
        position=1, selection_revision_id=members[0].selection_revision_id,
        considered_revision_id=considered, option_id="opt-a", formula_strategy="llm_authored"))
    with pytest.raises(ValueError, match="appears twice"):
        create_job(db, job_id="cgj-r1", considered_revision_id=considered,
                   target_reading_revision_id="trr-refuse", environment_id="e",
                   logical_group_name="g", declaration={}, declaration_identity={},
                   execution_parameters={}, members=twice, requested_by="u", requested_at="2026-08-23T00:00:00Z")


# ══ the lease ═══════════════════════════════════════════════════════════════════════════════════
def test_A_CLAIM_IS_A_LEASE_and_a_leased_job_is_not_claimable(db):
    job_id = _create(db, "lease")

    claimed = claim_due_job(db, worker_id="w1")
    assert claimed is not None and claimed.job_id == job_id
    assert claimed.lease_fence == 1 and claimed.attempts == 1
    assert claim_due_job(db, worker_id="w2") is None, "the lease excludes a second worker"

    release_job(db, job_id)
    reclaimed = claim_due_job(db, worker_id="w2")
    assert reclaimed is not None and reclaimed.lease_fence == 2


def test_a_terminal_job_is_never_claimed(db):
    job_id = _create(db, "term")
    advance_job(db, job_id, JobStatusV1.REQUESTED, JobStatusV1.CANCELLED,
                terminal_detail={"cancelled_by": "user:sam"})
    assert claim_due_job(db, worker_id="w1") is None


# ══ compare-and-set advances ════════════════════════════════════════════════════════════════════
def test_ADVANCE_IS_COMPARE_AND_SET_never_read_then_write(db):
    job_id = _create(db, "cas")
    advance_job(db, job_id, JobStatusV1.REQUESTED, JobStatusV1.PLANNING_FORMULAS)

    with pytest.raises(JobMovedUnderneath, match="somebody else moved it"):
        advance_job(db, job_id, JobStatusV1.REQUESTED, JobStatusV1.PLANNING_FORMULAS)
    with pytest.raises(InvalidJobMove):
        advance_job(db, job_id, JobStatusV1.PLANNING_FORMULAS, JobStatusV1.PREVIEW_READY)
    with pytest.raises(InvalidJobMove, match="terminal detail on a non-terminal"):
        advance_job(db, job_id, JobStatusV1.PLANNING_FORMULAS, JobStatusV1.AUTHORING,
                    terminal_detail={"x": 1})


def test_terminal_moves_release_the_lease_and_record_the_event(db):
    job_id = _create(db, "trel")
    claim_due_job(db, worker_id="w1")
    advance_job(db, job_id, JobStatusV1.REQUESTED, JobStatusV1.BLOCKED,
                terminal_detail={"members": []})

    row = db.execute(
        "SELECT lease_owner, lease_expires_at FROM code_generation_job WHERE job_id = %s",
        (job_id,)).fetchone()
    assert row == (None, None)
    assert [e["stage"] for e in read_events(db, job_id)] == ["REQUESTED", "BLOCKED"]


# ══ members ═════════════════════════════════════════════════════════════════════════════════════
def test_A_BLOCKED_MEMBER_NAMES_ITS_BLOCKERS_and_only_a_blocked_member_does(db):
    job_id = _create(db, "mem")
    with pytest.raises(InvalidJobMove, match="BLOCKED member names its blockers"):
        update_member(db, job_id, 0, state=MemberStateV1.BLOCKED)
    with pytest.raises(InvalidJobMove):
        update_member(db, job_id, 0, state=MemberStateV1.AUTHORING, blockers=["X"])

    update_member(db, job_id, 0, state=MemberStateV1.BLOCKED, blockers=["FORMULA_DRAFT_RETIRED"])
    member = read_members(db, job_id)[0]
    assert member.member_state is MemberStateV1.BLOCKED
    assert member.blockers == ("FORMULA_DRAFT_RETIRED",)


def test_the_event_history_is_append_only_by_trigger(db):
    job_id = _create(db, "evt")
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM code_generation_job_event WHERE job_id = %s", (job_id,))

"""Step 5a — the durable coordinator: one explicit user act, driven to a preview.

The journey it owns (child §3.5, a PREVIEW coordinator by settlement): resolve each member's
authoring strategy → request the drafts through the ONE shared composition
(``formula_draft_service``) → wait on DURABLE formula states, never on providers → bind each
selection to the exact formula it produced (parent §11.0.1) → declare the build set → decide
``GENERATE_PREVIEW`` through the one decision service → enqueue the generation the existing lane
performs → terminal at ``PREVIEW_READY``, with links to what follows.

**What this module is NOT.** It is not a second authority over any act: authoring is gated where
drafts are gated (money guard, tombstones, spend), generation is gated by the same
``authorize_and_decide_generation`` the route calls, and sandbox/production acts belong to their
own lanes. The coordinator SEQUENCES; every gate it passes through is the gate everything else
passes through.

**Crash discipline.** One stage per claim, compare-and-set advances, lease with a rising fence —
and NO explicit commits: the production worker's connection is autocommit, and a ``conn.commit()``
here would be a no-op there and a leak in every transactional caller (the 1103 lesson, relearned
by the verification lane last night).

**BLOCKED vs FAILED**, per the standing split: a member the platform REFUSES (retired identity,
not a formula, strategy refused, draft blocked) blocks the job with the members' blockers as the
answer; a platform crash fails it with the posture named. "Continue with the ready members" is a
NEW job identity created by an explicit user act — never a silent drop of a selected feature.
"""
from __future__ import annotations

import traceback
from typing import Any

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.upload.code_generation_job_store import (
    CodeGenJobV1,
    CodeGenMemberV1,
    JobStatusV1,
    MemberStateV1,
    advance_job,
    claim_due_job,
    read_job,
    read_members,
    record_event,
    record_job_action,
    release_job,
    update_member,
)
from featuregen.runtime.observability import counters, log

__all__ = ["process_code_generation_once"]


def _now(conn) -> str:
    return str(conn.execute("SELECT now()").fetchone()[0])


def process_code_generation_once(conn, *, worker_id: str) -> bool:
    """Claim and drive ONE due job one stage forward. Returns whether a job was claimed."""
    job = claim_due_job(conn, worker_id=worker_id)
    if job is None:
        return False
    try:
        _drive(conn, job)
        # ▲ ONE STAGE PER CLAIM, made honest: the lease exists to protect the DRIVE, so it is
        # given back the moment the stage lands — otherwise a healthy job waits out the full
        # lease TTL between every stage, and "the worker is driving it" becomes 120-second
        # silences. Every advance is compare-and-set, so a concurrent claim after this release
        # cannot double-apply a stage.
        current = read_job(conn, job.job_id)
        if current is not None and not current.status.is_terminal:
            release_job(conn, job.job_id)
    except Exception:
        # A platform crash, not a product outcome: FAILED with the traceback recorded, so the
        # operator page names the crash and the members' blockers stay what they were.
        current = read_job(conn, job.job_id)
        if current is not None and not current.status.is_terminal:
            advance_job(conn, job.job_id, current.status, JobStatusV1.FAILED,
                        terminal_detail={"failure": "coordinator crash",
                                         "traceback": traceback.format_exc()[-2000:]})
        counters.incr("featuregen.code_generation_job.crashed")
        raise
    return True


def _drive(conn, job: CodeGenJobV1) -> None:
    if job.status is JobStatusV1.REQUESTED:
        advance_job(conn, job.job_id, JobStatusV1.REQUESTED, JobStatusV1.PLANNING_FORMULAS)
        _plan_formulas(conn, job)
    elif job.status is JobStatusV1.PLANNING_FORMULAS:
        _plan_formulas(conn, job)
    elif job.status is JobStatusV1.AUTHORING:
        _watch_authoring(conn, job)
    elif job.status is JobStatusV1.READY_TO_BUILD:
        _build(conn, job)
    elif job.status is JobStatusV1.GENERATING_PREVIEW:
        _watch_generation(conn, job)
    # Terminal states are never claimed (the due-scan excludes them); nothing else to do.


# ── stage: request every member's draft through the ONE composition ─────────────────────────────
def _plan_formulas(conn, job: CodeGenJobV1) -> None:
    from featuregen.overlay.upload.formula_draft_service import (
        CandidateUnavailable,
        NotAFormulaCandidate,
        NotAnAnswerAtRequest,
        RetiredAtRequest,
        StrategyRefused,
        request_draft_for_candidate,
    )

    # §11.2 — the ceiling the write endpoint recorded for THIS job, if any: resolved once and
    # handed to every member's draft request, so the plan rows carry it to the dispatch seam.
    spend_row = conn.execute(
        "SELECT spend_authorization_id FROM llm_spend_authorization_revision "
        "WHERE job_identity = %s AND expires_at > now() "
        "ORDER BY authorized_at DESC LIMIT 1", (job.content_identity_hash,)).fetchone()
    spend_authorization_id = None if spend_row is None else spend_row[0]

    for member in read_members(conn, job.job_id):
        if member.member_state is not MemberStateV1.SELECTED:
            continue
        try:
            result = request_draft_for_candidate(
                conn, revision_id=member.considered_revision_id, option_id=member.option_id,
                formula_draft_id=mint_id("fd"), requested_by=job.requested_by, now=_now(conn),
                spend_authorization_id=spend_authorization_id)
        except RetiredAtRequest:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=["FORMULA_DRAFT_RETIRED"])
        except NotAnAnswerAtRequest:
            # A member whose previous attempt FAILED: blocked by name, never a whole-job
            # "coordinator crash" — re-buying is an approved act (§11.1.2).
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=["FORMULA_DRAFT_NOT_AN_ANSWER"])
        except NotAFormulaCandidate as exc:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=[exc.code])
        except StrategyRefused as exc:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=list(exc.blockers))
        except CandidateUnavailable as exc:
            # The job's own FK proved the revision existed at request time; failing to resolve it
            # NOW is a platform inconsistency, not a product refusal.
            update_member(conn, job.job_id, member.position, state=MemberStateV1.FAILED)
            record_event(conn, job.job_id, "MEMBER_FAILED",
                         {"position": member.position, "kind": exc.kind, "detail": exc.detail})
        else:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.AUTHORING,
                          formula_draft_id=result.formula_draft_id)

    # ▲ The AUTHOR_FORMULA action row goes PERFORMED at the aggregate level. Its resource stays
    # NULL deliberately: §0.1.4 makes AUTHOR_FORMULA's subject a CANDIDATE, so the per-candidate
    # authorization evidence lives with each draft — one job row could not truthfully name N
    # subjects, and inventing an aggregate one would be a second identity for the same act.
    record_job_action(conn, job.job_id, "AUTHOR_FORMULA", state="PERFORMED")
    _settle_after_authoring(conn, job, from_status=JobStatusV1.PLANNING_FORMULAS)


# ── stage: wait on DURABLE draft states — never on providers ────────────────────────────────────
def _watch_authoring(conn, job: CodeGenJobV1) -> None:
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, read_draft

    for member in read_members(conn, job.job_id):
        if member.member_state is not MemberStateV1.AUTHORING:
            continue
        draft = None if member.formula_draft_id is None else read_draft(
            conn, member.formula_draft_id)
        if draft is None:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.FAILED)
            record_event(conn, job.job_id, "MEMBER_FAILED",
                         {"position": member.position,
                          "detail": "the member's draft row is gone — a platform inconsistency"})
            continue
        if draft.is_retired:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=["FORMULA_DRAFT_RETIRED"])
        elif draft.state is DraftStateV1.READY:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.FORMULA_READY)
        elif draft.state is DraftStateV1.BLOCKED:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=list(draft.blockers) or ["FORMULA_AUTHORING_BLOCKED"])
        elif draft.state in (DraftStateV1.FAILED, DraftStateV1.CANCELLED):
            update_member(conn, job.job_id, member.position, state=MemberStateV1.FAILED)
            record_event(conn, job.job_id, "MEMBER_FAILED",
                         {"position": member.position, "draft_state": draft.state.value,
                          "detail": draft.failure_reason or ""})
        # else: still authoring — leave it; the lease expiry paces the next look.

    _settle_after_authoring(conn, job, from_status=JobStatusV1.AUTHORING)


def _settle_after_authoring(conn, job: CodeGenJobV1, *, from_status: JobStatusV1) -> None:
    """Advance, block, or keep waiting — decided from the MEMBERS, which are the durable truth."""
    members = read_members(conn, job.job_id)
    still_working = [m for m in members if m.member_state in (
        MemberStateV1.SELECTED, MemberStateV1.AUTHORING)]
    refused = [m for m in members if m.member_state in (
        MemberStateV1.BLOCKED, MemberStateV1.FAILED)]

    if still_working:
        # ▲ Some members refused ALREADY, but the job waits for every member to settle before it
        # blocks: "which of my selections need attention" must be one complete answer, not a
        # drip-feed of partial refusals that each restart the conversation.
        if from_status is JobStatusV1.PLANNING_FORMULAS:
            advance_job(conn, job.job_id, from_status, JobStatusV1.AUTHORING)
        else:
            release_job(conn, job.job_id)
        return
    if refused:
        failed = [m for m in refused if m.member_state is MemberStateV1.FAILED]
        advance_job(
            conn, job.job_id, from_status,
            JobStatusV1.FAILED if failed and len(failed) == len(refused) else JobStatusV1.BLOCKED,
            terminal_detail={
                "members": [
                    {"position": m.position, "state": m.member_state.value,
                     "blockers": list(m.blockers)} for m in refused],
                "detail": ("no selected feature is silently dropped: continuing with the ready "
                           "members is an explicit user act that creates a NEW job")})
        counters.incr("featuregen.code_generation_job.blocked")
        return
    if from_status is JobStatusV1.PLANNING_FORMULAS:
        # Every member settled inside one claim (all already READY — e.g. deduplicated drafts).
        advance_job(conn, job.job_id, from_status, JobStatusV1.AUTHORING)
        advance_job(conn, job.job_id, JobStatusV1.AUTHORING, JobStatusV1.READY_TO_BUILD)
    else:
        advance_job(conn, job.job_id, from_status, JobStatusV1.READY_TO_BUILD)


# ── stage: bind, declare, decide, enqueue — the build itself stays the lane's ───────────────────
def _build(conn, job: CodeGenJobV1) -> None:
    from featuregen.materialize.build_set_declaration import decode_declaration
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        TargetModeV1,
        record_generation_authorization,
    )
    from featuregen.materialize.generation_lane import (
        GenerationJobV2,
        authorize_and_decide_generation,
        enqueue_generation,
    )
    from featuregen.overlay.upload.build_set_store import (
        read_build_set,
        record_build_set,
        request_generation,
    )
    from featuregen.overlay.upload.selection_formula_binding import (
        BindingDisagreement,
        record_selection_formula_binding,
    )

    now = _now(conn)
    members = read_members(conn, job.job_id)

    # 1) The BINDING per member — parent §11.0.1: the relational proof the formula belongs to the
    # selection. The coordinator hands the binding through; it never re-resolves "the newest draft".
    for member in members:
        if member.member_state is MemberStateV1.BOUND:
            continue
        try:
            binding, _created = record_selection_formula_binding(
                conn, selection_revision_id=member.selection_revision_id,
                formula_draft_id=member.formula_draft_id)
        except BindingDisagreement as exc:
            update_member(conn, job.job_id, member.position, state=MemberStateV1.BLOCKED,
                          blockers=["SELECTION_FORMULA_BINDING_MISSING"])
            record_event(conn, job.job_id, "MEMBER_BINDING_REFUSED",
                         {"position": member.position, "detail": str(exc)})
            advance_job(conn, job.job_id, JobStatusV1.READY_TO_BUILD, JobStatusV1.BLOCKED,
                        terminal_detail={"members": [{
                            "position": member.position, "state": "BLOCKED",
                            "blockers": ["SELECTION_FORMULA_BINDING_MISSING"],
                            "detail": str(exc)}]})
            return
        update_member(conn, job.job_id, member.position, state=MemberStateV1.BOUND,
                      selection_formula_binding_id=binding.binding_id)

    members = read_members(conn, job.job_id)
    binding_ids = [m.selection_formula_binding_id for m in members]

    # 2) The build set, from the job's frozen declaration — idempotent on content, so a re-driven
    # claim after a crash lands on the same set.
    declaration = decode_declaration(job.declaration)
    build_set_revision_id, _created = record_build_set(
        conn, revision_id=mint_id("bs"),
        target_reading_revision_id=job.target_reading_revision_id,
        selection_formula_binding_ids=binding_ids, declaration=declaration,
        declared_by=job.requested_by, declared_at=now)
    build_set = read_build_set(conn, build_set_revision_id)

    # 3) The generation approval — SERVER-minted from the job's own facts, idempotent on content.
    # The old model stays authoritative for 1095's chain until 1100b; this is the §0.1.0 posture:
    # permission is broad and server-owned, and the record still names who.
    params = job.execution_parameters
    authorization_revision_id = record_generation_authorization(
        conn,
        GenerationAuthorizationV1(
            environment_id=job.environment_id, logical_group_name=job.logical_group_name,
            build_set_revision_id=build_set_revision_id,
            target_mode=TargetModeV1(params.get("target_mode", "exploration")),
            target_ref=params.get("target_ref")),
        authorized_by=job.requested_by, authorized_at=now)

    # 4) The request-time decision (§8.2) through the ONE service — the worker's second look
    # rechecks exactly this.
    decision_id, decision = authorize_and_decide_generation(
        conn, build_set_revision_id=build_set_revision_id,
        build_set_content_hash=build_set.content_hash,
        generation_authorization_revision_id=authorization_revision_id,
        environment_id=job.environment_id, actor_subject=job.requested_by,
        member_names=tuple(build_set.selection_revision_ids))
    action_authorization = conn.execute(
        "SELECT authorization_id FROM action_decision_revision WHERE decision_id = %s",
        (decision_id,)).fetchone()
    record_job_action(
        conn, job.job_id, "GENERATE_PREVIEW", resource_identity_hash=build_set_revision_id,
        authorization_revision_id=None if action_authorization is None else action_authorization[0],
        decision_revision_id=decision_id,
        state="DECIDED" if decision.allowed else "REFUSED")
    if not decision.allowed:
        advance_job(conn, job.job_id, JobStatusV1.READY_TO_BUILD, JobStatusV1.BLOCKED,
                    terminal_detail={"decision_blockers": list(decision.blockers),
                                     "decision_revision_id": decision_id})
        counters.incr("featuregen.code_generation_job.blocked")
        return

    # 5) The generation request and its queue row, together — the existing lane does the rest.
    request_id, created = request_generation(
        conn, request_id=mint_id("gen"), build_set_revision_id=build_set_revision_id,
        environment_id=job.environment_id, requested_by=job.requested_by, requested_at=now,
        generation_authorization_revision_id=authorization_revision_id,
        action_decision_revision_id=decision_id)
    if created:
        enqueue_generation(
            conn,
            job=GenerationJobV2(
                request_id=request_id,
                spine_declaration=declaration.spine_declaration,
                cadence=declaration.cadence,
                availability_promise=declaration.availability_promise,
                physical_type_policy=params["physical_type_policy"],
                empty_values=dict(params["empty_values"]),
                operand_facts=dict(declaration.operand_facts),
                policy_realization_ids=dict(declaration.policy_realization_ids),
                engine_id=params["engine_id"],
                roles=tuple(params.get("roles", ())),
                compiled_at=now, sealed_at=now,
                action_decision_revision_id=decision_id),
            environment_id=job.environment_id, logical_group_name=job.logical_group_name)

    conn.execute(
        "UPDATE code_generation_job SET build_set_revision_id = %s, generation_request_id = %s "
        "WHERE job_id = %s", (build_set_revision_id, request_id, job.job_id))
    record_job_action(conn, job.job_id, "GENERATE_PREVIEW", state="PERFORMED")
    advance_job(conn, job.job_id, JobStatusV1.READY_TO_BUILD, JobStatusV1.GENERATING_PREVIEW)
    log("featuregen.code_generation_job.generation_requested", job_id=job.job_id,
        build_set_revision_id=build_set_revision_id, request_id=request_id)


# ── stage: watch the generation — durable state again, never the worker ─────────────────────────
def _watch_generation(conn, job: CodeGenJobV1) -> None:
    from featuregen.overlay.upload.build_set_store import GenerationStatusV1, read_request

    request = None if job.generation_request_id is None else read_request(
        conn, job.generation_request_id)
    if request is None:
        advance_job(conn, job.job_id, JobStatusV1.GENERATING_PREVIEW, JobStatusV1.FAILED,
                    terminal_detail={"failure": "the generation request row is gone — a platform "
                                                "inconsistency"})
        return
    if request.status is GenerationStatusV1.SUCCEEDED:
        advance_job(conn, job.job_id, JobStatusV1.GENERATING_PREVIEW, JobStatusV1.PREVIEW_READY)
        counters.incr("featuregen.code_generation_job.preview_ready")
    elif request.status is GenerationStatusV1.REFUSED:
        advance_job(conn, job.job_id, JobStatusV1.GENERATING_PREVIEW, JobStatusV1.BLOCKED,
                    terminal_detail={"refusals": [dict(r) for r in request.refusals],
                                     "generation_request_id": request.request_id})
    elif request.status in (GenerationStatusV1.FAILED, GenerationStatusV1.CANCELLED):
        advance_job(conn, job.job_id, JobStatusV1.GENERATING_PREVIEW, JobStatusV1.FAILED,
                    terminal_detail={"generation_status": request.status.value,
                                     "failure_reason": request.failure_reason})
    else:
        release_job(conn, job.job_id)   # still compiling — the lease expiry paces the next look

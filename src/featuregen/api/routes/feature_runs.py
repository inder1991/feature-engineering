"""The run spine's surface (spec §5/§11/§12) — two reads, and TWO gestures (§R4.4.2, §R4.2).

404 covers both absence and denial, deliberately: a distinguishable 403 would confirm the run id
exists — a shape leak the read policy exists to prevent. That rule governs the writes too: a caller
who may not see a run gets the same body for acting on it as for a run that does not exist.

The two GETs are pure reads over the stores that already hold the evidence. NEITHER POST is a new
lifecycle: each records nothing of its own, derives nothing a person must decide, and stores no
state on the run. `prepare-code` resolves the run's own frozen facts and hands them to the ONE
job-creation service (`materialize.code_generation_coordinator.request_code_generation_job`) the
code-generation route also calls; `authoring-retries` hands one recorded attempt's candidate to the
ONE draft-request composition (`overlay.upload.formula_draft_service.request_draft_for_candidate`)
the formula-draft route also calls. Both surfaces therefore reach the same gates, in the same
order, and their refusals are the same refusals with the same statuses.
"""
from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_feature_generate,
    require_feature_read,
)

# ▲ The SIX approval keys, spelled once. This is a read-only import of the code-generation route's
# own body type: the ceiling a person confirms on the run page and the ceiling they confirm on the
# code-generation screen must be the same six fields, and a second declaration of them here is the
# copy that drifts (the Task 5 review's triplicated-constant finding, in body-schema form).
from featuregen.api.routes.code_generation_jobs import SpendApprovalIn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import list_runs, run_detail
from featuregen.runtime.observability import counters, log

#: A named candidate has no `feature_selection_revision` under the run's frozen considered set and
#: the declaring job's target reading — a build is a build of SELECTIONS, and this one was never
#: made. THIS surface's own vocabulary (the projection cannot know which candidates a caller will
#: name), unlike the pre-flight codes, which are `runs.projection`'s and are answered from its
#: `prepare_code` availability so the affordance and the entrance cannot disagree.
RUN_CANDIDATE_NOT_SELECTED = "RUN_CANDIDATE_NOT_SELECTED"

#: A candidate named twice. Also this surface's own — a body is not a fact about a run.
RUN_CANDIDATE_NAMED_TWICE = "RUN_CANDIDATE_NAMED_TWICE"

#: The named draft is not one of THIS run's recorded attempts. This surface's own, for the same
#: reason as the two above: a body is not a fact about a run, and the run's own attempt history is
#: what decides. It says nothing about whether that draft exists somewhere else — which is the point.
RUN_ATTEMPT_NOT_RECORDED = "RUN_ATTEMPT_NOT_RECORDED"

#: The named attempt bought something, or is still buying it. Retry is the question asked of an
#: attempt that bought NOTHING (spec §R4.2); asking it of an answer is R4.2's still-deferred
#: "request another opinion", which needs the identity to move and has no surface yet.
RUN_ATTEMPT_IS_NOT_A_FAILURE = "RUN_ATTEMPT_IS_NOT_A_FAILURE"

router = APIRouter(dependencies=[Depends(require_feature_read)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/feature-runs")
def feature_runs_list(conn: _Conn, identity: _Identity,
                      limit: int = 25, cursor: str | None = None) -> dict:
    """One page of the runs this caller may see, grouped by intent WITHIN the page.

    `archived` is neither filtered nor surfaced in the foundation: nothing can set it, because no
    write endpoint exists to.

    The groups carry the intent's RAW hypothesis, because the reader is either its owner or an
    admin — the redacted variants exist for LLM egress, not for owner display.

    Both caller-supplied query values are the route's to police, not the projection's. `limit` is
    CLAMPED rather than refused (0 would page zero rows and then index the empty page; an
    unbounded value would read the table), and a malformed `cursor` is a 422 because it is spliced
    into a `::timestamptz` cast that raises out of the driver — a caller's typo is never a server
    error."""
    try:
        return list_runs(conn, identity, limit=min(max(limit, 1), 100), cursor=cursor)
    except psycopg.DataError as exc:
        # The failed cast aborts the request transaction; `get_conn` rolls it back on the way out.
        # With no cursor there is no caller-supplied value in that query to blame, so a data error
        # is the server's own and must stay a 500 rather than be mislabelled as bad input.
        if cursor is None:
            raise
        raise HTTPException(status_code=422, detail="cursor is not a valid page cursor") from exc


@router.get("/feature-runs/{run_id}")
def feature_run_detail(run_id: str, conn: _Conn, identity: _Identity) -> dict:
    """One run's milestones, authoring axes and honest rail — or 404 for absent AND invisible.

    The projection returns None for both, and the two are mapped to the SAME body here: telling
    them apart would let a probe enumerate other people's run ids."""
    detail = run_detail(conn, identity, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return detail


class PrepareCodeIn(BaseModel):
    """The run page's gesture: WHICH candidates, and the ceiling the person confirmed.

    Nothing else, and the omissions are the design. The considered revision, the target reading and
    each candidate's selection are the RUN's frozen facts — a body that carried them would let a
    caller aim one run's gesture at another run's revision, and the server would have no way to know
    it had been lied to. They are resolved here instead.

    `spend_approval` is the ONE thing a server cannot resolve, because it is not a fact — it is a
    person agreeing to a cost (§11.2, "a modal is not a money guard"). It is optional in the SCHEMA
    and mandatory in the ANSWER: a job with LLM members and no approval is refused with
    `COST_AUTHORIZATION_MISSING`, by the same coordinator, for the same reason, as on the
    code-generation route. Minting one here would be the modal-as-money-guard defect with a server
    address.
    """

    model_config = ConfigDict(extra="forbid")

    #: ORDERED — the order a person picked candidates in decides the published column order, so this
    #: is a list and never a set. Duplicate-free: naming a feature twice is a caller error, refused
    #: by name below rather than left to the member store's unique constraint.
    option_ids: list[str] = Field(min_length=1)
    spend_approval: SpendApprovalIn | None = None


def _refusal(code: str, detail: str, *, status: int = 409, **extra: Any) -> HTTPException:
    """One refusal shape for this surface: a closed `code`, a sentence a person can act on, and
    whatever facts name the specific thing that is wrong. The same shape the build-set and
    code-generation routes answer with, so a client parses one body everywhere."""
    return HTTPException(status_code=status, detail={"code": code, "detail": detail, **extra})


@router.post("/feature-runs/{run_id}/prepare-code", status_code=202,
             dependencies=[Depends(require_feature_generate)])
def prepare_run_code(run_id: str, body: PrepareCodeIn, conn: _Conn,
                     identity: _Identity) -> dict[str, Any]:
    """Prepare formulas and code for candidates of THIS run — the run page's one write (§R4.4.2).

    `202`: a job is ACCEPTED, not completed — the coordinator's worker drives it, and the refreshed
    run detail in the answer is where it will be watched.

    **THIS ROUTE IS AN ADAPTER, NOT A SECOND AUTHORITY.** Every gate — the members' governance
    blockers, the retirement tombstones, the spend ceiling, the idempotency — belongs to
    `request_code_generation_job`, which is the same function `POST /code-generation-jobs` calls.
    The refusals are re-raised with the statuses that route already answers with (the Task 5
    review's ruling: same refusals, same statuses, both routes), so a person cannot get two
    different answers to one question by arriving through two doors.

    THE ORDER OF THE GATES, and why each is where it is:

    1. **§11 object policy first.** `run_detail` returns None for a run that does not exist AND for
       one this caller may not see, and both become the byte-identical 404 the GET answers with.
       Any refusal placed ahead of it would answer a question about a run whose existence the caller
       has not earned the right to learn.
    2. **The gesture's own availability** — the deployment switch, then `PRE_SPINE_NOT_ACTIONABLE`,
       then `RUN_BUILD_DECLARATION_ABSENT` — taken WHOLE from `prepare_code_availability`, the same
       derivation the run page disables its control off. The precedence and the sentences live
       there; see it for why each step is where it is.
    3. **The candidates the caller named**, resolved against the run's own frozen set. These are
       this route's alone: the projection cannot know what a body will say.
    4. **Everything else is the coordinator's**, unchanged and un-second-guessed.

    ▲ **WHERE THE FIVE DECLARATIONS COME FROM — the honest gap in this gesture.** A build set
    declares the population, the cadence, the availability promise, the operand facts and the policy
    realizations, and `POST /build-sets` states the rule: "a route that filled them in would be
    choosing what gets published on behalf of whoever clicked the button". None of the five is
    derivable from a run, and this route does not derive them. It REUSES the declaration this run's
    most recent code-generation job froze — a declaration a person made, for this run, through the
    route whose body carries it — and names which job it came from in the answer, so the reuse is
    visible rather than silent. A run whose declaration nobody has ever made is refused with
    `RUN_BUILD_DECLARATION_ABSENT` and the sentence that says where to make it. The consequence is
    stated plainly: until a declaration surface exists, this gesture continues a build that was
    declared elsewhere; it cannot start the first one.
    """
    from featuregen.materialize.code_generation_coordinator import (
        CodeGenJobRequestV1,
        CodeGenMembersRefused,
        CodeGenRequestInvalid,
        SelectionUnavailable,
        SpendApprovalRequired,
        request_code_generation_job,
    )
    from featuregen.overlay.upload.code_generation_job_store import read_job
    from featuregen.overlay.upload.formula_draft_service import CandidateUnavailable

    detail = run_detail(conn, identity, run_id)
    if detail is None:
        # Byte-identical to the GET's, and to an absent run's: telling denial and absence apart is
        # exactly the enumeration this policy exists to prevent, and a write is no more entitled to
        # leak it than a read.
        raise HTTPException(status_code=404, detail="Not Found")
    # ▲ THE ENTRANCE AND THE AFFORDANCE, FROM ONE ANSWER. The switch, the missing identity row and
    # the never-made declaration are all decided by `prepare_code_availability`, which is also what
    # the run page disables its control off — so the page cannot offer a gesture this route refuses,
    # and this route cannot refuse one the page said was ready. Re-checking them here in words of
    # this file's own is precisely the drift that rule exists to prevent.
    gate = detail["prepare_code"]
    if not gate["available"]:
        raise _refusal(gate["reason_code"], gate["detail"])

    duplicates = sorted({o for o in body.option_ids if body.option_ids.count(o) > 1})
    if duplicates:
        raise _refusal(
            RUN_CANDIDATE_NAMED_TWICE,
            "a candidate named twice is one choice recorded twice, not two features to build",
            status=422, option_ids=duplicates)

    # ── the declaration this run already made ───────────────────────────────────────────────────
    # `jobs` is the projection's considered-revision bridge, newest first — ONE derivation, two
    # surfaces: the list a person reads on the run page is the list this route picks the
    # declaration out of, so the source can never be a job the page does not show. Its emptiness is
    # already refused above (it is what `prepare_code.available` folds), so this indexes safely.
    source = read_job(conn, detail["jobs"][0]["job_id"])
    if source is None:                                      # pragma: no cover - FK makes it absurd
        raise _refusal(
            "RUN_DECLARATION_SOURCE_GONE",
            "the job this run's declaration was read from is gone — a platform inconsistency")

    # THE RUN's considered revision, not the job's. They agree by construction — `record_run_identity`
    # refuses to write an identity for a run with anything other than exactly one considered
    # revision — and reading the run's own is what makes this gesture the RUN's rather than the
    # job's: a caller cannot aim it at a frozen set this run does not own.
    considered_revision_id = detail["identity"]["considered_revision_id"]

    # ── the selections, resolved from the run's frozen set ──────────────────────────────────────
    # Under the SOURCE JOB's target reading: 1072 keys a selection by (reading, considered set,
    # option), so one candidate can carry a selection under two readings, and the reading this
    # build continues is the one its declaration was made under. Picking "the newest" instead would
    # let a later reading silently move which rows the build is computed for.
    rows = dict(conn.execute(
        "SELECT option_id, revision_id FROM feature_selection_revision "
        "WHERE considered_revision_id = %s AND target_reading_revision_id = %s "
        "AND option_id = ANY(%s)",
        (considered_revision_id, source.target_reading_revision_id,
         list(body.option_ids))).fetchall())
    missing = [o for o in body.option_ids if o not in rows]
    if missing:
        raise _refusal(
            RUN_CANDIDATE_NOT_SELECTED,
            "a build is a build of selections, and no selection was recorded for these candidates "
            f"under target reading {source.target_reading_revision_id!r}: select them first, then "
            "prepare the code", option_ids=missing)

    request = CodeGenJobRequestV1(
        considered_revision_id=considered_revision_id,
        target_reading_revision_id=source.target_reading_revision_id,
        # The CALLER's order, not the query's: the order a person picked features in decides the
        # published table's column order, and a dict built from an unordered SELECT would publish
        # whatever the planner returned.
        selection_revision_ids=tuple(rows[o] for o in body.option_ids),
        environment_id=source.environment_id,
        logical_group_name=source.logical_group_name,
        declaration=source.declaration,
        execution_parameters=source.execution_parameters,
        spend_approval=None if body.spend_approval is None else body.spend_approval.model_dump())

    try:
        job_id, created = request_code_generation_job(
            conn, request, principal=identity)
    except SelectionUnavailable as exc:
        # `unknown_selection` is unreachable from here — this route resolved the ids out of the
        # run's own store — but the mapping is the code-generation route's, verbatim, because a
        # refusal that CAN reach two surfaces must not arrive with two statuses.
        raise HTTPException(
            status_code=404 if exc.kind == "unknown_selection" else 409,
            detail=exc.detail) from exc
    except CandidateUnavailable as exc:
        raise HTTPException(
            status_code={"unknown_revision": 404,
                         "option_not_in_revision": 422}.get(exc.kind, 409),
            detail=exc.detail) from exc
    except CodeGenRequestInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CodeGenMembersRefused as exc:
        raise HTTPException(status_code=409, detail={
            "code": exc.refused[0]["blockers"][0],
            "members": [{"selection_revision_id": p["selection_revision_id"],
                         "option_id": p["option_id"], "blockers": p["blockers"]}
                        for p in exc.refused],
            "detail": "these candidates are refused before any spend; remove them or resolve the "
                      "named blockers, then prepare again"}) from exc
    except SpendApprovalRequired as exc:
        # VERBATIM, including the numbers: the ceiling a person confirms must be quoted from the
        # enforcement's own budget, and a browser that guessed it would be confirming a different
        # spend from the one the platform will hold itself to.
        raise HTTPException(status_code=409, detail={
            "code": "COST_AUTHORIZATION_MISSING",
            "llm_members": exc.llm_members,
            "estimated_provider_calls": exc.estimated_provider_calls,
            "detail": "this job authors with the LLM, and spend is an authorized act (§11.2): "
                      "confirm the ceiling by including spend_approval"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    counters.incr("featuregen.feature_run.prepare_code.requested" if created
                  else "featuregen.feature_run.prepare_code.deduplicated")
    log("featuregen.feature_run.prepare_code", run_id=run_id, job_id=job_id, created=created,
        members=len(body.option_ids))
    return {
        "job_id": job_id,
        "created": created,
        # Named, never silent: this build's population, cadence and promise were declared for the
        # job named here, and a person is entitled to see whose declaration they just continued.
        "declaration_source_job_id": source.job_id,
        # The run, READ BACK after the write rather than patched from the request — the jobs list
        # now carries the new row, and a client that trusted an echo would render a job the store
        # might not have accepted.
        "run": run_detail(conn, identity, run_id),
        "detail": ("the job was recorded; the worker drives it — watch this run, or the "
                   "generation workspace" if created else
                   "this exact request is already live; its job is the answer, and nothing was "
                   "queued or spent again"),
    }


class RetryAuthoringIn(BaseModel):
    """WHICH recorded attempt to buy again. Nothing else, and the omission is the design.

    The candidate is the ATTEMPT's — resolved from this run's own history — so a body cannot aim
    one run's retry at another run's candidate, and cannot name a considered revision this run does
    not own. The ceiling is not here either: a retry rides the ceiling its GOVERNANCE APPROVAL
    already confirmed (§11.2, and 1103's spend authorization is NOT NULL), so a ceiling collected
    at click time would be a second, unapproved one.
    """

    model_config = ConfigDict(extra="forbid")

    formula_draft_id: str = Field(min_length=1)


@router.post("/feature-runs/{run_id}/authoring-retries", status_code=202,
             dependencies=[Depends(require_feature_generate)])
def retry_run_authoring(run_id: str, body: RetryAuthoringIn, conn: _Conn,
                        identity: _Identity) -> dict[str, Any]:
    """Buy a failed formula attempt again — the run page's second write (spec §R4.2).

    `202`: a draft is REQUESTED, not authored. The worker drives it, and the refreshed run detail in
    the answer is where it will be watched — Task 2's fold shows the new attempt as the candidate's
    current reading and the failure as the history that got it there.

    **THIS ROUTE IS AN ADAPTER, NOT A SECOND AUTHORITY**, exactly as `prepare-code` is. The
    approval, the ceiling, the tombstones and the money guard all belong to
    `request_draft_for_candidate` — the same function `POST /considered-revisions/.../formula-drafts`
    calls — and its typed refusals are re-raised here with the statuses that route already answers
    with.

    ▲ **THE EXHAUSTED-CEILING DIVERGENCE THIS PARAGRAPH ONCE STATED IS RETIRED.** The request
    seam now refuses a mint whose approved ceiling cannot cover ONE per-call worst-case
    reservation (`DraftCeilingExhausted` → 409 `COST_AUTHORIZATION_EXHAUSTED`) BEFORE consuming
    the coupon — so both doors refuse the same case with the same code, and this route's
    pre-flight is an advisory that agrees by construction: the SAME pick (the coupon the mint
    would consume, dead money skipped — `approved_ceiling_for` serves only the no-coupon doors
    now) and the SAME bar (`llm_spend.per_call_worst_case`, the one arithmetic all three
    surfaces call).

    THREE REPORTING differences remain, all reports rather than purchases and all in the
    conservative direction: on a database without the 1103/1105 substrate this door degrades to
    `RETRY_SUBSTRATE_ABSENT` where the legacy door would raise; where a live draft holds the
    identity this door answers 409 `RETRY_ATTEMPT_ALREADY_LIVE` where the legacy door answers
    202 with `created: false`; and where the frozen record can no longer name the candidate this
    door answers 409 `RETRY_CANDIDATE_UNRESOLVABLE` where the legacy door raises
    `CandidateUnavailable` into a 422 with a bare-string body. None mints, consumes, or writes
    anything on either side.

    THE ORDER OF THE GATES, and why each is where it is:

    1. **§11 object policy first.** `run_detail` returns None for a run that does not exist AND for
       one this caller may not see, and both become the byte-identical 404 the GET answers with.
    2. **The attempt is the RUN's.** The body names a draft; this run's own attempt history is what
       says whether it is one of its attempts. A draft the history does not carry is refused without
       saying whether it exists elsewhere — a build of the same rule the 404 above enforces.
    3. **The gesture's own availability** — taken WHOLE from the projection's `retryable` /
       `retry_blockers`, which is the same derivation the run page draws the control from. §7
       [R3.1]: a control offered over an entrance that refuses is a false rail with a click in it,
       and re-deciding it here in words of this file's own is precisely the drift that rule exists
       to prevent.
    4. **Everything else is the service's**, unchanged and un-second-guessed.

    ▲ **NO SECOND CONFIRM.** The exception WAS the approval moment: somebody with `governance:
    confirm` approved this regeneration and confirmed its cost, durably, against the exact identity
    this mint will produce. A modal here would ask the engineer to re-agree to a decision that is
    not theirs, and §11.2's own rule is that a modal is not a money guard.
    """
    from featuregen.aggregates.ids import mint_id
    from featuregen.overlay.upload.formula_draft_service import (
        AuthoringRefused,
        CandidateUnavailable,
        NotAFormulaCandidate,
        NotAnAnswerAtRequest,
        RetiredAtRequest,
        StrategyRefused,
        request_draft_for_candidate,
    )
    from featuregen.overlay.upload.semantic_eligibility_reasons import (
        FORMULA_DRAFT_NOT_AN_ANSWER,
        FORMULA_DRAFT_RETIRED,
    )
    from featuregen.runs.projection import RETRY_ATTEMPT_ALREADY_LIVE

    detail = run_detail(conn, identity, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Not Found")

    attempt = next((row for row in detail["authoring"]["history"]
                    if row["formula_draft_id"] == body.formula_draft_id), None)
    if attempt is None:
        raise _refusal(
            RUN_ATTEMPT_NOT_RECORDED,
            "this run has no such formula attempt, so there is nothing here to buy again — a "
            "retry is a retry OF something this run recorded",
            formula_draft_id=body.formula_draft_id)
    if not attempt["retryable"]:
        blockers = attempt["retry_blockers"]
        if not blockers:
            # No blockers means the QUESTION was never asked of this row, not that it was asked and
            # refused — so the refusal has to say which, rather than quoting an empty list.
            raise _refusal(
                RUN_ATTEMPT_IS_NOT_A_FAILURE,
                f"this attempt is {attempt['state']}: it bought an answer, or is still buying one, "
                f"and it holds the formula identity while it does. Retry answers a FAILED or "
                f"CANCELLED attempt; asking another opinion of a live one needs the identity to "
                f"move, and that surface does not exist yet",
                state=attempt["state"])
        # VERBATIM, code and sentence: the greyed-out control and this 409 are one refusal.
        raise _refusal(blockers[0]["code"], blockers[0]["detail"],
                       blockers=[b["code"] for b in blockers])

    try:
        result = request_draft_for_candidate(
            conn, revision_id=attempt["considered_revision_id"],
            option_id=attempt["option_id"], formula_draft_id=mint_id("fd"),
            requested_by=identity.subject,
            # The database's clock, so two application instances cannot disagree about when — the
            # formula-draft route's own rule, and the value the store folds into its ordering.
            now=str(conn.execute("SELECT now()").fetchone()[0]))
    except CandidateUnavailable as exc:
        raise HTTPException(
            status_code={"unknown_revision": 404,
                         "option_not_in_revision": 422}.get(exc.kind, 409),
            detail=exc.detail) from exc
    except NotAFormulaCandidate as exc:
        raise _refusal(exc.code, "this candidate is not a deterministic formula, so no formula "
                                 "can be drafted for it",
                       formula_strategy=str(exc.strategy)) from exc
    except StrategyRefused as exc:
        raise _refusal(exc.blockers[0],
                       "the authoring strategy could not be resolved for this candidate",
                       blockers=list(exc.blockers)) from exc
    except AuthoringRefused as exc:
        raise _refusal(exc.blockers[0] if exc.blockers else "AUTHORING_REFUSED",
                       "the authoring decision refused; nothing was recorded or spent",
                       blockers=list(exc.blockers)) from exc
    except NotAnAnswerAtRequest as exc:
        # Reachable only through a race with a consumer of the same coupon — the gate above already
        # answered it — and the statuses must still be the formula-draft route's.
        raise _refusal(FORMULA_DRAFT_NOT_AN_ANSWER, str(exc)) from exc
    except RetiredAtRequest as exc:
        raise _refusal(FORMULA_DRAFT_RETIRED, str(exc)) from exc

    if not result.created:
        # ▲ THE MONEY GUARD HAD THE LAST WORD. 1107's partial index refused the INSERT because an
        # answer — or a purchase in flight — already holds this identity, and `request_draft`
        # returned that row without consuming any coupon. Reporting it as a new attempt would
        # describe a purchase that did not happen, so it is the same refusal the page renders.
        raise _refusal(
            RETRY_ATTEMPT_ALREADY_LIVE,
            f"draft {result.formula_draft_id} already holds this formula identity, so nothing was "
            f"minted and nothing was spent — watch that attempt instead",
            formula_draft_id=result.formula_draft_id)

    counters.incr("featuregen.feature_run.authoring_retry.requested")
    log("featuregen.feature_run.authoring_retry", run_id=run_id,
        retried_formula_draft_id=body.formula_draft_id,
        formula_draft_id=result.formula_draft_id)
    return {
        "formula_draft_id": result.formula_draft_id,
        "created": True,
        # The method the SERVER chose and the reasons it recorded — a client never sends a strategy,
        # it reads the one that was resolved.
        "formula_strategy": str(result.strategy),
        "strategy_warnings": list(result.warnings),
        # The run, READ BACK after the write rather than patched from the request: the attempt table
        # now carries both rows, and a client that trusted an echo would render an attempt the store
        # might not have accepted.
        "run": run_detail(conn, identity, run_id),
        "detail": ("the retry was recorded; a worker authors it — watch this run's attempts. The "
                   "failure stays on the record, because what was tried is part of what happened"),
    }

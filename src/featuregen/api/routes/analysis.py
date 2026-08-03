"""The data agent over HTTP: a question in, a checked plan out.

Seven read models existed with no surface — `retrieve_candidates`, `extract_intent`,
`clarifications_for`, `apply_answer`, `ground_analysis_plan`, `plan_to_execution_ir`, `preview` — and
a read model nobody can call is the same inert mechanism this programme has found six times already.
These are the routes.

**Nothing here executes, and one thing here WRITES.** `POST /analysis/plan` retrieves, extracts,
grounds and previews; it never runs the statement. That split is deliberate and not merely cautious:
execution needs an `ExecutionInputs` this deployment cannot yet build — the catalog records no
physical database and there is no connection registry — so a route that promised to run would be
promising something impossible. The preview reports exactly which piece is missing.

No WAREHOUSE data is touched. The CATALOG is written twice, and both are disclosed rather than
implied: a refusal records a learning gap (below), and — with
`FEATUREGEN_SOURCE_TEMPORAL_SELECTION` on — a SELECTION persists the winner's physical binding
revision. That second write is a content-addressed catalog ADDRESS, not a decision: it is the row a
later decision, observation or snapshot points at when it names `pbr_...`, it is idempotent, it
happens only for the dataset that was selected, and re-deriving it produces the same id. With the
flag off nothing is written at all.

**`feature:generate`, not `catalog:read`.** Planning dispatches an LLM call against catalog metadata
on the caller's behalf — the same class of action as the feature-generation routes next door. The
caller's identity is threaded into `extract_intent`, which writes the `llm_call` record, so the
dispatch is attributed to the human who asked rather than to a service actor. That sentence was
here before the record was, and was false: the first version called the raw driver and audited
nothing.

**Read scope is the caller's.** Retrieval prunes candidates by `identity.role_claims`, so a column
the caller may not see is never offered to a model on their behalf — and here that set becomes prompt
text, which is the case the governed read-scope fix exists for.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from featuregen.analysis.assembly import first_unmet_requirement
from featuregen.analysis.clarify import (
    ClarificationError,
    apply_answer,
    clarifications_for_codes,
)
from featuregen.analysis.execution import ExecutionInputs
from featuregen.analysis.grounding import (
    PRODUCTION_TIER,
    ground_analysis_plan,
    plan_cutoff_value_ref,
    record_selection_gaps,
)
from featuregen.analysis.intent import (
    AnalysisIntentInputV2,
    IntentUnavailable,
    extract_intent,
)
from featuregen.analysis.preview import preview
from featuregen.analysis.retrieval import (
    Retrieval,
    RetrievalBudget,
    catalog_snapshot_id,
    record_retrieval_gap,
    retrieve_candidates,
    stable_analysis_request_id,
)
from featuregen.data_agent.binding_store import resolve_binding
from featuregen.data_agent.eligibility_store import resolve_eligibility
from featuregen.data_agent.connection import ConnectionError_
from featuregen.api.deps import get_conn, get_identity, get_llm, require_feature_generate
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.source_selection import SELECTION_POPULATION_UNDECLARED

#: Codes this MODULE names in its own fallback. Everything else a caller sees comes from
#: `analysis.assembly`, and a test asserts the route surfaces codes absent from here — the
#: evidence that the enumeration is not maintained in two places.
BLOCKED_ROUTE_CODES: frozenset[str] = frozenset({"EXECUTION_INPUTS_ABSENT"})

logger = logging.getLogger(__name__)

router = APIRouter()


class RetrievalRefused(Exception):
    """The question matched no readable catalog column — the ONE refusal that WRITES on its way out.

    Not an `HTTPException`, and deliberately so. `get_conn` rolls the request transaction back on any
    exception that leaves the handler, so raising the 422 discarded the learning gap recorded a line
    earlier — the store's only production producer, reverted by the very refusal that produced it.
    Both routes convert this to the same `JSONResponse` FastAPI's own handler would have built, so
    the wire format is unchanged and the transaction commits. Same idiom, same reason, as
    `routes/assets.py`'s field-correction denial, which returns rather than raises so its
    `COMMAND_DENIED` audit row commits."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _refusal_response(exc: RetrievalRefused) -> JSONResponse:
    """Byte-identical to what `HTTPException(422, detail=...)` produces — same status, same body."""
    return JSONResponse(status_code=422, content={"detail": exc.detail})


def _missing_context_of(retrieval: Retrieval) -> tuple[str, ...]:
    """The union of the closed missing-context codes across the offered set's bundles.

    Deduped and sorted, because the model is being told what this VIEW does not carry, not how
    often each thing is absent — a frequency here would read as a coverage metric, which the
    vocabulary's own contract forbids."""
    codes: set[str] = set()
    for entry in retrieval.context_bundles:
        codes.update(entry.get("missing_context", ()))
    return tuple(sorted(codes))
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


class PlanIn(BaseModel):
    question: str = Field(min_length=1)
    #: Cap on how much catalog reaches the prompt. Exposed because the right value is a deployment
    #: judgement, not a constant — but bounded here so a caller cannot ask for the whole catalog.
    max_columns: int = Field(default=60, ge=1, le=200)


class AnswerIn(BaseModel):
    question: str = Field(min_length=1)
    code: str
    chosen: list[str] = Field(default_factory=list)
    max_columns: int = Field(default=60, ge=1, le=200)


def _ground(conn, plan, identity: IdentityEnvelope, question: str):
    """Ground one plan with this route's OWN context, and record what it refuses.

    **The tier is PRODUCTION, and it is passed explicitly.** This surface plans questions people
    ask about the bank's real data; nothing here is a sandbox experiment. Saying it rather than
    relying on a default is the point of the default having moved: a route that names its tier
    cannot have the governance posture changed underneath it by a signature edit.

    **The cutoff ref comes from the PLAN**, through `plan_cutoff_value_ref` — the instant its
    windows are measured back from is the report cutoff the dimension row rule reads. A plan that
    expresses no time context has none to give, and a row rule that needs one then refuses
    VISIBLY rather than resolving to "whatever is there now".
    """
    grounded = ground_analysis_plan(
        conn, plan, roles=identity.role_claims, execution_tier=PRODUCTION_TIER,
        cutoff_value_ref=plan_cutoff_value_ref(plan), recorded_by=identity.subject)
    _record_selection_gaps(conn, grounded, question=question, roles=identity.role_claims)
    return grounded


def _record_selection_gaps(conn, grounded, *, question: str, roles) -> None:
    """The ONE production caller of `record_selection_gaps`, at the surface refusals reach a person.

    The request id is DERIVED from the question and the catalog snapshot (`record_selection_gaps`
    does that itself), so asking a blocked question three times is ONE thing to decide — the same
    property the retrieval gap above has, and for the same reason.

    Fail-soft, exactly like the retrieval gap: a learning write must never turn a planned question
    into a 500. Unlike that one it does NOT need a returned-rather-than-raised dance, because this
    path answers 200 and the transaction reaches its commit on its own.
    """
    selections = grounded.selections
    if selections is None or not selections.refusals:
        return
    try:
        record_selection_gaps(
            conn, selections, question=question,
            dependency_snapshot_id=catalog_snapshot_id(conn, roles=roles),
            now=datetime.now(UTC))
    except Exception:   # noqa: BLE001
        logger.warning("could not record a selection learning gap", exc_info=True)


def _serialize_selection(selections) -> dict | None:
    """The Release-B source/row decisions, as the caller sees them. `None` while the flag is off.

    Shown even when it refuses — especially then. "Which copy served this, and which of its rows"
    is half of what makes an answer explainable (§5.7), and a preview that showed only the
    resolved half would report the absence of a row rule as silence.
    """
    if selections is None:
        return None
    return {
        "resolved": selections.resolved,
        "sources": [{"need_role": s.need.need_role.value,
                     "dataset_ref": s.selected_dataset_ref,
                     "selection_basis": s.selection_basis.value,
                     "authority_basis": s.authority_basis.value,
                     "considered": [{"dataset_ref": c.dataset_ref,
                                     "disposition": c.disposition.value,
                                     "reason_codes": list(c.reason_codes)}
                                    for c in s.considered_candidates]}
                    for s in selections.source_selections],
        "rows": [{"dataset_ref": r.dataset_logical_ref,
                  "selection_kind": r.selection_kind.value,
                  "cutoff_value_ref": r.cutoff_value_ref}
                 for r in selections.row_selections],
        "refusals": [{"code": r.code, "subjects": list(r.subject_refs), "detail": r.detail}
                     for r in selections.refusals],
        "warnings": list(selections.warnings),
    }


def _clarification_codes(extraction, selections) -> tuple[str, ...]:
    """The model's own abstentions PLUS the selector's refusals, as one set of questions.

    Rendered through the single `clarifications_for_codes` ordering rather than appended after it,
    or the population would stop outranking the row questions the moment a refusal joined the list.

    `SELECTION_POPULATION_UNDECLARED` is dropped when the model already raised `population`: they
    are the same decision in the same words (`clarify` builds one from the other), and asking it
    twice under two codes is two questions for one thing to decide.
    """
    codes = list(extraction.unresolved)
    for code in (selections.refusal_codes if selections is not None else ()):
        if code == SELECTION_POPULATION_UNDECLARED and "population" in codes:
            continue
        codes.append(code)
    return tuple(codes)


def _serialize_clarifications(extraction, retrieval, grounded, *, answered: str = "") -> list[dict]:
    selections = grounded.selections
    return [
        {"code": c.code, "question": c.question, "optional": c.optional,
         "allows_multiple": c.allows_multiple,
         "options": [{"value": o.value, "label": o.label} for o in c.options]}
        for c in clarifications_for_codes(
            _clarification_codes(extraction, selections), retrieval.candidates,
            refusals=(selections.refusals if selections is not None else ()))
        if c.code != answered]


def _serialize_preview(view) -> dict:
    return {
        "question": view.question, "entity": view.entity, "measure": view.measure,
        "comparison": view.comparison, "dimensions": list(view.dimensions),
        "periods": [{"label": p.label, "partitions": list(p.partitions)} for p in view.periods],
        "findings": [{"code": f.code, "subject": f.subject, "detail": f.detail,
                      "clears_when": f.clears_when} for f in view.findings],
        "sql": view.sql, "plan_hash": view.plan_hash,
        "runnable": view.runnable,
        "rests_on_unconfirmed_facts": view.rests_on_unconfirmed_facts,
        "blocked_by": ({"code": view.blocked_by[0], "subject": view.blocked_by[1]}
                       if view.blocked_by else None),
    }


def _plan_for(conn, question: str, identity: IdentityEnvelope, client: LLMClient,
              max_columns: int):
    """Retrieve → extract → ground. Shared by both routes so a clarification is applied to a plan
    built exactly as the original was; rebuilding it differently is how an answer ends up folded into
    a plan the user never saw."""
    now = datetime.now(UTC)
    retrieval = retrieve_candidates(conn, question, now=now, roles=identity.role_claims,
                                    budget=RetrievalBudget(max_columns=max_columns))
    if retrieval.is_empty:
        # A typed refusal AND a recorded learning gap. The gap store had no production producer at
        # all (`record_gap` was reachable only from the caller-less `run_analysis`), so
        # `GET /learning/gaps` read a table nothing populated. A question the catalog has no word
        # for is the canonical actionable gap, and this path is reached on every planning request.
        # Fail-soft: a learning write must never turn a clear 422 into a 500.
        #
        # The request id is DERIVED, never minted. `record_gap` dedupes on (request, gap, snapshot)
        # and an `areq-{uuid4}` per call made every row unique by construction, so three identical
        # unanswered questions wrote three identical gaps and demand read 3 for one thing to decide.
        # The snapshot is computed ONCE and threaded into both the id and the event, so the two can
        # never disagree about which catalog state this refusal was reached under.
        try:
            snapshot = catalog_snapshot_id(conn, roles=identity.role_claims)
            record_retrieval_gap(
                conn, question, roles=identity.role_claims,
                analysis_request_id=stable_analysis_request_id(
                    question, dependency_snapshot_id=snapshot),
                now=now, dependency_snapshot_id=snapshot)
        except Exception:   # noqa: BLE001
            logger.warning("could not record a retrieval learning gap", exc_info=True)
        # RAISED, this 422 would take the learning write with it: `get_conn` rolls the request
        # transaction back on ANY exception leaving the handler, so the row written one line above
        # would never commit and the store would go on having no production producer. Returned, the
        # response is byte-identical (FastAPI's own `HTTPException` handler emits exactly this
        # body) and the transaction reaches its commit.
        raise RetrievalRefused(retrieval.empty_reason)
    try:
        extraction = extract_intent(
            conn, client, question,
            # The VERSIONED input contract (semantic Task 9). Same metadata block, new keys: the
            # offered refs stay exactly where they were.
            AnalysisIntentInputV2(
                candidates=retrieval.candidates,
                context=retrieval.context_bundles,
                missing_context=_missing_context_of(retrieval)),
            actor=identity)
    except IntentUnavailable as exc:
        # 422, not 500: the question could not be expressed, which is about the request rather than
        # a fault in the service.
        raise HTTPException(status_code=422, detail=str(exc)) from None
    grounded = _ground(conn, extraction.plan, identity, question)
    return retrieval, extraction, grounded


@router.post("/analysis/plan", dependencies=[Depends(require_feature_generate)])
def plan(body: PlanIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """A question, planned and previewed. Never executed — see the module docstring.

    With the selection flag ON this also PINS the binding revision of each dataset it selects
    (`grounding`'s one write) and records each selection refusal as a learning gap. The pin is a
    catalog address rather than a decision — content-addressed, idempotent, winner only — so a
    planning request that is never acted on leaves nothing that needs revoking.
    """
    try:
        retrieval, extraction, grounded = _plan_for(
            conn, body.question, identity, client, body.max_columns)
    except RetrievalRefused as exc:
        return _refusal_response(exc)
    view = _previewed(conn, grounded)
    return {
        "preview": _serialize_preview(view),
        # WHICH COPY served each need, and WHICH of its rows — or the typed refusal saying nobody
        # has decided yet. `None` while the selection flag is off.
        "selection": _serialize_selection(grounded.selections),
        "clarifications": _serialize_clarifications(extraction, retrieval, grounded),
        # Truncation is reported, never silent: a non-zero count means the plan rests on a narrower
        # view of the catalog than exists. PER LEG (D12.2) as well as in aggregate — one number
        # cannot say whether relevance narrowed the answer or a link budget did, and the two call
        # for different actions from the person reading it.
        "retrieval": {"tables_considered": list(retrieval.tables_considered),
                      "dropped_columns": retrieval.dropped_columns,
                      "legs": [leg.as_dict() for leg in retrieval.legs],
                      # The CONTROLLED vocabulary leg 3 expanded on — platform tokens, never the
                      # user's words, so showing them explains the answer without echoing input.
                      "expansion_terms": list(retrieval.expansion_terms),
                      "context_bundles": len(retrieval.context_bundles)},
    }


@router.post("/analysis/clarify", dependencies=[Depends(require_feature_generate)])
def clarify(body: AnswerIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Fold one clarification answer into the plan and re-preview.

    The answer is re-validated against the candidates rather than trusted: a client is the layer
    least able to guarantee that what came back is what it offered.
    """
    try:
        retrieval, extraction, grounded = _plan_for(
            conn, body.question, identity, client, body.max_columns)
    except RetrievalRefused as exc:
        return _refusal_response(exc)
    try:
        answered = apply_answer(grounded.plan, body.code, tuple(body.chosen),
                                retrieval.candidates)
    except ClarificationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    regrounded = _ground(conn, answered, identity, body.question)
    view = _previewed(conn, regrounded)
    return {
        "preview": _serialize_preview(view),
        "selection": _serialize_selection(regrounded.selections),
        "clarifications": _serialize_clarifications(
            extraction, retrieval, regrounded, answered=body.code),
    }


def _previewed(conn, grounded):
    """Preview, with the blocked reason sharpened by what the registry actually knows."""
    from dataclasses import replace as _replace

    view = preview(grounded, _execution_inputs_or_none(conn, grounded.plan))
    if view.blocked_by and view.blocked_by[0] == "EXECUTION_INPUTS_ABSENT":
        view = _replace(view, blocked_by=_binding_hint(conn, grounded.plan))
    return _replace(view, findings=view.findings + _eligibility_findings(conn, grounded.plan))


def _eligibility_findings(conn, plan) -> tuple:
    """Disclose an eligibility policy nobody has agreed to.

    Usable before confirmation is the product rule; passing SILENTLY is not. "Which rows count" is
    the definition every number in the answer rests on, so an unconfirmed one is a finding for the
    same reason an unconfirmed join identity is.
    """
    from featuregen.analysis.preview import FindingPreview

    source, table = _source_and_table(plan.base_table_ref)
    stored = resolve_eligibility(conn, catalog_source=source, table=table)
    if stored is None or stored.confirmed:
        return ()
    return (FindingPreview(
        code="ELIGIBILITY_UNCONFIRMED", subject=f"{source}::{table}",
        detail=f"which rows count was proposed by {stored.proposed_by!r} and confirmed by nobody",
        clears_when="a human confirms the eligibility policy for this table"),)


def _execution_inputs_or_none(conn, plan) -> ExecutionInputs | None:
    """Assemble what CAN be assembled, and return None when something real is missing.

    The binding registry (migration 1037) supplies the physical address the catalog cannot — a
    database, and the connection authorized to read it. What it cannot supply is the rest of
    `ExecutionInputs`: a population spine distinct from the event table, which `AnalysisPlanV1`
    structurally cannot express, and an eligibility policy, which no store yet holds.

    So this still returns None — but only after LOOKING, so `_blocked_reason` can say whether the
    operator needs to bind a table or make a decision. Returning None without checking would leave
    both cases reading as one, and "not configured" and "cannot be expressed" call for different
    people.
    """
    return None


def _binding_hint(conn, plan) -> tuple[str, str]:
    """The first unmet requirement, from the single enumeration in `analysis.assembly`.

    This route used to keep its own short list, and the list was WRONG — it stopped at four gaps and
    omitted attribution and join evidence. A hand-maintained "what's still missing" drifts the moment
    a store lands; asking the enumerator cannot.

    `granularity` is deliberately not supplied: nothing records whether a partition column names
    months or days, so the honest report is that the calendar is unknown rather than a month assumed.
    """
    return first_unmet_requirement(conn, plan, reference=datetime.now(UTC)) or (
        "EXECUTION_INPUTS_ABSENT", plan.base_table_ref)


def _source_and_table(table_ref: str) -> tuple[str, str]:
    """``source::[schema.]table`` -> (source, table), mirroring `grounding._parse`."""
    source, _, rest = table_ref.partition("::")
    parts = [p for p in rest.split(".") if p]
    return source.strip().lower(), (parts[-1] if parts else "")

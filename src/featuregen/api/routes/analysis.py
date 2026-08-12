"""The data agent over HTTP: a question in, a checked plan out.

Seven read models existed with no surface — `retrieve_candidates`, `extract_intent`,
`clarifications_for`, `apply_answer`, `ground_analysis_plan`, `plan_to_execution_ir`, `preview` — and
a read model nobody can call is the same inert mechanism this programme has found six times already.
These are the routes.

**PLANNING NEVER EXECUTES, AND EXECUTION NEVER PLANS.** `POST /analysis/plan` retrieves, extracts,
grounds, previews and — behind `FEATUREGEN_SOURCE_TEMPORAL_SELECTION` — SEALS; it never runs the
statement. `POST /analysis/execute` runs one SEALED plan by its identity and never re-plans one.
That split is the whole design: a caller who handed a question to an execute route would be running
decisions nobody previewed, made by a model dispatched a second time under state that may have
moved. (Until Release-B Task 9 this module said "nothing here executes" and gave the reason that
execution inputs could not be built — no physical database, no connection registry. Both are now
built: the binding registry supplies the address, the Release-B declaration supplies the
population, and the eligibility store supplies which rows count.)

WAREHOUSE data is touched by ONE route, `/analysis/execute`, and only through the governed engine
provider — whose shipped default REFUSES, because running against the bank's warehouse is a
separate approval gate. The CATALOG is written by planning in three disclosed ways: a refusal
records a learning gap (below); with the selection flag on a SELECTION persists the winner's
physical binding revision; and a resolved, executable plan is SEALED into `structured_result`. The
binding write is a content-addressed catalog ADDRESS, not a decision — the row a later decision,
observation or snapshot points at when it names `pbr_...`, idempotent, winner only. The seal is
content-addressed on the plan's own identity, so re-planning the same thing writes nothing new.
With the flag off none of the three happens.

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
from featuregen.analysis.execution import (
    BridgeRefusal,
    ExecutionInputs,
    plan_to_execution_ir,
)
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
from featuregen.api.deps import (
    get_analysis_engine,
    get_conn,
    get_identity,
    get_llm,
    require_feature_generate,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.data_agent.connection import ConnectionError_
from featuregen.data_agent.eligibility_store import resolve_eligibility
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.source_selection import (
    SELECTION_POPULATION_UNDECLARED,
    source_temporal_selection_enabled,
)

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


def _serialize_selection(conn, selections, *, roles=()) -> dict | None:
    """The Release-B source/row decisions, READ-SCOPED for this caller. `None` with the flag off.

    Delegates to `analysis.explain`, which owns the rule that matters here: the decision is
    RECORDED whole (rule 9) and RENDERED narrow — a candidate this caller may not see becomes an
    opaque count and never a name, because "there is a better copy you may not see" is an existence
    oracle over a catalog they were never granted.
    """
    from featuregen.analysis.explain import render_selection

    return render_selection(conn, selections, roles=roles)


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
    view, sealed = _previewed(conn, grounded, identity=identity)
    return {
        "preview": _serialize_preview(view),
        # WHICH COPY served each need, and WHICH of its rows — or the typed refusal saying nobody
        # has decided yet. `None` while the selection flag is off.
        "selection": _serialize_selection(conn, grounded.selections,
                                          roles=identity.role_claims),
        # The identity a caller executes this exact plan by. `None` when nothing was sealed —
        # every flag-off request, and every plan that could not decide where its data comes from.
        "sealed_plan_hash": sealed,
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
    view, sealed = _previewed(conn, regrounded, identity=identity)
    return {
        "preview": _serialize_preview(view),
        "selection": _serialize_selection(conn, regrounded.selections,
                                          roles=identity.role_claims),
        "sealed_plan_hash": sealed,
        "clarifications": _serialize_clarifications(
            extraction, retrieval, regrounded, answered=body.code),
    }


class ExecuteIn(BaseModel):
    #: The SEALED plan identity `POST /analysis/plan` returned. Deliberately not a question: a
    #: question would have to be re-planned (another LLM dispatch, another set of decisions), and
    #: the caller would then be running something they never previewed.
    plan_hash: str = Field(min_length=32, max_length=128)


#: The engine PROVIDER, injected the same way `_Conn` is — so the suite substitutes the pilot's
#: fixture engine through `dependency_overrides` rather than the route reaching for a driver.
_EngineProvider = Annotated[object, Depends(get_analysis_engine)]


@router.post("/analysis/execute", dependencies=[Depends(require_feature_generate)])
def execute(body: ExecuteIn, conn: _Conn, identity: _Identity,
            engine_provider: _EngineProvider) -> dict:
    """Run ONE sealed plan, after revalidating every pin it rests on.

    **Flag-gated, and 404 while the flag is off** — the surface is hidden rather than forbidden, so
    a flag-off API is byte-identical to the one that shipped (the `dataset_policies.py` convention).

    **READ SCOPE PRECEDES EVERY 409.** The caller's readability of every dataset the plan names is
    checked immediately after replay — before the plan's engine connection is resolved and before
    the engine provider is asked for anything. Both of those report on datasets, and a 409 handed
    to someone who may not see them answers "a sealed plan over a catalog you were never granted
    exists here". An unreadable plan gets the SAME body an absent one gets (D11): hidden and
    missing are indistinguishable at the wire.

    **A stale plan refuses and runs nothing.** The refusal names WHAT moved and from which value to
    which, because "this plan is stale" sends the reader nowhere. Nothing is re-derived: running
    today's decisions under yesterday's plan identity is precisely the silent substitution the
    seal exists to prevent.
    """
    from featuregen.analysis.engine import AnalysisEngineUnavailable
    from featuregen.analysis.sealed_execution import SealedRunV1, run_sealed_plan
    from featuregen.analysis.sealed_plan import SealedPlanError
    from featuregen.analysis.sealed_plan_store import (
        caller_may_read_every_dataset,
        replay_sealed_plan,
    )

    if not source_temporal_selection_enabled():
        raise HTTPException(status_code=404, detail="not found")
    record = replay_sealed_plan(conn, body.plan_hash)
    if record is None:
        return _sealed_plan_not_found(body.plan_hash)
    # FIRST, and deliberately ahead of everything that could report on this plan's datasets.
    # `run_sealed_plan` makes the same check per pin, but it ran THIRD: `_engine_connection_for`
    # 409'd naming the event dataset and the provider 409'd naming the connection, both before any
    # scope was read. Same body as absence, so the two cannot be told apart.
    if not caller_may_read_every_dataset(conn, record, roles=identity.role_claims):
        return _sealed_plan_not_found(body.plan_hash)

    engine_connection = _engine_connection_for(conn, record)
    try:
        engine = engine_provider(engine_connection)
    except AnalysisEngineUnavailable as exc:
        # 409, not 503: the deployment is healthy and the plan is fine — what is missing is an
        # approval, which is a state of the world rather than a fault.
        return JSONResponse(status_code=409, content={
            "detail": str(exc),
            "refusal": {"code": exc.code, "subjects": [exc.subject], "detail": str(exc),
                        "sealed": "", "current": ""}})

    try:
        outcome = run_sealed_plan(conn, record, engine=engine, roles=identity.role_claims)
    except SealedPlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not isinstance(outcome, SealedRunV1):
        return JSONResponse(status_code=409, content={
            "detail": outcome.detail, "refusal": outcome.as_dict()})
    return {
        "plan_hash": outcome.plan_hash,
        # RESULT PROVENANCE: the exact decision refs this answer rests on, carried WITH the answer.
        # A number whose sources have to be looked up elsewhere is a number nobody can defend.
        "provenance": _serialize_provenance(conn, record),
        "rows": [{"key": r.key, "previous_count": r.previous_count,
                  "current_count": r.current_count, "decreased": r.decreased,
                  "dimensions": dict(r.dimensions)} for r in outcome.rows],
        "row_count": len(outcome.rows),
    }


def _sealed_plan_not_found(plan_hash: str) -> JSONResponse:
    """The ONE not-found body this route has, for BOTH "nothing sealed that" and "you may not see
    what did". One function rather than two literals, because two would eventually drift into two
    distinguishable answers — and the whole point is that they cannot be told apart."""
    from featuregen.analysis.sealed_plan import SEALED_PLAN_ABSENT

    return JSONResponse(status_code=404, content={
        "detail": "no sealed plan has that identity",
        "refusal": {"code": SEALED_PLAN_ABSENT, "subjects": [plan_hash], "detail": "",
                    "sealed": "", "current": ""}})


def _engine_connection_for(conn, record):
    """The governed connection the plan's EVENT source is addressed through.

    One connection per plan by construction: `resolve_table` routes a whole catalog through one
    declared engine, and every dataset in a sealed plan comes from the same catalog. Taking it from
    the event source rather than a deployment constant is what keeps the executor pointed at the
    cluster the decisions were made against.

    **NO 409 FROM HERE NAMES A DATASET.** Read scope is checked before this is called, so a caller
    who reaches it may see the plan's datasets — but the refusal is about ADDRESSING, and the ref
    adds nothing a person could act on while making the message one edit away from being an
    existence oracle if the ordering above were ever changed back.
    """
    from featuregen.data_agent.binding_store import resolve_table
    from featuregen.overlay.upload.object_ref import parse_ref

    events = next((s for s in record.sources if s.need_role == "event_source"), None)
    if events is None:
        raise HTTPException(status_code=409, detail="the sealed plan names no event source")
    source, _schema, table, _column = parse_ref(events.dataset_ref)
    try:
        resolved = resolve_table(conn, catalog_source=source, table=table)
    except ConnectionError_ as exc:
        # The CODE and the connection's own words: a withdrawn grant is a governance answer an
        # operator acts on, and it names a connection rather than a dataset.
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if resolved is None:
        raise HTTPException(
            status_code=409,
            detail="a sealed dataset can no longer be addressed by this deployment")
    return resolved[1]


def _serialize_provenance(conn, record) -> dict:
    """The seven pins, as the answer's own provenance."""
    return {
        "contract_version": record.contract_version,
        "sources": [{"need_role": s.need_role, "dataset_ref": s.dataset_ref,
                     "dataset_profile_hash": s.dataset_profile_hash,
                     "serving_policy_revision_id": s.serving_policy_revision_id,
                     "source_selection_hash": s.source_selection_hash,
                     "binding_revision_id": s.binding_revision_id}
                    for s in record.sources],
        "rows": [{"dataset_ref": r.dataset_ref,
                  "dataset_profile_hash": r.dataset_profile_hash,
                  "temporal_policy_revision_id": r.temporal_policy_revision_id,
                  "row_selection_hash": r.row_selection_hash,
                  "selection_kind": r.selection_kind}
                 for r in record.rows],
        # PIN 7. The definition of WHICH ROWS COUNT this number was measured under, plus whether a
        # human ever agreed to it: the planning preview already discloses `ELIGIBILITY_UNCONFIRMED`
        # as a finding, and an ANSWER that dropped the disclosure would be the finding travelling
        # only as far as the preview. Usable before confirmation is the product rule; passing
        # silently is not.
        "eligibility": _eligibility_provenance(conn, record),
        "warnings": list(record.decisions.warnings),
    }


def _eligibility_provenance(conn, record) -> dict | None:
    """The sealed eligibility pin, and the CURRENT confirmation state of the policy it names.

    The two are deliberately different kinds of fact and both are needed. `policy_hash` is what the
    plan was sealed under and revalidation has just proved unmoved; `confirmed` is read now, because
    a person confirming a proposal between the seal and the run is exactly the change a reader wants
    reflected, and it is not drift.
    """
    sealed = record.eligibility
    if sealed is None:
        return None
    source, table = _source_and_table(sealed.dataset_ref)
    stored = resolve_eligibility(conn, catalog_source=source, table=table)
    return {
        "dataset_ref": sealed.dataset_ref,
        "policy_hash": sealed.policy_hash,
        "confirmed": bool(stored is not None and stored.confirmed),
        "status": ("" if stored is not None and stored.confirmed
                   else "ELIGIBILITY_UNCONFIRMED"),
    }


def _previewed(conn, grounded, *, identity: IdentityEnvelope | None = None,
               reference: datetime | None = None):
    """Preview, with the blocked reason sharpened by what the registry actually knows — and, when
    the plan is executable and its selections resolved, SEALED on the way past.

    Sealing here rather than in a separate call is deliberate: the plan a person is shown and the
    plan that can later be executed must be the same one, and a seal taken from a second
    assembly could differ from the preview in any of the six pins. Returns
    ``(view, sealed_plan_hash)``; the hash is ``None`` whenever nothing was sealed, which includes
    every flag-off request and every refused plan.
    """
    from dataclasses import replace as _replace

    inputs = _execution_inputs_or_none(
        conn, grounded, reference=reference,
        roles=(identity.role_claims if identity is not None else ()))
    view = preview(grounded, inputs)
    if view.blocked_by and view.blocked_by[0] == "EXECUTION_INPUTS_ABSENT":
        view = _replace(view, blocked_by=_binding_hint(conn, grounded.plan))
    view = _replace(view, findings=view.findings + _eligibility_findings(conn, grounded.plan))
    return view, _sealed_plan_hash(conn, grounded, inputs, identity=identity)


def _sealed_plan_hash(conn, grounded, inputs, *, identity: IdentityEnvelope | None) -> str | None:
    """Seal the previewed plan, and return the identity a caller executes it by.

    Fail-soft, exactly like the learning writes above: the seal is a record, and a record that
    cannot be written must not turn a planned question into a 500. A caller that gets no hash
    simply cannot execute yet, which is the same state a blocked plan is in.

    **WITH ONE EXCEPTION.** `StructuredResultCorruption` is not a write that failed: it says one
    plan identity already names a DIFFERENT byte sequence. Swallowing it would log a warning,
    return no hash, and leave the caller re-planning against a store that is lying about what was
    approved — the loudest possible fact reported as the quietest possible outcome.
    """
    from featuregen.analysis.sealed_plan import build_execution_ir_v2
    from featuregen.analysis.sealed_plan_store import seal_analysis_plan
    from featuregen.overlay.upload.structured_results import StructuredResultCorruption

    selections = grounded.selections
    if inputs is None or selections is None or not selections.resolved:
        return None
    try:
        ir = plan_to_execution_ir(grounded, inputs)
    except BridgeRefusal:
        # The bridge refused — the plan is not executable, so there is no validated IR to seal and
        # `blocked_by` above already says which decision is owed.
        return None
    try:
        record = seal_analysis_plan(
            conn, build_execution_ir_v2(ir, selections, question=grounded.plan.question),
            sealed_by=(identity.subject if identity is not None else "anonymous"),
            question=grounded.plan.question)
    except StructuredResultCorruption:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("could not seal the analysis plan", exc_info=True)
        return None
    return record.plan_hash


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


def _execution_inputs_or_none(conn, grounded, *, reference: datetime | None = None,
                              roles=()) -> ExecutionInputs | None:
    """Assemble what CAN be assembled, and return None when something real is missing.

    **This used to return None unconditionally**, with a docstring explaining that the rest of
    `ExecutionInputs` could not be supplied: a population spine distinct from the event table, and
    an eligibility policy no store held. Both are now supplied — the population by the Release-B
    declaration or serving policy, the eligibility by its store — so the honest implementation is
    the real one.

    Behind `FEATUREGEN_SOURCE_TEMPORAL_SELECTION`, and it returns None while the flag is off
    WITHOUT READING ANYTHING, so a flag-off preview is byte-identical to the one that shipped.
    A None here still falls through to `_binding_hint`, which names the first unmet requirement
    from the single enumeration in `analysis.assembly` — so "not configured" and "cannot be
    expressed" keep reading as the different things they are.
    """
    from featuregen.analysis.sealed_execution import execution_inputs_for_plan

    if not source_temporal_selection_enabled():
        return None
    return execution_inputs_for_plan(
        conn, grounded, reference=reference or datetime.now(UTC), roles=roles)


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

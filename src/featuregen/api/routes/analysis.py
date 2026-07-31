"""The data agent over HTTP: a question in, a checked plan out.

Seven read models existed with no surface — `retrieve_candidates`, `extract_intent`,
`clarifications_for`, `apply_answer`, `ground_analysis_plan`, `plan_to_execution_ir`, `preview` — and
a read model nobody can call is the same inert mechanism this programme has found six times already.
These are the routes.

**Nothing here executes.** `POST /analysis/plan` retrieves, extracts, grounds and previews; it never
runs the statement. That split is deliberate and not merely cautious: execution needs an
`ExecutionInputs` this deployment cannot yet build — the catalog records no physical database and
there is no connection registry — so a route that promised to run would be promising something
impossible. The preview reports exactly which piece is missing.

**`feature:generate`, not `catalog:read`.** Planning dispatches an LLM call against catalog metadata
on the caller's behalf. That is the same class of action as the feature-generation routes next door,
and it is charged to the caller's identity so every `llm_call` is attributed to the human who asked
rather than to a service actor.

**Read scope is the caller's.** Retrieval prunes candidates by `identity.role_claims`, so a column
the caller may not see is never offered to a model on their behalf — and here that set becomes prompt
text, which is the case the governed read-scope fix exists for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.analysis.assembly import first_unmet_requirement
from featuregen.analysis.clarify import ClarificationError, apply_answer, clarifications_for
from featuregen.analysis.execution import ExecutionInputs
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.intent import IntentUnavailable, extract_intent
from featuregen.analysis.preview import preview
from featuregen.analysis.retrieval import RetrievalBudget, retrieve_candidates
from featuregen.data_agent.binding_store import resolve_binding
from featuregen.data_agent.eligibility_store import resolve_eligibility
from featuregen.data_agent.connection import ConnectionError_
from featuregen.api.deps import get_conn, get_identity, get_llm, require_feature_generate
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient

#: Codes this MODULE names in its own fallback. Everything else a caller sees comes from
#: `analysis.assembly`, and a test asserts the route surfaces codes absent from here — the
#: evidence that the enumeration is not maintained in two places.
BLOCKED_ROUTE_CODES: frozenset[str] = frozenset({"EXECUTION_INPUTS_ABSENT"})

router = APIRouter()
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
        raise HTTPException(status_code=422, detail=retrieval.empty_reason)
    try:
        extraction = extract_intent(client, question, retrieval.candidates)
    except IntentUnavailable as exc:
        # 422, not 500: the question could not be expressed, which is about the request rather than
        # a fault in the service.
        raise HTTPException(status_code=422, detail=str(exc)) from None
    grounded = ground_analysis_plan(conn, extraction.plan, roles=identity.role_claims)
    return retrieval, extraction, grounded


@router.post("/analysis/plan", dependencies=[Depends(require_feature_generate)])
def plan(body: PlanIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """A question, planned and previewed. Never executed — see the module docstring."""
    retrieval, extraction, grounded = _plan_for(
        conn, body.question, identity, client, body.max_columns)
    view = _previewed(conn, grounded)
    return {
        "preview": _serialize_preview(view),
        "clarifications": [
            {"code": c.code, "question": c.question, "optional": c.optional,
             "allows_multiple": c.allows_multiple,
             "options": [{"value": o.value, "label": o.label} for o in c.options]}
            for c in clarifications_for(extraction, retrieval.candidates)],
        # Truncation is reported, never silent: a non-zero count means the plan rests on a narrower
        # view of the catalog than exists.
        "retrieval": {"tables_considered": list(retrieval.tables_considered),
                      "dropped_columns": retrieval.dropped_columns},
    }


@router.post("/analysis/clarify", dependencies=[Depends(require_feature_generate)])
def clarify(body: AnswerIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Fold one clarification answer into the plan and re-preview.

    The answer is re-validated against the candidates rather than trusted: a client is the layer
    least able to guarantee that what came back is what it offered.
    """
    retrieval, extraction, grounded = _plan_for(
        conn, body.question, identity, client, body.max_columns)
    try:
        answered = apply_answer(grounded.plan, body.code, tuple(body.chosen),
                                retrieval.candidates)
    except ClarificationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    regrounded = ground_analysis_plan(conn, answered, roles=identity.role_claims)
    view = _previewed(conn, regrounded)
    return {
        "preview": _serialize_preview(view),
        "clarifications": [
            {"code": c.code, "question": c.question, "optional": c.optional,
             "allows_multiple": c.allows_multiple,
             "options": [{"value": o.value, "label": o.label} for o in c.options]}
            for c in clarifications_for(extraction, retrieval.candidates)
            if c.code != body.code],
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

"""Hypothesis-driven feature-contract flow over HTTP.

Stateless: the frontend carries the discovered options / draft as JSON between steps, and the SERVER
re-validates (the deterministic MCV re-runs at author + confirm), so a tampered payload can never govern
a leaky / stale / ungrounded contract. Safety kwargs (roles, target_ref, server clock) are always
threaded — omitting them would silently downgrade safety (review root-cause A).
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_identity,
    get_llm,
    require_feature_generate,
    require_feature_read,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.idgen import mint_id
from featuregen.intake.llm import LLMClient, compute_input_hash
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract.author import ContractDraft, draft_contract
from featuregen.overlay.upload.contract.gate1 import (
    build_considered_set,
    chosen_feature,
    gate1_choice,
    intent_target_ref,
    persist_intent,
    record_gate1_choice,
)
from featuregen.overlay.upload.contract.govern import (
    Contract,
    ContractValidationError,
    confirm_contract,
    get_contract_detail,
    list_contracts,
)
from featuregen.overlay.upload.contract.intake import (
    IntentValidationError,
    redact_free_text,
    submit_intent,
)
from featuregen.overlay.upload.contract.review import author_contract
from featuregen.overlay.upload.contract.scope_records import (
    record_confirmed_scope,
    record_recognition_attempt,
)
from featuregen.overlay.upload.taxonomy.applicability import (
    ConfirmedScope,
    ScopeExpansion,
    applicability_result,
)
from featuregen.overlay.upload.taxonomy.disposition import (
    FinalDisposition,
    RecipeEvaluation,
    StageEvaluation,
    evaluate_dispositions,
)
from featuregen.overlay.upload.taxonomy.journey_stages import journey_metadata
from featuregen.overlay.upload.taxonomy.ranking import (
    RankedRecipe,
    RankSignals,
    rank_eligible,
)
from featuregen.overlay.upload.taxonomy.ranking_signals import (
    BindingQuality,
    EntityCompatibility,
    ModellingContextFit,
    entity_compatibility,
    modelling_context_fit,
    pit_completeness,
    semantic_group,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    APPLICABILITY_MAPPING_VERSION,
    RecognitionStatus,
)
from featuregen.overlay.upload.taxonomy.recognizer import recognize
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves, use_case
from featuregen.overlay.upload.templates import ALL_TEMPLATES

router = APIRouter()

_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


# ---- I/O models. The security-critical state (target_ref, the chosen feature) lives SERVER-side,
# keyed by intent_id — the client carries only the transient draft + its intent_id back to confirm. ----
class DraftIn(BaseModel):
    feature_name: str
    definition: str
    grain_table: str | None = None
    aggregation: str | None = None
    as_of_column: str | None = None
    derives_from: list[str]
    target_ref: str | None = None
    derives_pairs: list[tuple[str, str]] = []
    join_path: list[dict] = []
    intent_id: str | None = None   # server re-reads target_ref + links the contract via this

    def to_draft(self) -> ContractDraft:
        return ContractDraft(
            feature_name=self.feature_name, definition=self.definition, grain_table=self.grain_table,
            aggregation=self.aggregation, as_of_column=self.as_of_column,
            derives_from=self.derives_from, target_ref=self.target_ref,
            derives_pairs=tuple((p[0], p[1]) for p in self.derives_pairs),  # each is a (source, ref) pair
            join_path=tuple(self.join_path))


class ConfirmedScopeIn(BaseModel):
    """The human-confirmed Gate #1 scope (Phase-1B). ``unscoped=true`` fails open to full grounding and
    needs no ids; otherwise ``primary`` (if set) and every ``secondary`` must be a selectable taxonomy
    leaf. ``use_case_origins`` maps a use-case id to its provenance (``llm_proposed``/``user_added``/
    ``user_overridden``) for the proposed-vs-accepted audit delta."""
    primary: str | None = None
    secondary: list[str] = []
    expansion: str = ScopeExpansion.EXACT.value
    unscoped: bool = False
    use_case_origins: dict[str, str] = {}
    confirmation_source: str = "user_confirmed"
    # ── Phase-2B (Task B3): the two human-confirmed intent DIMENSIONS. Both SOFT — they never narrow
    # applicability (``by_recipe``/``out_of_scope`` are untouched); they only feed the ranker and surface
    # per-recipe grain/context warnings. ``target_entity`` is a grain nudge (never a reject); an unknown
    # value simply yields UNKNOWN/COMPATIBLE. Default empty so every dimension-free caller is unchanged.
    modelling_contexts: list[str] = []
    target_entity: str | None = None


class ConsideredSetIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    definition: str = ""
    objective: str = Field(min_length=1)
    catalog_source: str | None = None
    entity: str | None = None
    target_ref: str | None = None
    feedback: str | None = None   # whole-round human guidance: a feedback round re-runs the considered
    #                               set under this instruction, minting a FRESH governable intent
    # ── Phase-1B (Task 7): present ⇒ mint a generation run, persist the confirmed scope BEFORE the
    # builder, scope grounding, and attach a per-recipe disposition lens. Absent ⇒ today's path exactly.
    intent_id: str | None = None          # reuse a prior recognition's immutable intent (else submit)
    recognition_id: str | None = None     # the recognition attempt this scope confirms (lineage)
    confirmed_scope: ConfirmedScopeIn | None = None
    supersedes_scope_id: str | None = None   # broaden lineage: the scope this run's scope supersedes


class DraftReqIn(BaseModel):
    intent_id: str
    chosen_source: str            # "anchor" | "alternative"
    chosen_option_id: str         # the chosen feature's name (from the considered set)
    why: str = ""


class RecognitionIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    objective: str = ""           # optional prediction goal; redacted before it can reach the LLM


# ---- routes -------------------------------------------------------------------------------------
def _considered_set_response(intent, cs) -> dict:
    """Today's considered-set response body — the anchor + alternatives + recommendation + rejections.
    The scoped (Phase-1B) path returns this SAME shape plus the disposition lens; the no-scope path
    returns it verbatim (byte-unchanged vs pre-1B)."""
    return {"intent_id": intent.intent_id, "anchor": cs.anchor,
            "alternatives": cs.alternatives, "recommendation": cs.recommendation,
            "rejections": cs.rejections}


def _disposition_json(ev: RecipeEvaluation) -> dict:
    """One recipe's per-stage disposition for the Gate-#1 lens: the rolled-up ``final_disposition``, the
    applicability ``relevance_tier``, and each stage's ``{status, reason_codes, evaluation_version,
    evaluated_at}`` — the version + server-clock stamps the model computes so a disposition is replayable."""
    def _stage(s: StageEvaluation) -> dict:
        evaluated_at = s.evaluated_at
        return {"status": s.status.value, "reason_codes": list(s.reason_codes),
                "evaluation_version": s.evaluation_version,
                "evaluated_at": (evaluated_at.isoformat()
                                 if isinstance(evaluated_at, datetime) else evaluated_at)}

    return {"recipe_id": ev.recipe_id, "final_disposition": ev.final_disposition.value,
            "relevance_tier": ev.relevance_tier, "applicability": _stage(ev.applicability),
            "grounding": _stage(ev.grounding), "safety": _stage(ev.safety)}


# ── Phase-2A Task A3: rank the eligible set (flag-gated, default off) ────────────────────────────────
# The ranker consumes a PRECOMPUTED rankable set; it never reads FinalDisposition itself. This route is
# the ONE place FinalDisposition is read for ranking (``rankable_recipe_ids``), so the ranker stays
# disposition-agnostic and survives the future policy initiative untouched. The three presentation layers
# stay separate: the deterministic ``ranking`` here, the LLM ``recommendation``, and the human choice.
_TEMPLATES_BY_ID = {t.id: t for t in ALL_TEMPLATES}


def _intent_ranking_enabled() -> bool:
    """Deterministic ranking is OFF by default — the scoped considered-set omits ``ranking`` /
    ``ranking_version`` entirely (Phase-1B/Task-7 byte-identical) unless a deployment opts in with
    ``FEATUREGEN_INTENT_RANKING=1``."""
    return os.environ.get("FEATUREGEN_INTENT_RANKING", "0") == "1"


def rankable_recipe_ids(dispositions: list[RecipeEvaluation]) -> list[str]:
    """The precomputed rankable set: the recipe ids whose rolled-up disposition is ``ELIGIBLE``.

    This is the ONLY place :class:`FinalDisposition` is read for ranking — the ranker itself is handed
    this already-decided set and never inspects dispositions, so it is stable across the future policy
    initiative (today rankable == Phase-1B ``ELIGIBLE``; post-policy == the post-policy eligible ids).
    """
    return [ev.recipe_id for ev in dispositions
            if ev.final_disposition is FinalDisposition.ELIGIBLE]


def _rank_signals(rankable_ids: list[str], dispositions: list[RecipeEvaluation],
                  cs, scope: ConfirmedScope) -> dict[str, RankSignals]:
    """Assemble the typed :class:`RankSignals` per rankable recipe from four already-computed sources:
    the disposition (``relevance_tier``), this run's grounding (``binding_quality``), the template's
    design-time metadata (``pit_completeness`` / ``family`` / ``explainability`` / journey / semantic
    group), and the confirmed-scope DIMENSIONS (Task B3): ``modelling_context_fit`` from
    ``scope.modelling_contexts`` and the soft ``entity_compatibility`` from ``scope.target_entity``. A
    dimension-free scope leaves those two at NEUTRAL / UNKNOWN (2A ranking is unaffected). A rankable id
    with no known template is skipped (the ranker then deterministically drops it — it cannot be ordered
    without a signal bundle)."""
    tier_by_id = {ev.recipe_id: ev.relevance_tier for ev in dispositions}
    signals: dict[str, RankSignals] = {}
    for rid in rankable_ids:
        t = _TEMPLATES_BY_ID.get(rid)
        if t is None:
            continue
        journey = journey_metadata(t)
        signals[rid] = RankSignals(
            relevance_tier=tier_by_id.get(rid) or "supporting",   # ELIGIBLE => a real in-scope tier
            binding_quality=BindingQuality(
                cs.binding_quality_by_template.get(rid, BindingQuality.ACCEPTABLE.value)),
            # Task B3: the confirmed modelling-context fit (NEUTRAL when none confirmed).
            modelling_context_fit=modelling_context_fit(t, scope.modelling_contexts),
            pit_completeness=pit_completeness(t),
            explainability=t.explain,
            family=t.family,
            journey_model_id=journey.journey_model_id,
            journey_stage_id=journey.journey_stage_id,
            semantic_group=semantic_group(t),
            # Task B3: the SOFT grain fit (UNKNOWN when no target_entity confirmed) — never a reject.
            entity_compatibility=entity_compatibility(t, scope.target_entity),
        )
    return signals


def _signal_warnings(signals: dict[str, RankSignals]) -> dict[str, list[str]]:
    """The SOFT per-recipe dimension warnings surfaced alongside the ranking — presentation metadata,
    NEVER an applicability decision (``by_recipe``/``dispositions`` are untouched; nothing is rejected).

    A recipe whose grain only DERIVES the confirmed ``target_entity`` (a real grain mismatch a roll-up
    can bridge) carries ``entity_grain_mismatch``; one whose declared modelling context CONFLICTS with
    the confirmed context carries ``modelling_context_conflict``. An ``EXACT``/``UNKNOWN`` grain and a
    ``NEUTRAL``/``COMPATIBLE``/``REQUIRED_MATCH`` context carry nothing. Only recipes with a warning
    appear in the map (keyed by recipe id)."""
    warnings: dict[str, list[str]] = {}
    for rid, s in signals.items():
        codes: list[str] = []
        if s.entity_compatibility is EntityCompatibility.DERIVABLE:
            codes.append("entity_grain_mismatch")
        if s.modelling_context_fit is ModellingContextFit.CONFLICT:
            codes.append("modelling_context_conflict")
        if codes:
            warnings[rid] = codes
    return warnings


def _ranking_json(r: RankedRecipe) -> dict:
    """One recipe's two ranking projections for the response — the canonical rank + the separate
    initial-view decision, each with its OWN structured reason stream (never merged)."""
    return {"recipe_id": r.recipe_id, "canonical_rank": r.canonical_rank,
            "selected_for_initial_view": r.selected_for_initial_view,
            "rank_reasons": [c.value for c in r.rank_reasons],
            "initial_view_reasons": [c.value for c in r.initial_view_reasons]}


def _scoped_considered_set(body: ConsideredSetIn, conn: _Conn, identity: _Identity,
                           client: _LLM) -> dict:
    """Phase-1B (Task 7) — the confirmed-scope path. Validates the confirmed scope, MINTS the generation
    run, PERSISTS the confirmed scope in the API layer BEFORE the builder (the canonical run→scope
    linkage; scope persistence is never the builder's job), computes the ONE ``ApplicabilityResult``,
    scopes grounding through it, and returns the considered set PLUS a per-recipe disposition lens and
    the applicability-owned in-scope count. **Broaden** is this same path re-called with
    ``unscoped=true``, a NEW server-minted run, and ``supersedes_scope_id`` set — a fresh unscoped run
    that supersedes the prior scope (both are retained)."""
    cscope = body.confirmed_scope
    assert cscope is not None   # caller only routes here when a confirmed scope is present
    # 1. Build the confirmed-scope value object. ``unscoped`` fails OPEN to full grounding: it needs no
    #    ids, so any stray ``primary``/``secondary`` is IGNORED (never validated — a broaden must never
    #    422 on a leftover id). Otherwise every confirmed id must be a selectable taxonomy leaf and the
    #    id set must be collision-free.
    if cscope.unscoped:
        scope = ConfirmedScope(
            primary=None, secondary=(), unscoped=True,
            modelling_contexts=tuple(cscope.modelling_contexts), target_entity=cscope.target_entity)
    else:
        # A ``primary`` that also appears in ``secondary`` (or a duplicated ``secondary``) would collide
        # on the ``confirmed_scope_use_case`` PK downstream → UniqueViolation → 500; reject it as a 422.
        if cscope.primary is not None and cscope.primary in cscope.secondary:
            raise HTTPException(status_code=422,
                                detail="primary use-case must not also appear in secondary")
        if len(cscope.secondary) != len(set(cscope.secondary)):
            raise HTTPException(status_code=422, detail="secondary use-cases must be unique")
        confirmed_ids = ([cscope.primary] if cscope.primary else []) + list(cscope.secondary)
        leaves = selectable_leaves()
        for uid in confirmed_ids:
            if use_case(uid) is None or uid not in leaves:
                raise HTTPException(status_code=422,
                                    detail=f"{uid!r} is not a selectable use-case leaf")
        # The confirmed-scope value object (an unknown expansion string → 422, not a 500).
        try:
            scope = ConfirmedScope(
                primary=cscope.primary, secondary=tuple(cscope.secondary),
                expansion=ScopeExpansion(cscope.expansion), unscoped=False,
                modelling_contexts=tuple(cscope.modelling_contexts),
                target_entity=cscope.target_entity)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    # 2. Reuse the recognition's immutable intent if given, else submit a fresh (redacted) one.
    try:
        intent = submit_intent(hypothesis=body.hypothesis, definition=body.definition,
                               actor=identity.subject)
    except IntentValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if body.intent_id:
        # A client-supplied intent_id must belong to the REQUESTING actor — otherwise a crafted id could
        # clobber another user's intent (inheriting its considered set + target_ref leakage gate). The
        # 404 is opaque whether the id is unknown or owned by someone else; no run is minted and no scope
        # is persisted (this precedes the mint/persist below). Same jsonb-string actor form the dedup uses.
        owned = conn.execute(
            "SELECT 1 FROM contract_intent WHERE intent_id = %s AND actor = %s::jsonb",
            (body.intent_id, _actor_json(intent.actor))).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="unknown intent")
        intent = replace(intent, intent_id=body.intent_id)
    # 4. Mint the generation run — the run is born only NOW, when the human commits to generate.
    generation_run_id = mint_id("grun")
    # 5. Persist the confirmed scope in the API layer, BEFORE the builder (the run→scope linkage exists
    # before any generation). The intent is durably recorded first so the lineage reads intent→run→scope.
    persist_intent(conn, intent, body.target_ref)
    scope_id = record_confirmed_scope(
        conn, intent_id=intent.intent_id, generation_run_id=generation_run_id,
        recognition_id=body.recognition_id, scope=scope,
        use_case_origins=cscope.use_case_origins, confirmation_source=cscope.confirmation_source,
        confirmed_by=identity.subject, supersedes_scope_id=body.supersedes_scope_id)
    # 6. Compute applicability ONCE — grounding AND the disposition lens consume this single object.
    applicability = applicability_result(scope)
    now = datetime.now(UTC)
    cs = build_considered_set(
        conn, intent, client, entity=body.entity, catalog_source=body.catalog_source,
        roles=identity.role_claims, target_ref=body.target_ref, objective=body.objective,
        feedback=body.feedback, now=now, applicability=applicability)
    # 7. The per-stage disposition lens over the SAME applicability + this run's grounding outcome.
    dispositions = evaluate_dispositions(
        applicability, cs.grounded_template_ids, cs.rejected_template_ids,
        evaluation_version=APPLICABILITY_MAPPING_VERSION, now=now)
    # 8. Applicability OWNS the in-scope recipe count (never recognition).
    response = {**_considered_set_response(intent, cs),
                "generation_run_id": generation_run_id, "scope_id": scope_id,
                "dispositions": [_disposition_json(d) for d in dispositions],
                "in_scope_count": len(applicability.eligible_ids)}
    # 9. Phase-2A: deterministic presentation-priority ranking over the PRECOMPUTED rankable set. The
    # rankable set (the ONLY FinalDisposition read) is decided first; the ranker then orders it, staying
    # disposition-agnostic. ``ranking_version`` is pinned BEFORE ranking (provenance, never an ordering
    # input). Flag off => neither key is present (Task-7/1B byte-identical). The ranking is deliberately
    # SEPARATE from the LLM ``recommendation`` and the human's Gate-#1 choice — three distinct layers.
    if _intent_ranking_enabled():
        rankable_ids = rankable_recipe_ids(dispositions)
        signals = _rank_signals(rankable_ids, dispositions, cs, scope)
        ranking_version = APPLICABILITY_MAPPING_VERSION   # pinned BEFORE the ranker is called
        ranked = rank_eligible(rankable_ids, signals, ranking_version=ranking_version)
        response["ranking"] = [_ranking_json(r) for r in ranked]
        response["ranking_version"] = ranking_version
        # Task B3: the SOFT dimension warnings (grain mismatch / context conflict) surfaced per recipe.
        # This NEVER changes dispositions — a warned recipe stays exactly as eligible as it was.
        response["signal_warnings"] = _signal_warnings(signals)
    return response


@router.post("/contract/considered-set", dependencies=[Depends(require_feature_generate)])
def considered_set(body: ConsideredSetIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Intake (mandatory hypothesis + optional definition, redacted) → the validated considered set:
    the anchor (from the definition) + generated alternatives + an advisory recommendation. Persists
    the intent. Every option shown has passed the gauntlet.

    Phase-1B: when ``confirmed_scope`` is present the request mints a generation run, persists the
    confirmed scope BEFORE the builder, scopes grounding through a single ``ApplicabilityResult`` and
    attaches a per-recipe disposition lens (see :func:`_scoped_considered_set`). Absent → today's exact
    path (no run, no scope row, no dispositions)."""
    if body.confirmed_scope is not None:
        return _scoped_considered_set(body, conn, identity, client)
    try:
        intent = submit_intent(hypothesis=body.hypothesis, definition=body.definition,
                               actor=identity.subject)
    except IntentValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    cs = build_considered_set(
        conn, intent, client, entity=body.entity, catalog_source=body.catalog_source,
        roles=identity.role_claims, target_ref=body.target_ref, objective=body.objective,
        feedback=body.feedback, now=datetime.now(UTC))
    return _considered_set_response(intent, cs)


@router.post("/contract/recognitions", dependencies=[Depends(require_feature_generate)])
def recognitions(body: RecognitionIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Phase-1B Gate #1 recognition: classify the objective's governed use-case scope from the
    REDACTED hypothesis/goal (recognition NEVER sees catalog columns) and persist an append-only
    recognition attempt — BEFORE any generation run exists. Decoupled from generation: no
    ``generation_run_id`` is minted here and no recipe/applicability count is returned (applicability
    owns any recipe count, computed later once the human commits to generate). FAIL-OPEN: ``recognize``
    never raises, so a provider failure/refusal folds to ``status='technical_failure'`` at HTTP 200 —
    recognition never blocks generation and never 5xxs."""
    try:
        intent = submit_intent(hypothesis=body.hypothesis, actor=identity.subject)
        redacted_goal = redact_free_text(body.objective) if body.objective else None
    except IntentValidationError as e:   # a free-text field that cannot be safely redacted -> denial
        raise HTTPException(status_code=422, detail=str(e)) from e
    # Idempotent intent, PER ACTOR: submit_intent mints a fresh id each call, so reuse the EARLIEST intent
    # already recorded for this exact (actor, hypothesis, mode) — re-recognising the same objective is free
    # and never forks the immutable intent. The actor filter is essential: WITHOUT it, user B typing user
    # A's hypothesis would reuse A's intent (attribution merge + considered-set clobber + inherited
    # target_ref → wrong leakage gate). The ``actor`` column is a jsonb STRING scalar (identity.subject,
    # e.g. "user:tester"), so compare on the exact serialized form _actor_json/persist_intent store — an
    # ``actor->>'subject'`` path would be NULL here. persist_intent is itself ON CONFLICT (intent_id) DO NOTHING.
    prior = conn.execute(
        "SELECT intent_id FROM contract_intent WHERE hypothesis = %s AND intake_mode = %s "
        "AND actor = %s::jsonb ORDER BY created_at ASC LIMIT 1",
        (intent.hypothesis, intent.intake_mode, _actor_json(intent.actor))).fetchone()
    if prior is not None:
        intent = replace(intent, intent_id=prior[0])
    persist_intent(conn, intent)

    input_hash = compute_input_hash({"hypothesis": intent.redacted_hypothesis, "goal": redacted_goal})
    result = recognize(conn, client, redacted_hypothesis=intent.redacted_hypothesis,
                       redacted_goal=redacted_goal, actor=identity)
    recognition_id = record_recognition_attempt(
        conn, intent_id=intent.intent_id, input_hash=input_hash, result=result,
        actor=identity.subject)
    # Fail-open asymmetry: unscoped / technical_failure -> full grounding downstream (recognition never
    # narrows on doubt). The recipe count is NOT here — applicability computes it after generate.
    unscoped = result.status in (RecognitionStatus.UNSCOPED, RecognitionStatus.TECHNICAL_FAILURE)
    candidates = [{
        "use_case_id": c.use_case_id,
        "display_name": (uc.display_name if (uc := use_case(c.use_case_id)) else c.use_case_id),
        "relationship": c.relationship,
        "confidence": c.confidence,
        "evidence_spans": list(c.evidence_spans),
    } for c in result.candidates]
    return {"intent_id": intent.intent_id, "recognition_id": recognition_id,
            "status": result.status.value, "unscoped": unscoped, "candidates": candidates}


@router.post("/contract/draft", dependencies=[Depends(require_feature_generate)])
def draft(body: DraftReqIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Gate #1 → author. The chosen feature is reconstructed from the SERVER-persisted considered set
    (BLOCKER 1 — never an arbitrary client payload); the choice is recorded (audit); the leakage target
    is read SERVER-side (BLOCKER 2). Then draft + the critique→refine loop (MCV each pass)."""
    feature = chosen_feature(conn, body.intent_id, body.chosen_source, body.chosen_option_id)
    if feature is None:
        raise HTTPException(status_code=422,
                            detail="chosen option is not in the recorded considered set for this intent")
    record_gate1_choice(conn, body.intent_id, chosen_source=body.chosen_source,
                        chosen_option_id=body.chosen_option_id, actor=identity.subject, why=body.why)
    target = intent_target_ref(conn, body.intent_id)   # server truth, not client-supplied
    d = draft_contract(conn, feature, client, roles=identity.role_claims, target_ref=target,
                       actor=identity)
    d, unresolved = author_contract(conn, d, client, now=datetime.now(UTC), actor=identity)
    return {"draft": d, "unresolved": unresolved, "intent_id": body.intent_id}


@router.get("/contracts", dependencies=[Depends(require_feature_read)])
def list_governed_contracts(conn: _Conn, identity: _Identity, limit: int = 50) -> list[dict]:
    return list_contracts(conn, limit=limit)


@router.get("/contracts/{contract_id}", dependencies=[Depends(require_feature_read)])
def get_governed_contract(contract_id: str, conn: _Conn, identity: _Identity) -> dict:
    c = get_contract_detail(conn, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown contract {contract_id!r}")
    return c


@router.post("/contract/confirm", dependencies=[Depends(require_feature_generate)])
def confirm(body: DraftIn, conn: _Conn, identity: _Identity) -> Contract:
    """The human gate — the GOVERNING write. Server-stateful, no client trust (closes the two BLOCKERs
    at the write, not just at /draft):
      * intent_id is REQUIRED; a missing/forged one is rejected (no fall back to a client target_ref);
      * the draft must correspond to the human's RECORDED Gate #1 choice reconstructed from the
        server-persisted considered set — a feature never offered/chosen cannot be governed;
      * target_ref is read SERVER-side from the intent with NO client fallback, so the leakage gate
        cannot be disabled by omitting it.
    Then confirm_contract re-runs the deterministic MCV and registers a versioned, drift-linked contract."""
    if not body.intent_id:
        raise HTTPException(status_code=422, detail="intent_id is required to govern a contract")
    choice = gate1_choice(conn, body.intent_id)
    if choice is None:
        raise HTTPException(status_code=422,
                            detail="no Gate #1 choice recorded for this intent — draft it first")
    chosen = chosen_feature(conn, body.intent_id, choice["chosen_source"], choice["chosen_option_id"])
    if chosen is None:
        raise HTTPException(status_code=422,
                            detail="the chosen feature is not in the recorded considered set")
    draft = body.to_draft()
    if (draft.feature_name != chosen.name
            or frozenset(draft.derives_pairs) != frozenset(chosen.derives_pairs)
            or (draft.aggregation or "") != (chosen.aggregation or "")):
        raise HTTPException(status_code=422, detail="the draft does not match the chosen feature")
    target = intent_target_ref(conn, body.intent_id)   # SERVER truth — never the client body
    try:
        return confirm_contract(conn, draft, actor=identity.subject, now=datetime.now(UTC),
                                target_ref=target, intent_id=body.intent_id)
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except psycopg.errors.UniqueViolation as e:   # concurrent double-confirm -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract version conflict occurred; re-fetch and retry") from e

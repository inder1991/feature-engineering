"""Hypothesis-driven feature-contract flow over HTTP.

Stateless: the frontend carries the discovered options / draft as JSON between steps, and the SERVER
re-validates (the deterministic MCV re-runs at author + confirm), so a tampered payload can never govern
a leaky / stale / ungrounded contract. Safety kwargs (roles, target_ref, server clock) are always
threaded — omitting them would silently downgrade safety (review root-cause A).
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_feature_gen_conn,
    get_identity,
    get_llm,
    require_feature_generate,
    require_feature_read,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.idgen import mint_id
from featuregen.intake.llm import LLMClient, compute_input_hash
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract.author import (
    ContractDraft,
    CrossCatalogPlanRequired,
    StalePlan,
    _as_of_column,
    _envelope_join_path,
    draft_contract,
)
from featuregen.overlay.upload.contract.gate1 import (
    _intent_scoped_applicability_enabled,
    build_considered_set,
    chosen_feature,
    considered_snapshot_lineage,
    gate1_choice,
    intent_target_ref,
    persist_intent,
    record_gate1_choice,
)
from featuregen.overlay.upload.contract.govern import (
    Contract,
    ContractPointerConflict,
    ContractValidationError,
    binding_exposure,
    binding_hash,
    confirm_contract,
    confirmed_role_bindings,
    get_contract_detail,
    list_contracts,
)
from featuregen.overlay.upload.contract.intake import (
    IntentValidationError,
    redact_free_text,
    submit_intent,
)
from featuregen.overlay.upload.contract.live_activation import (
    CROSS_CATALOG_GROUNDING_NOT_ENABLED,
    LiveActivationNotReady,
    cross_catalog_grounding_enabled,
    is_live_cross_catalog_enabled,
    require_live_ready,
)
from featuregen.overlay.upload.contract.review import author_contract
from featuregen.overlay.upload.contract.scope_records import (
    dimension_provenance,
    record_confirmed_scope,
    record_recognition_attempt,
)
from featuregen.overlay.upload.feature_metadata_snapshot import CatalogProjectionUnavailable
from featuregen.overlay.upload.planner.contracts import ReplayFreshness
from featuregen.overlay.upload.planner.plan_envelope import recheck_plan_freshness
from featuregen.overlay.upload.planner.shadow import run_shadow_planner
from featuregen.overlay.upload.taxonomy.applicability import (
    ConfirmedScope,
    ScopeExpansion,
    applicability_result,
)
from featuregen.overlay.upload.taxonomy.dimensions import MODELLING_CONTEXTS, known_entities
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

logger = logging.getLogger(__name__)

router = APIRouter()

_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
# Delivery C0: ONLY the considered-set route BUILDS the C0 metadata snapshot, so ONLY it needs the
# REPEATABLE READ (_FeatureGenConn) torn-free view. MF-2: /contract/draft, /contract/confirm and
# /contract/recognitions do NOT build a snapshot — they only RELOAD server lineage / re-run the MCV — so
# they stay on the default _Conn (READ COMMITTED). Putting them on REPEATABLE READ gave them no benefit
# and turned designed 409 races (a concurrent re-confirm / double-submit) into uncaught 40001
# SerializationFailure 500s. The read-only /contracts list/detail routes also stay on _Conn.
_FeatureGenConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
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
    # H1b Gate-1 role-binding confirmation: the binding_hash the client SAW at /contract/draft. At
    # confirm the server recomputes the CURRENT binding_hash from its authoritative reconciled bindings
    # and 409s if it differs (bindings drifted since draft — re-review). LEGACY DEGRADATION: absent
    # (None) ⟹ the gate is SKIPPED (a pre-H1b client that never fetched a hash is not broken); a client
    # that sends it gets the fail-closed gate. Requirement ids / "passed" are NEVER accepted here — the
    # server mints durable ids at confirm (Pydantic ignores any such extra body fields).
    expected_binding_hash: str | None = None

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


def _live_cross_catalog_flag_on() -> bool:
    """3C.2a — the LIVE governed cross-catalog kill switch, read ONLY in the route (the builder is handed
    the resolved boolean, never the env). OFF by default → no readiness query, no governed lens, byte-
    identical to today. On its own it is necessary-but-not-sufficient: activation approval is still
    required (see :func:`require_live_ready`), so a flag-on-but-unapproved deployment fails closed 503."""
    return os.environ.get("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "0") == "1"


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
        # NOTE: EntityCompatibility.AMBIGUOUS is reserved (seed never emits it). A future multi-path
        # registry edge would need an AMBIGUOUS warning here.
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


def _scoped_considered_set(body: ConsideredSetIn, conn: _FeatureGenConn, identity: _Identity,
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
    # 0. Non-fatally CLEAN the confirmed dimensions against the closed vocab at the boundary (mirror
    #    ``recognition.normalize_dimensions`` — DROP unknowns, NEVER reject). A hand-crafted request could
    #    otherwise send a bogus ``modelling_context`` that makes every framework-tagged recipe CONFLICT
    #    (a spurious ``modelling_context_conflict`` warning, contradicting the field's own "unknown value
    #    yields COMPATIBLE" contract) and writes garbage to the immutable table. Cleaned BEFORE the scope
    #    is built, so ranking, warnings, AND the persisted rows all see the cleaned set. Dimensions stay
    #    SOFT — this narrows nothing (applicability is untouched); it only discards ungoverned values.
    clean_contexts = tuple(c for c in cscope.modelling_contexts if c in MODELLING_CONTEXTS)
    clean_entity = cscope.target_entity if cscope.target_entity in known_entities() else None
    # 1. Build the confirmed-scope value object. ``unscoped`` fails OPEN to full grounding: it needs no
    #    ids, so any stray ``primary``/``secondary`` is IGNORED (never validated — a broaden must never
    #    422 on a leftover id). Otherwise every confirmed id must be a selectable taxonomy leaf and the
    #    id set must be collision-free.
    if cscope.unscoped:
        scope = ConfirmedScope(
            primary=None, secondary=(), unscoped=True,
            modelling_contexts=clean_contexts, target_entity=clean_entity)
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
                modelling_contexts=clean_contexts,
                target_entity=clean_entity)
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
    # 3C.2a — the LIVE governed cross-catalog readiness interlock. On an entity-scoped run (no single
    # catalog) with the live flag ON, the deployment MUST be activation-approved BEFORE any LLM/planner
    # dispatch — fail-closed 503, NEVER a legacy fallback, and BEFORE any run/scope is minted or
    # persisted. The env flag is read ONLY here; the builder is handed the resolved boolean below. Flag
    # unset → no readiness query at all (``is_live_cross_catalog_enabled`` short-circuits), byte-identical.
    if body.catalog_source is None and _live_cross_catalog_flag_on():
        try:
            require_live_ready(conn)
        except LiveActivationNotReady as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
    # 4. Mint the generation run — the run is born only NOW, when the human commits to generate.
    generation_run_id = mint_id("grun")
    # 5. Persist the confirmed scope in the API layer, BEFORE the builder (the run→scope linkage exists
    # before any generation). The intent is durably recorded first so the lineage reads intent→run→scope.
    persist_intent(conn, intent, body.target_ref)
    # Reconstruct each confirmed dimension's provenance from the IMMUTABLE recognition attempt (never the
    # client): a value the recognizer proposed is ``accepted_llm_proposal``, one the human introduced is
    # ``user_added``, and a corrected entity is a ``user_replacement`` recording what it superseded.
    dim_sources, dim_replaces = dimension_provenance(conn, body.recognition_id, scope)
    scope_id = record_confirmed_scope(
        conn, intent_id=intent.intent_id, generation_run_id=generation_run_id,
        recognition_id=body.recognition_id, scope=scope,
        use_case_origins=cscope.use_case_origins, confirmation_source=cscope.confirmation_source,
        confirmed_by=identity.subject, supersedes_scope_id=body.supersedes_scope_id,
        dimension_sources=dim_sources, replaces=dim_replaces)
    # 6. Compute applicability ONCE — grounding AND the disposition lens consume this single object.
    applicability = applicability_result(scope)
    now = datetime.now(UTC)
    # 3C.2a: the resolved live-activation boolean threads into the builder so the governed cross-catalog
    # lens runs ONLY when the deployment is flag-on-and-approved (short-circuits to False when the flag is
    # unset — no DB query). ``target_entity`` is the confirmed-scope grain the governed planner plans to,
    # exactly the entity the log-only shadow planner already uses below.
    is_live = is_live_cross_catalog_enabled(conn)
    # Delivery C0 Task 5: anchor the metadata snapshot to THIS run (the scoped path already minted it
    # in step 4). A projection-lagged catalog aborts the whole considered set — feature generation must
    # not proceed on a stale projected view — surfaced as 503 CATALOG_PROJECTION_UNAVAILABLE.
    try:
        cs = build_considered_set(
            conn, intent, client, entity=body.entity, catalog_source=body.catalog_source,
            roles=identity.role_claims, target_ref=body.target_ref, objective=body.objective,
            feedback=body.feedback, now=now, applicability=applicability,
            is_live=is_live, target_entity=scope.target_entity,
            generation_run_id=generation_run_id)
    except CatalogProjectionUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail) from e
    except psycopg.errors.SerializationFailure as e:   # MF-2: the RR broaden race on contract_considered
        raise HTTPException(   # (ON CONFLICT (intent_id) DO UPDATE) → a designed conflict, never a 500
            status_code=409,
            detail="a concurrent request updated this intent; re-fetch and retry") from e
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
    # 3B.3a shadow: on an entity-scoped run (no single catalog to ground on) compute + LOG single-catalog
    # binding plans for the eligible recipes. Log-only — the response is UNCHANGED.
    if body.catalog_source is None and scope.target_entity is not None:
        try:
            with conn.transaction():         # savepoint — a shadow DB error must not poison the request's txn
                # 3B.3c (C8): the contract-compile kill-switch is read HERE and only here — the
                # planner stays pure (no os.environ below the route). Default OFF: plans stay
                # contract_resolution_status=not_compiled and the shadow pass is byte-identical.
                run_shadow_planner(conn, eligible_recipe_ids=applicability.eligible_ids,
                                   target_entity=scope.target_entity, roles=identity.role_claims,
                                   run_id=generation_run_id, now=now,
                                   compile_contracts=os.environ.get(
                                       "FEATUREGEN_INTENT_CONTRACT_COMPILE", "0") == "1",
                                   # 3B.4: the telemetry flag gates PERSISTENCE, independent of the
                                   # compile flag — read ONLY here so the planner stays pure.
                                   persist=os.environ.get(
                                       "FEATUREGEN_INTENT_SHADOW_TELEMETRY", "0") == "1",
                                   # 3C.1 run provenance: the OTHER two intent flags, recorded on the
                                   # dispatch manifest (read here, in the route, like the two above —
                                   # the planner stays pure and only stamps what it is handed).
                                   scoped_applicability=_intent_scoped_applicability_enabled(),
                                   ranking=_intent_ranking_enabled())
        except Exception:                    # shadow must NEVER affect the live response
            logger.exception("shadow planner dispatch failed")
    return response


@router.post("/contract/considered-set", dependencies=[Depends(require_feature_generate)])
def considered_set(body: ConsideredSetIn, conn: _FeatureGenConn, identity: _Identity,
                   client: _LLM) -> dict:
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
    # 3C.2a — the LIVE governed cross-catalog interlock on the NON-scoped path too (mirrors
    # _scoped_considered_set): an entity-scoped run (no single catalog) with the live flag ON must be
    # activation-approved BEFORE any dispatch — fail-closed 503 — and the resolved is_live threads into the
    # builder so the SAME _reject_cross_catalog_llm + anchor-drop + governed lens filters run here as on the
    # scoped path. FLAG UNSET → the gate short-circuits (no readiness query) and is_live reads False WITHOUT
    # a DB query, so this is byte-identical to today for every flag-off / single-catalog request.
    if body.catalog_source is None and _live_cross_catalog_flag_on():
        try:
            require_live_ready(conn)
        except LiveActivationNotReady as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
    is_live = is_live_cross_catalog_enabled(conn)
    # Delivery C0 Task 5: on the REPEATABLE READ feature-gen conn the builder mints an ``fgr`` run and
    # snapshots the in-scope catalog state, recording the lineage on the considered set. A
    # projection-lagged catalog aborts here → 503 (feature generation never proceeds on a stale view).
    try:
        cs = build_considered_set(
            conn, intent, client, entity=body.entity, catalog_source=body.catalog_source,
            roles=identity.role_claims, target_ref=body.target_ref, objective=body.objective,
            feedback=body.feedback, now=datetime.now(UTC), is_live=is_live)
    except CatalogProjectionUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail) from e
    except psycopg.errors.SerializationFailure as e:   # MF-2: the RR broaden race on contract_considered
        raise HTTPException(   # (ON CONFLICT (intent_id) DO UPDATE) → a designed conflict, never a 500
            status_code=409,
            detail="a concurrent request updated this intent; re-fetch and retry") from e
    return _considered_set_response(intent, cs)


@router.post("/contract/recognitions", dependencies=[Depends(require_feature_generate)])
def recognitions(body: RecognitionIn, conn: _Conn, identity: _Identity,
                 client: _LLM) -> dict:
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
            "status": result.status.value, "unscoped": unscoped, "candidates": candidates,
            "modelling_contexts": list(result.modelling_contexts),
            "target_entity": result.target_entity,
            "warnings": list(result.warnings)}


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
    # 3C.2a authoring fail-closed: a governed feature drafts its compiled plan envelope's path, rechecked
    # for freshness under the REQUEST's roles (the set it compiled under — else it would spuriously drift);
    # a drifted plan → 409 (regenerate, never a substitute path). I-1 draft/confirm parity: a cross-catalog
    # feature with NO governed envelope is refused at draft with the SAME umbrella reason confirm uses
    # (``CROSS_CATALOG_GROUNDING_NOT_ENABLED``) whatever the deployment state, so a user never drafts
    # something confirm will always reject; ``find_cross_catalog_path`` is never invoked from a draft (3C.2b).
    try:
        d = draft_contract(conn, feature, client, roles=identity.role_claims, target_ref=target,
                           actor=identity)
    except StalePlan as e:
        raise HTTPException(status_code=409, detail="plan stale, regenerate") from e
    except CrossCatalogPlanRequired as e:
        raise HTTPException(
            status_code=422,
            detail=f"{CROSS_CATALOG_GROUNDING_NOT_ENABLED}: cross-catalog feature requires a governed "
                   "plan envelope") from e
    d, unresolved = author_contract(conn, d, client, now=datetime.now(UTC), actor=identity)
    # Delivery C0 Task 5: carry the SERVER-persisted snapshot lineage forward (the run + immutable
    # snapshot the considered set was authored against). Reloaded from the server considered-set row —
    # the request model carries no client snapshot id, so there is nothing client-supplied to trust.
    # Null on a READ COMMITTED / pre-C0 considered set. This is ADDITIVE — the validator is unchanged.
    snapshot = considered_snapshot_lineage(conn, body.intent_id)
    # H1b — expose the exact role bindings (role / column-ref / source / authority / warnings) + the
    # overall binding_hash the human is confirming. The confirm requires this hash and 409s if the
    # server's authoritative bindings drift before finalize (see /contract/confirm). Computed over the
    # SERVER-authoritative reconciled draft `d`, so it equals the confirm-time recompute unless the
    # underlying catalog state actually drifts. READ-ONLY (no global authority write).
    bindings = confirmed_role_bindings(conn, d)
    return {"draft": d, "unresolved": unresolved, "intent_id": body.intent_id, "snapshot": snapshot,
            "bindings": binding_exposure(bindings), "binding_hash": binding_hash(bindings)}


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
    # SAFETY (tri-state gate): grain_table + derives_from drive the confirm-time MCV re-run's
    # grain/join/additivity dispositions but are NOT covered by the match check above. A client could
    # echo a matching name/derives_pairs/aggregation yet send grain_table=None (the grain + cross-table
    # join dispositions are gated on `if grain_table and single-catalog`, so they silently no-op and
    # GRAIN_IS_UNIQUE / JOIN_CONNECTIVITY vanish) or a trimmed derives_from (a measure kept in
    # derives_pairs but dropped here is absent from the per-operand `pairs`, so its
    # ADDITIVITY_SUPPORTS_OPERATION check never runs) — either erases the honest requirements and flips
    # NEEDS_EXTERNAL_VALIDATION -> DESIGN_CHECKED at the GOVERNING write. Overwrite both from the SERVER-
    # reconstructed chosen (the same server-authoritative pattern as join_path below), so the re-run
    # always reasons over the operands the human actually chose.
    # H1b: reconcile as_of_column SERVER-side too (mirroring grain_table/derives_from). The as_of role
    # is a confirmed binding; deriving it from the server chosen (never the client body) makes the
    # persisted bindings + the binding_hash fully server-authoritative and stable draft→confirm, so an
    # honest confirm can never be derailed and a tampered as_of is simply ignored (like grain_table).
    _catalogs = {cs for cs, _ref in chosen.derives_pairs}
    _grain_catalog = next(iter(_catalogs)) if len(_catalogs) == 1 else None
    draft = replace(draft, grain_table=chosen.grain_table,
                    derives_from=list(chosen.derives_from),
                    as_of_column=_as_of_column(conn, chosen.grain_table, _grain_catalog))
    # 3C.2a fail-closed at the GOVERNING write: re-run the freshness recheck against the SERVER-
    # reconstructed chosen feature's plan envelope (never the client body) under the request's roles —
    # a plan that drifted between draft and confirm must never silently finalize (409, regenerate). The
    # envelope branch is self-gated (only the flag-on governed planner attaches one), so it needs no
    # is_live guard. The cross-catalog-without-envelope 422 fires ONLY when the deployment is flag-on-and-
    # approved; FLAG-OFF a cross-catalog feature confirms via the permissive path, byte-identical to before.
    env = chosen.plan_envelope
    # H1c — does the candidate SPAN more than one catalog_source? Computed from the SERVER-reconstructed
    # chosen feature's ``derives_pairs`` (``_catalogs`` above — the same set H1b hashes), never the client
    # body. F3: when a governed ``plan_envelope`` is present the span MUST also fold in the envelope's OWN
    # participating catalogs (``catalog_sources``) and its ordered-path catalogs — a governed plan can
    # BRIDGE >1 catalog while its derives_pairs read-set stays single-catalog, and that bridge is exactly
    # the cross-catalog participation the interlock must gate. Union everything so ANY multi-catalog
    # participation trips the interlock (fail-closed). A cross-catalog contract may be governed ONLY under
    # the full interlock; anything short of it fails closed with ``CROSS_CATALOG_GROUNDING_NOT_ENABLED``.
    span_catalogs = set(_catalogs)
    if env is not None:
        span_catalogs |= set(env.catalog_sources)
        span_catalogs |= {seg.split(":", 1)[0] for seg in env.ordered_path if seg}
    cross_catalog = len(span_catalogs) > 1
    if env is not None:
        if recheck_plan_freshness(conn, env, identity.role_claims) is not ReplayFreshness.current:
            raise HTTPException(status_code=409, detail="plan stale, regenerate")
        # H1c fail-closed — a CROSS-catalog governed contract may be finalized ONLY while cross-catalog
        # grounding is GENUINELY enabled for this deployment AT THE GOVERNING WRITE: the durable
        # live-activation interlock (flag + PASS enablement + APPROVE + version vector) AND a valid signed
        # 3C gate artifact must BOTH still hold. Activation can be revoked / the signed artifact can expire
        # between draft and confirm, so re-check HERE (reusing the existing interlock + verifier) and refuse
        # rather than finalize a cross-catalog contract whose enablement lapsed. A single-catalog governed
        # plan needs no cross-catalog enablement — this sub-check is scoped to ``cross_catalog`` and is
        # byte-identical for every single-catalog / flag-off envelope.
        if cross_catalog and not cross_catalog_grounding_enabled(conn):
            raise HTTPException(
                status_code=422,
                detail=f"{CROSS_CATALOG_GROUNDING_NOT_ENABLED}: live cross-catalog grounding is not "
                       "enabled for this deployment (missing/stale activation or signed 3C gate artifact)")
        # 3C.2a fail-closed: a governed contract's persisted join_path is RE-DERIVED from the SERVER
        # envelope's ordered_path, NEVER the client body — the match-check above validates
        # name/derives_pairs/aggregation but NOT join_path, so a replay carrying a FABRICATED path (which
        # the freshness recheck still passes) would otherwise be persisted as the "governed" bridge. Scoped
        # strictly to the envelope-present case (single-catalog / flag-off drafts keep their client path).
        draft = replace(draft, join_path=tuple(_envelope_join_path(env.ordered_path)))
    elif cross_catalog:
        # H1c fail-closed — a cross-catalog candidate with NO governed plan envelope can NEVER be governed,
        # whatever the deployment state: it has no governed physical plan to author from, and the governing
        # write must NEVER fall back to the permissive ``find_cross_catalog_path``. This closes the hole
        # where a flag-off / unapproved multi-catalog confirm fell through to ``confirm_contract`` on the
        # client-supplied permissive join_path. (Supersedes the prior is_live-gated 422: a no-envelope
        # cross-catalog candidate is now refused unconditionally — the strongest fail-closed. The detail
        # still names the governed plan envelope, the specific missing prerequisite.)
        raise HTTPException(
            status_code=422,
            detail=f"{CROSS_CATALOG_GROUNDING_NOT_ENABLED}: cross-catalog feature requires a governed "
                   "plan envelope")
    target = intent_target_ref(conn, body.intent_id)   # SERVER truth — never the client body
    # Delivery C0 Task 5: reload the SERVER snapshot lineage the considered set was authored against and
    # bind the governing write to it in the audit trail (a regulator can prove EXACTLY what catalog state
    # this contract was authored against). Reloaded from the server considered-set row — the confirm
    # request model (DraftIn) carries no client snapshot id, so no client value is ever trusted. ADDITIVE:
    # the confirm-time MCV re-run + Slice-3 tamper-fix (grain_table/derives_from/join_path above) are
    # UNCHANGED — re-sourcing the validator onto the snapshot is a later delivery (C2–C4/H).
    lineage = considered_snapshot_lineage(conn, body.intent_id)
    if lineage is not None:
        logger.info("governing contract for intent %s against snapshot %s (run %s, content_hash %s)",
                    body.intent_id, lineage["snapshot_id"], lineage["generation_run_id"],
                    lineage["content_hash"])
    # H1b — the GATE-1 ROLE-BINDING analog of the plan-staleness 409. Recompute the CURRENT binding_hash
    # from the SERVER-authoritative reconciled bindings (the exact set confirm will persist) and, when the
    # client sent the hash it saw at draft, fail closed (409) if they differ — a binding drifted between
    # draft and confirm (a column retyped, a fact retired/expired, an authority changed). This is confirm-
    # time REVALIDATION: the per-binding state signature (H2c) folds each referenced fact's current
    # governed state, so an expired/unauthorized fact moves the hash and never finalizes on the drifted
    # binding set. LEGACY DEGRADATION: a body with no `expected_binding_hash` skips the gate (unchanged).
    current_binding_hash = binding_hash(confirmed_role_bindings(conn, draft))
    if (body.expected_binding_hash is not None
            and current_binding_hash != body.expected_binding_hash):
        raise HTTPException(status_code=409, detail="bindings changed, re-review")
    try:
        return confirm_contract(conn, draft, actor=identity.subject,
                                roles=identity.role_claims,   # the CONFIRMER's authority reaches the
                                #                               re-run's join-authority disposition
                                now=datetime.now(UTC), target_ref=target, intent_id=body.intent_id,
                                confirmed_binding_hash=current_binding_hash)
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ContractPointerConflict as e:   # M-a: the pointer CAS lost a race -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract pointer conflict occurred; re-fetch and retry") from e
    except psycopg.errors.UniqueViolation as e:   # concurrent double-confirm -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract version conflict occurred; re-fetch and retry") from e

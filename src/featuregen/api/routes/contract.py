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
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_feature_gen_conn,
    get_identity,
    get_llm,
    get_llm_optional,
    require_feature_generate,
    require_feature_read,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.idgen import mint_id
from featuregen.intake.llm import LLMClient, compute_input_hash
from featuregen.intake.redaction import REDACTION_VERSION
from featuregen.overlay.upload.activation_policy import activation_decision
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
    Gate1Error,
    UnknownConsideredOption,
    _intent_scoped_applicability_enabled,
    _public_considered_snapshot,
    build_considered_set,
    gate1_choice,
    intent_target_ref,
    persist_intent,
    recorded_gate1_choice_revision,
    recorded_gate1_draft_choice,
    select_and_record_gate1_choice,
    verified_considered_revision_by_id,
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
from featuregen.overlay.upload.contract.governed_plan import GovernedPlanDrift
from featuregen.overlay.upload.contract.intake import (
    IntentValidationError,
    redact_free_text,
    submit_intent,
)
from featuregen.overlay.upload.contract.intake_ticket import (
    _use_case_vocabulary,
    extract_intake_ticket,
    is_readable_column,
    record_target_reading,
    target_reading,
)
from featuregen.overlay.upload.contract.live_activation import (
    CROSS_CATALOG_GROUNDING_NOT_ENABLED,
    LiveActivationNotReady,
    cross_catalog_grounding_enabled,
    is_live_cross_catalog_enabled,
    require_live_ready,
)
from featuregen.overlay.upload.contract.review import author_contract
from featuregen.overlay.upload.contract.scope_mode import confirmation_required, scope_mode_status
from featuregen.overlay.upload.contract.scope_records import (
    GenerationInputUnavailable,
    RecognitionInput,
    RecognitionInputUnavailable,
    dimension_provenance,
    generation_input_for_run,
    load_recognition_input,
    recognition_id_for_scope,
    recognition_input_material,
    record_confirmed_scope,
    record_generation_input,
    record_recognition_attempt,
    use_case_provenance,
)
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CatalogProjectionUnavailable,
    check_projection_readiness,
    compare_snapshot_to_current,
    ensure_generation_run,
)
from featuregen.overlay.upload.planner.contracts import ReplayFreshness
from featuregen.overlay.upload.planner.plan_envelope import recheck_plan_freshness
from featuregen.overlay.upload.planner.shadow import run_shadow_planner
from featuregen.overlay.upload.recipe_formula_shadow import (
    capture_ranked_shadow,
    declare_expected_run,
    recipe_formula_shadow_enabled,
)
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
from featuregen.runtime.observability import counters

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
_OptionalLLM = Annotated[LLMClient | None, Depends(get_llm_optional)]


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
    choice_id: str | None = None
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
    leaf. The two deprecated provenance fields are accepted only for wire compatibility and ignored;
    the server derives governance provenance from the authenticated action and recognition record."""
    primary: str | None = None
    secondary: list[str] = []
    expansion: str = ScopeExpansion.EXACT.value
    unscoped: bool = False
    # B10: the confirmed unit of analysis + spine (from the yes/no on the derived proposal;
    # a "no" picks from the catalog's realistic list — the UI never free-texts these).
    uoa_entity: str | None = None
    spine_ref: str | None = None
    use_case_origins: dict[str, str] | None = Field(default=None, deprecated=True)
    confirmation_source: str | None = Field(default=None, deprecated=True)
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
    # SE-11: the EXPLICIT response-contract opt-in. 1 (the default) = today's response, byte-
    # identical — an old client never infers a version from optional fields. 2 = the semantic
    # candidate contract: top-level contract_version + the resolved semantic-planning mode
    # (the step-7 diagnostic), with the per-card semantic fields the v2 card serializer already
    # carries. Never an env flag — the CLIENT asks per request.
    contract_version: Literal[1, 2] = 1


class DraftReqIn(BaseModel):
    intent_id: str
    chosen_source: str = "alternative"  # legacy-only in confirmation-required mode
    chosen_option_id: str         # opaque option id in confirmation-required mode
    why: str = ""
    expected_generation_run_id: str | None = None


class RecognitionIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    objective: str = ""           # optional prediction goal; redacted before it can reach the LLM
    feedback: str | None = Field(default=None, max_length=2000)
    supersedes_scope_id: str | None = None


class IntakeIn(BaseModel):
    """The mandatory read (intake build, router-quality plan 2026-08-10): one hypothesis in, one
    ticket out — a DRAFT reading for the confirm screen, never a decision."""
    hypothesis: str = Field(min_length=1)
    catalog_source: str | None = None


class IntakeTargetIn(BaseModel):
    """The human's answer to the confirm screen. ``confirmed`` = "Yes, that's my target" (agreed
    with the draft), ``corrected`` = "Change it" (the click IS the extractor's ground truth — the
    two are one provenance and two telemetry counters), ``exploring`` = an explicit no-target
    declaration. The server validates the signed ref against the READ-SCOPED catalog — never
    against the ticket — so a correction to any real, visible column is one click."""
    intent_id: str
    decision: str = Field(pattern="^(confirmed|corrected|exploring)$")
    target_ref: str | None = None
    target_window_days: int | None = Field(default=None, ge=1)
    target_type: str | None = Field(default=None,
                                    pattern="^(binary_classification|regression|multiclass)$")
    business_domain: list[str] = []
    catalog_source: str | None = None


# ---- routes -------------------------------------------------------------------------------------
@router.get("/contract/scope-mode", dependencies=[Depends(require_feature_read)])
def scope_mode() -> dict:
    """Expose the server authority mode so clients and operators can detect rollout mismatches."""
    status = scope_mode_status()
    return {
        "mode": status.mode.value,
        "confirmation_required": status.confirmation_required,
        "configuration_valid": status.configuration_valid,
    }


@router.get("/contract/uoa-proposal", dependencies=[Depends(require_feature_read)])
def uoa_proposal(catalog_source: str, conn: _FeatureGenConn,
                 target_ref: str | None = None,
                 recognized_entity: str | None = None) -> dict:
    """B10 — the derived unit-of-analysis proposal the scope screen confirms with ONE click.

    Derived from FACTS, never guessed: the target column's table + that table's DECLARED
    grain column's entity. The alternatives are the catalog's REALISTIC list only — entities
    that actually have a keyed spine table (a closed list; the UI never free-texts a UOA).
    A recognizer entity that disagrees with the derivation is surfaced as a contradiction —
    stated, never silently resolved."""
    rows = conn.execute(
        "SELECT table_name, object_ref, entity FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND is_grain AND entity IS NOT NULL "
        "ORDER BY table_name, object_ref", (catalog_source,)).fetchall()
    alternatives = [{"entity": r[2], "spine_table": r[0], "spine_ref": r[1]} for r in rows]
    proposed = None
    if target_ref:
        target_table = target_ref.split(".")[-2] if target_ref.count(".") >= 2 else None
        proposed = next((a for a in alternatives if a["spine_table"] == target_table), None)
    if proposed is None and len(alternatives) == 1:
        proposed = alternatives[0]
    contradiction = None
    if (proposed and recognized_entity
            and recognized_entity.lower() != str(proposed["entity"]).lower()):
        contradiction = (f"the recognizer suggested {recognized_entity!r} but the target's "
                         f"table is keyed per {proposed['entity']!r} — you decide")
    return {"proposed": proposed, "alternatives": alternatives,
            "contradiction": contradiction}


@router.get("/contract/considered-revisions/{considered_revision_id}/options/{option_id}",
            dependencies=[Depends(require_feature_read)])
def considered_option_detail(considered_revision_id: str, option_id: str,
                             conn: _FeatureGenConn) -> dict:
    """SE-11 step 4 — the audit drawer: full eligibility and plan evidence for ONE option,
    served from the IMMUTABLE stored revision (hash-verified, the same verification the Gate-1
    choice path runs) plus this run's semantic observations. Never a live-catalog read to
    decorate it — what the human saw is what this returns."""
    try:
        revision = verified_considered_revision_by_id(conn, considered_revision_id)
    except Gate1Error as e:
        if str(e) == "UNKNOWN_CONSIDERED_REVISION":
            raise HTTPException(status_code=404, detail=str(e)) from e
        raise HTTPException(status_code=409, detail=str(e)) from e
    option = revision["considered"].get("options_by_id", {}).get(option_id)
    if option is None:
        raise HTTPException(status_code=404, detail="UNKNOWN_CONSIDERED_OPTION")
    detail = {
        "considered_revision_id": revision["considered_revision_id"],
        "considered_content_hash": revision["considered_content_hash"],
        "generation_run_id": revision["generation_run_id"],
        "option_id": option_id,
        "option": option,
    }
    # D1 — the STORED decision record by its exact (revision, option) key: the full audit
    # (planning request, verdicts, eligibility incl. the losing shortlist, validation with
    # families) + PLAN-15's decision manifest. LIFE-03's wrong-row risk is structurally
    # gone: never "newest observation for the definition". Verification is real: a manifest
    # whose planning_request_hash disagrees with the stored option identity is a typed 409.
    from featuregen.overlay.upload.semantic_option_decision import (
        load_option_decision_record,
    )

    identity = option.get("canonical_candidate_identity") or {}
    feature_body = identity.get("feature") or {}
    record = load_option_decision_record(
        conn, considered_revision_id=considered_revision_id, option_id=option_id)
    if record is not None:
        stored_request_hash = (record.get("decision_manifest") or {}).get(
            "planning_request_hash")
        if (stored_request_hash
                and record["planning_request_hash"] != stored_request_hash):
            raise HTTPException(status_code=409, detail={
                "code": "DECISION_RECORD_TAMPERED",
                "message": "the stored decision's manifest disagrees with its own request "
                           "identity — the record cannot be served"})
        detail["decision_record"] = record
        # The exact-linked observation (never newest-row): the run's raw binder output.
        if record.get("observation_id"):
            row = conn.execute(
                "SELECT context_hash, planning_request_hash, binding_state, verdicts, "
                "eligibility, policy_hashes FROM semantic_candidate_observation "
                "WHERE observation_id = %s", (record["observation_id"],)).fetchone()
            if row is not None:
                detail["semantic_evidence"] = {
                    "context_hash": row[0], "planning_request_hash": row[1],
                    "binding_state": row[2], "verdicts": row[3],
                    "eligibility": row[4], "policy_hashes": row[5],
                }
        return detail
    # Compatibility: an option with no decision row (pre-A1b revisions, non-semantic
    # options) keeps the newest-observation read — honest absence, no backfill.
    recipe_id = feature_body.get("source_definition_id") or feature_body.get("recipe_id")
    if recipe_id:
        row = conn.execute(
            "SELECT context_hash, planning_request_hash, binding_state, verdicts, "
            "eligibility, policy_hashes "
            "FROM semantic_candidate_observation "
            "WHERE generation_run_id = %s AND source_definition_id = %s "
            "ORDER BY recorded_at DESC LIMIT 1",
            (revision["generation_run_id"], recipe_id)).fetchone()
        if row is not None:
            detail["semantic_evidence"] = {
                "context_hash": row[0], "planning_request_hash": row[1],
                "binding_state": row[2], "verdicts": row[3],
                "eligibility": row[4], "policy_hashes": row[5],
            }
    return detail


def _considered_set_response(conn, intent, cs) -> dict:
    """Return the controlled public considered-set projection.

    Private ranking signals remain sealed in the revision and are not promoted into the API contract.
    """
    public = _public_considered_snapshot(conn, cs)
    return {
        "intent_id": intent.intent_id,
        "anchor": public["anchor"],
        "alternatives": [
            {"lens": feature_set["lens"], "features": feature_set["features"]}
            for feature_set in public["alternatives"]
        ],
        "recommendation": public["recommendation"],
        "rejections": cs.rejections,
    }


def _require_generation_llm(client: LLMClient | None) -> LLMClient:
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="no LLM provider is configured on this deployment "
            "(set FEATUREGEN_LLM_PROVIDER=anthropic to enable feature-assist)",
        )
    return client


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
    """Deterministic ranking is ALWAYS ON — the FEATUREGEN_INTENT_RANKING opt-in retired with
    the pre-live simplification (2026-08-11): a built, tested, deterministic ordering that
    nobody could see behind a dark flag is exactly the class of switch the steer removes. The
    helper remains (three call sites stamp it into provenance) but no longer reads env."""
    return True


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


def _target_for_generation(
    conn,
    *,
    intent_id: str,
    snapshot_lineage: dict | None,
) -> str | None:
    """Resolve leakage authority from the exact chosen run, with legacy fallback only in legacy mode."""
    generation_run_id = (
        snapshot_lineage.get("generation_run_id") if snapshot_lineage is not None else None)
    if generation_run_id is not None:
        try:
            sealed = generation_input_for_run(conn, generation_run_id)
        except GenerationInputUnavailable as e:
            raise HTTPException(status_code=409, detail="GENERATION_INPUT_INVALID") from e
        if sealed is not None:
            if sealed.intent_id != intent_id:
                raise HTTPException(status_code=409, detail="GENERATION_INPUT_LINEAGE_CHANGED")
            return sealed.target_ref
    if confirmation_required():
        raise HTTPException(status_code=409, detail="GENERATION_INPUT_UNAVAILABLE")
    return intent_target_ref(conn, intent_id)


def _scoped_considered_set(body: ConsideredSetIn, conn: _FeatureGenConn, identity: _Identity,
                           client: LLMClient | None) -> dict:
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
        if confirmation_required():
            if body.recognition_id is None and body.supersedes_scope_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="unscoped broaden must reference a recognition or prior confirmed scope",
                )
        scope = ConfirmedScope(
            primary=None, secondary=(), unscoped=True,
            modelling_contexts=clean_contexts, target_entity=clean_entity,
            uoa_entity=cscope.uoa_entity, spine_ref=cscope.spine_ref)
    else:
        # A ``primary`` that also appears in ``secondary`` (or a duplicated ``secondary``) would collide
        # on the ``confirmed_scope_use_case`` PK downstream → UniqueViolation → 500; reject it as a 422.
        if cscope.primary is not None and cscope.primary in cscope.secondary:
            raise HTTPException(status_code=422,
                                detail="primary use-case must not also appear in secondary")
        if len(cscope.secondary) != len(set(cscope.secondary)):
            raise HTTPException(status_code=422, detail="secondary use-cases must be unique")
        confirmed_ids = ([cscope.primary] if cscope.primary else []) + list(cscope.secondary)
        if confirmation_required() and not confirmed_ids:
            raise HTTPException(
                status_code=422,
                detail="confirmed scope requires at least one selectable use-case",
            )
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
                target_entity=clean_entity,
                uoa_entity=cscope.uoa_entity, spine_ref=cscope.spine_ref)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    # 2. Reuse the recognition's immutable intent if given, else submit a fresh (redacted) one.
    try:
        intent = submit_intent(hypothesis=body.hypothesis, definition=body.definition,
                               actor=identity.subject)
        submitted_goal = redact_free_text(body.objective, label="prediction goal")
        submitted_feedback = redact_free_text(body.feedback or "", label="feedback")
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
    effective_recognition_id = body.recognition_id
    sealed_recognition: RecognitionInput | None = None
    if confirmation_required():
        if body.recognition_id is None and body.supersedes_scope_id is None:
            raise HTTPException(
                status_code=422,
                detail="confirmed scope must reference its recognition or prior confirmed scope",
            )
        if body.supersedes_scope_id is not None:
            prior_owned = conn.execute(
                "SELECT recognition_id FROM confirmed_generation_scope "
                "WHERE scope_id = %s AND intent_id = %s AND confirmed_by = %s",
                (body.supersedes_scope_id, intent.intent_id, identity.subject),
            ).fetchone()
            if prior_owned is None:
                raise HTTPException(status_code=404, detail="unknown prior confirmed scope")
            prior_recognition_id = recognition_id_for_scope(
                conn, scope_id=body.supersedes_scope_id, intent_id=intent.intent_id)
            if effective_recognition_id is None:
                effective_recognition_id = prior_recognition_id
            elif (prior_recognition_id is not None
                  and prior_recognition_id != effective_recognition_id
                  and not submitted_feedback):
                raise HTTPException(
                    status_code=409, detail="RECOGNITION_LINEAGE_CHANGED")
        if effective_recognition_id is None:
            raise HTTPException(
                status_code=409, detail="RECOGNITION_INPUT_UNAVAILABLE")
        recognition_owned = conn.execute(
            "SELECT 1 FROM intent_recognition_attempt a "
            "JOIN contract_intent i ON i.intent_id = a.intent_id "
            "WHERE a.recognition_id = %s AND a.intent_id = %s AND i.actor = %s::jsonb",
            (effective_recognition_id, intent.intent_id, _actor_json(intent.actor)),
        ).fetchone()
        if recognition_owned is None:
            raise HTTPException(status_code=404, detail="unknown recognition")
        try:
            sealed_recognition = load_recognition_input(
                conn,
                recognition_id=effective_recognition_id,
                intent_id=intent.intent_id,
            )
        except RecognitionInputUnavailable as e:
            raise HTTPException(
                status_code=409, detail="RECOGNITION_INPUT_UNAVAILABLE") from e
        if (
            intent.redacted_hypothesis != sealed_recognition.redacted_hypothesis
            or submitted_goal != sealed_recognition.redacted_prediction_goal
            or submitted_feedback != sealed_recognition.redacted_feedback
        ):
            raise HTTPException(status_code=409, detail="RECOGNITION_INPUT_CHANGED")
        if (
            sealed_recognition.redacted_feedback
            and body.supersedes_scope_id != sealed_recognition.supersedes_scope_id
        ):
            raise HTTPException(status_code=409, detail="RECOGNITION_LINEAGE_CHANGED")
    client = _require_generation_llm(client)
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
    # B8 (PLAN-14 closed): the projection-readiness gate runs BEFORE any model dispatch — a
    # lagged catalog projection 503s here having spent ZERO provider calls. (It previously
    # fired inside the snapshot persist, AFTER generation.) Only the semantic serving mode
    # pays LLM bills in this builder, but the probe is cheap and honest for every mode.
    if body.catalog_source is not None:
        try:
            check_projection_readiness(conn)
        except CatalogProjectionUnavailable as e:
            raise HTTPException(status_code=503, detail=e.detail) from e
    # 4. Mint the generation run — the run is born only NOW, when the human commits to generate.
    generation_run_id = mint_id("grun")
    # 5. Persist the confirmed scope in the API layer, BEFORE the builder (the run→scope linkage exists
    # before any generation). The intent is durably recorded first so the lineage reads intent→run→scope.
    persist_intent(conn, intent, body.target_ref if not confirmation_required() else None)
    ensure_generation_run(
        conn,
        generation_run_id,
        identity_to_jsonb(identity),
        {
            "scoped_applicability": _intent_scoped_applicability_enabled(),
            "ranking": _intent_ranking_enabled(),
            "recipe_formula_shadow": recipe_formula_shadow_enabled(),
        },
        intent_id=intent.intent_id,
    )
    # Reconstruct each confirmed dimension's provenance from the IMMUTABLE recognition attempt (never the
    # client): a value the recognizer proposed is ``accepted_llm_proposal``, one the human introduced is
    # ``user_added``, and a corrected entity is a ``user_replacement`` recording what it superseded.
    dim_sources, dim_replaces = dimension_provenance(conn, effective_recognition_id, scope)
    use_case_origins, proposed_relationships, use_case_replacements = use_case_provenance(
        conn, effective_recognition_id, scope)
    confirmation_source = (
        "user_broadened" if scope.unscoped
        else "user_feedback" if sealed_recognition and sealed_recognition.redacted_feedback
        else "user_confirmed"
    )
    scope_id = record_confirmed_scope(
        conn, intent_id=intent.intent_id, generation_run_id=generation_run_id,
        recognition_id=effective_recognition_id, scope=scope,
        use_case_origins=use_case_origins,
        use_case_proposed_relationships=proposed_relationships,
        use_case_replacements=use_case_replacements,
        confirmation_source=confirmation_source,
        confirmed_by=identity.subject, supersedes_scope_id=body.supersedes_scope_id,
        dimension_sources=dim_sources, replaces=dim_replaces)
    sealed_generation = (
        record_generation_input(
            conn,
            generation_run_id=generation_run_id,
            intent_id=intent.intent_id,
            recognition=sealed_recognition,
            confirmed_scope_id=scope_id,
            redacted_definition=intent.redacted_definition,
            redacted_feedback=submitted_feedback,
            target_ref=body.target_ref,
            actor=identity,
        )
        if sealed_recognition is not None
        else None
    )
    run_target_ref = (
        sealed_generation.target_ref if sealed_generation is not None else body.target_ref)
    run_feedback = (
        sealed_generation.redacted_feedback
        if sealed_generation is not None else submitted_feedback)
    # 6. Compute applicability ONCE — grounding AND the disposition lens consume this single object.
    applicability = applicability_result(scope)
    # B1: an entity-only (cross-catalog) request is REFUSED typed — the semantic engine plans
    # over one frozen catalog context; until a multi-catalog context is chartered, an honest
    # refusal beats a silently empty page (E4: the legacy free-form path that used to fill it is
    # deleted, so there is no mode in which this request could be served).
    if body.catalog_source is None:
        raise HTTPException(status_code=422, detail={
            "code": "SEMANTIC_REQUIRES_CATALOG_SOURCE",
            "message": "semantic generation plans over ONE catalog; entity-only cross-catalog "
                       "scope is not yet supported — name a catalog_source",
        })
    # SE-7 part 4: the DISPOSITION universe is the V2 registry — the universe that was actually
    # planned. The legacy object still feeds the legacy machinery (shadow planner,
    # scoped-grounding narrowing) untouched.
    from featuregen.overlay.upload.recipe_planning_lens import v2_applicability_as_result

    disposition_applicability = v2_applicability_as_result(scope)
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
            conn, intent, client, catalog_source=body.catalog_source,
            roles=identity.role_claims, target_ref=run_target_ref,
            feedback=run_feedback, now=now, applicability=applicability,
            is_live=is_live, target_entity=scope.target_entity,
            generation_run_id=generation_run_id,
            # The confirmed scope the engine classifies against — with a catalog_source it is
            # what makes the engine's lens run at all.
            scope=scope,
            actor_envelope=identity)
    except CatalogProjectionUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail) from e
    except psycopg.errors.SerializationFailure as e:   # MF-2: the RR broaden race on contract_considered
        raise HTTPException(   # (ON CONFLICT (intent_id) DO UPDATE) → a designed conflict, never a 500
            status_code=409,
            detail="a concurrent request updated this intent; re-fetch and retry") from e
    # 7. The per-stage disposition lens over the MODE'S applicability universe + this run's
    #    grounding outcome (the ids the builder actually returned live in the same universe).
    dispositions = evaluate_dispositions(
        disposition_applicability, cs.grounded_template_ids, cs.rejected_template_ids,
        evaluation_version=APPLICABILITY_MAPPING_VERSION, now=now,
        incomplete=cs.incomplete_template_ids)
    # 8. Applicability OWNS the in-scope recipe count (never recognition).
    response = {**_considered_set_response(conn, intent, cs),
                "generation_run_id": generation_run_id, "scope_id": scope_id,
                "dispositions": [_disposition_json(d) for d in dispositions],
                "in_scope_count": len(disposition_applicability.eligible_ids)}
    # SE-11: the v2 contract is an EXPLICIT opt-in and the v1 response never carries the new
    # keys — no newer semantic field silently leaks into the frozen old contract (pinned).
    if body.contract_version == 2:
        response["contract_version"] = 2
        # E4: `semantic_planning_mode` is GONE from the response, not frozen to "semantic_v1".
        # It named which of three pipelines answered; there is one pipeline now, so the field
        # could only ever repeat itself — and a constant that looks like a reading is worse than
        # no reading. Clients that branched on it have nothing left to branch on.
        # Step 3's audit-drawer provenance: the immutable revision this response was minted
        # from — the address of GET /contract/considered-revisions/{id}/options/{option_id}.
        response["considered_revision_id"] = cs.considered_revision_id
        response["considered_content_hash"] = cs.considered_content_hash
        # A3: the three-section shape + per-option actions from the SAME fold the durable
        # writes consult. At serve time the current state IS generation time (the review fold,
        # pins, and snapshot were computed in this very transaction), so the current layer is
        # constructed from the frozen facts — the durable writes re-read for real later.
        from featuregen.overlay.upload.activation_policy import (
            CurrentActivationStateV1,
            FrozenOptionFactsV1,
            decide_all_actions,
        )

        recommended, actionable = [], []
        for feature_set in response["alternatives"]:
            for entry in feature_set["features"]:
                rid = entry.get("source_definition_id") or entry.get("recipe_id")
                facts = cs.semantic_decision_facts_by_definition_id.get(rid) if rid else None
                if facts is None or not entry.get("option_id"):
                    continue
                frozen = FrozenOptionFactsV1(
                    binding_state=facts["binding_state"],
                    generation_source=facts["generation_source"],
                    computation_kind=facts["computation_kind"],
                    readiness=facts["readiness"],
                    review_current=facts["review_current"],
                    source_definition_id=facts["source_definition_id"],
                    recipe_revision_hash=facts["recipe_revision_hash"],
                    confirmation_required_roles=tuple(facts["confirmation_required_roles"]),
                    has_reviewed_formula_expectation=facts[
                        "has_reviewed_formula_expectation"],
                    plan_envelope_present=facts["plan_envelope_present"],
                    validation_status=facts["validation_status"],
                    outstanding_requirement_codes=tuple(
                        facts["outstanding_requirement_codes"]),
                    plan_refusal_codes=tuple(
                        (facts.get("dataset_story") or {}).get("plan_refusals", ())))
                # C2: the serve-time execution floor is folded from the authorities the
                # engine JUST measured (the frozen operand_authorities); authoring is True
                # at the generation instant — any failure rides confirmation_required_roles.
                from featuregen.overlay.upload.semantic_eligibility import clears

                story = facts.get("dataset_story") or {}
                serve_authorities = list((story.get("operand_authorities") or {}).values())
                plan_present = facts.get("plan_envelope_present", False)
                current = CurrentActivationStateV1(
                    review_current=facts["review_current"],
                    policy_revisions_current=True,
                    snapshot_freshness="current",
                    uoa_current=True,
                    authoring_floor_met=True,
                    execution_authority_evaluated=bool(plan_present and serve_authorities),
                    execution_floor_met=bool(serve_authorities) and all(
                        clears(a or "absent", "execution_at_governed")
                        for a in serve_authorities),
                    effective_readiness=facts["readiness"])
                decisions = decide_all_actions(frozen, current)
                section_entry = {
                    "option_id": entry["option_id"],
                    "name": entry.get("name"),
                    "recipe_id": rid,
                    "binding_state": facts["binding_state"],
                    "allowed_actions": [a for a, d in decisions.items() if d.allowed],
                    "blocked_actions": {
                        a: [{"code": b.code, "next_step": b.next_step} for b in d.blockers]
                        for a, d in decisions.items() if not d.allowed},
                }
                # B10: bound-but-planless (UOA mismatch, cross-dataset…) is ACTIONABLE — a
                # candidate that cannot create_contract never sits in the recommended list.
                (recommended if (facts["binding_state"] == "bound"
                                 and facts["plan_envelope_present"])
                 else actionable).append(section_entry)
        response["recommended_options"] = recommended
        response["actionable_options"] = actionable
        response["rejected_outputs"] = cs.rejections
    # 9. Phase-2A: deterministic presentation-priority ranking over the PRECOMPUTED rankable set. The
    # rankable set (the ONLY FinalDisposition read) is decided first; the ranker then orders it, staying
    # disposition-agnostic. ``ranking_version`` is pinned BEFORE ranking (provenance, never an ordering
    # input). Flag off => neither key is present (Task-7/1B byte-identical). The ranking is deliberately
    # SEPARATE from the LLM ``recommendation`` and the human's Gate-#1 choice — three distinct layers.
    ranking_enabled = _intent_ranking_enabled()
    ranked: tuple[RankedRecipe, ...] = ()
    ranking_version: str | None = None
    if ranking_enabled:
        rankable_ids = rankable_recipe_ids(dispositions)
        signals = _rank_signals(rankable_ids, dispositions, cs, scope)
        ranking_version = APPLICABILITY_MAPPING_VERSION   # pinned BEFORE the ranker is called
        ranked = tuple(rank_eligible(
            rankable_ids, signals, ranking_version=ranking_version))
        response["ranking"] = [_ranking_json(r) for r in ranked]
        response["ranking_version"] = ranking_version
        # Task B3: the SOFT dimension warnings (grain mismatch / context conflict) surfaced per recipe.
        # This NEVER changes dispositions — a warned recipe stays exactly as eligible as it was.
        response["signal_warnings"] = _signal_warnings(signals)
    # Delivery B formula shadow: enroll only this confirmed-scope, immutable-revision path. The
    # expected-run declaration is the narrow flag-on durability interlock: without it a wholly
    # missing manifest could never be detected, so failure is an explicit 503. Everything after
    # declaration is isolated in a savepoint and remains behavior-neutral; reconciliation will
    # report a missing/incomplete manifest if capture fails.
    if recipe_formula_shadow_enabled():
        if cs.considered_revision_id is None or cs.considered_content_hash is None:
            raise HTTPException(
                status_code=503,
                detail="SHADOW_EXPECTATION_STORE_UNAVAILABLE",
            )
        try:
            declare_expected_run(
                conn,
                generation_run_id=generation_run_id,
                intent_id=intent.intent_id,
                confirmed_scope_id=scope_id,
                considered_revision_id=cs.considered_revision_id,
                considered_content_hash=cs.considered_content_hash,
                ranking_flag=ranking_enabled,
            )
        except Exception as exc:
            logger.exception("recipe formula shadow expected-run declaration failed")
            raise HTTPException(
                status_code=503,
                detail="SHADOW_EXPECTATION_STORE_UNAVAILABLE",
            ) from exc
        try:
            with conn.transaction():
                revision = conn.execute(
                    "SELECT r.metadata_snapshot_id, r.metadata_snapshot_content_hash, "
                    "s.read_scope_hash FROM contract_considered_revision r "
                    "LEFT JOIN catalog_metadata_snapshot s "
                    "ON s.snapshot_id = r.metadata_snapshot_id "
                    "WHERE r.considered_revision_id=%s",
                    (cs.considered_revision_id,),
                ).fetchone()
                if revision is None or (
                    ranking_enabled and not isinstance(revision[2], str)
                ):
                    raise ValueError("recipe formula shadow requires snapshot read-scope lineage")
                capture_ranked_shadow(
                    conn,
                    generation_run_id=generation_run_id,
                    intent_id=intent.intent_id,
                    confirmed_scope_id=scope_id,
                    considered_revision_id=cs.considered_revision_id,
                    considered_content_hash=cs.considered_content_hash,
                    metadata_snapshot_id=revision[0] if revision else None,
                    metadata_snapshot_content_hash=revision[1] if revision else None,
                    ranked=ranked,
                    ranking_version=ranking_version,
                    ranking_enabled=ranking_enabled,
                    candidate_keys_by_recipe_id=cs.recipe_candidate_keys_by_recipe_id,
                    grounding_context_by_candidate_key=(
                        cs.recipe_grounding_context_by_candidate_key),
                    identity=identity,
                    request_read_scope_hash=revision[2],
                )
        except Exception:
            logger.exception(
                "recipe formula shadow capture failed after expected-run declaration")
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
                   client: _OptionalLLM) -> dict:
    """Intake (mandatory hypothesis + optional definition, redacted) → the validated considered set:
    the anchor (from the definition) + generated alternatives + an advisory recommendation. Persists
    the intent. Every option shown has passed the gauntlet.

    When ``confirmed_scope`` is present the request mints a generation run, persists the
    confirmed scope BEFORE the builder, scopes grounding through a single ``ApplicabilityResult`` and
    attaches a per-recipe disposition lens (see :func:`_scoped_considered_set`). Release mode rejects
    an absent scope. Only the explicit legacy_unscoped emergency mode retains the old one-shot path."""
    if body.confirmed_scope is not None:
        return _scoped_considered_set(body, conn, identity, client)
    if body.contract_version == 2:
        # SE-11: the semantic candidate contract needs the scoped pipeline (mode resolution,
        # dispositions, the semantic engine). The emergency unscoped path stays frozen at v1.
        raise HTTPException(
            status_code=422,
            detail="contract_version 2 requires a confirmed_scope")
    if confirmation_required():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SCOPE_CONFIRMATION_REQUIRED",
                "message": (
                    "recognize and confirm a use-case scope before generating candidates; "
                    "use an explicit broaden action to inspect all buildable recipes"
                ),
            },
        )
    counters.incr("contract.legacy_unscoped_requests")
    logger.warning(
        "legacy unscoped considered-set request accepted: actor=%s catalog=%s",
        identity.subject,
        body.catalog_source,
    )
    client = _require_generation_llm(client)
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
            conn, intent, client, catalog_source=body.catalog_source,
            roles=identity.role_claims, target_ref=body.target_ref,
            feedback=body.feedback, now=datetime.now(UTC), is_live=is_live)
    except CatalogProjectionUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail) from e
    except psycopg.errors.SerializationFailure as e:   # MF-2: the RR broaden race on contract_considered
        raise HTTPException(   # (ON CONFLICT (intent_id) DO UPDATE) → a designed conflict, never a 500
            status_code=409,
            detail="a concurrent request updated this intent; re-fetch and retry") from e
    return _considered_set_response(conn, intent, cs)


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
        redacted_goal = redact_free_text(body.objective, label="prediction goal")
        redacted_feedback = redact_free_text(body.feedback or "", label="feedback")
    except IntentValidationError as e:   # a free-text field that cannot be safely redacted -> denial
        raise HTTPException(status_code=422, detail=str(e)) from e
    if bool(redacted_feedback) != bool(body.supersedes_scope_id):
        raise HTTPException(
            status_code=422,
            detail="feedback recognition requires exactly one prior confirmed scope",
        )
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
    if body.supersedes_scope_id is not None:
        prior_scope = conn.execute(
            "SELECT 1 FROM confirmed_generation_scope "
            "WHERE scope_id = %s AND intent_id = %s AND confirmed_by = %s",
            (body.supersedes_scope_id, intent.intent_id, identity.subject),
        ).fetchone()
        if prior_scope is None:
            raise HTTPException(status_code=404, detail="unknown prior confirmed scope")

    input_json = recognition_input_material(
        redacted_hypothesis=intent.redacted_hypothesis,
        redacted_prediction_goal=redacted_goal,
        redaction_policy_version=REDACTION_VERSION,
        redacted_feedback=redacted_feedback,
        supersedes_scope_id=body.supersedes_scope_id,
    )
    input_hash = compute_input_hash(input_json)
    result = recognize(conn, client, redacted_hypothesis=intent.redacted_hypothesis,
                       redacted_goal=redacted_goal, redacted_feedback=redacted_feedback,
                       actor=identity)
    recognition_id = record_recognition_attempt(
        conn, intent_id=intent.intent_id, input_hash=input_hash, result=result,
        actor=identity.subject, input_json=input_json,
        redaction_policy_version=REDACTION_VERSION)
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


def _intent_row(conn, intent_id: str):
    return conn.execute(
        "SELECT actor, target_provenance, target_ref FROM contract_intent WHERE intent_id = %s",
        (intent_id,)).fetchone()


@router.post("/contract/intake", dependencies=[Depends(require_feature_generate)])
def intake(body: IntakeIn, conn: _Conn, identity: _Identity, client: _OptionalLLM) -> dict:
    """The mandatory read: persist the intent (per-actor idempotent, the recognitions discipline),
    extract the ticket (ONE cached governed call — replay is free), and return the DRAFT reading
    for the confirm screen. A literally-typed name is recorded server-side as ``user_typed``
    immediately (shows-doesn't-gate: human-origin by construction, no click required) — but NEVER
    over a reading a human already signed. Degrades, never blocks: with no LLM configured the
    pinned target still lands and everything else honestly abstains."""
    try:
        intent = submit_intent(hypothesis=body.hypothesis, actor=identity.subject)
    except IntentValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    prior = conn.execute(
        "SELECT intent_id FROM contract_intent WHERE hypothesis = %s AND intake_mode = %s "
        "AND actor = %s::jsonb ORDER BY created_at ASC LIMIT 1",
        (intent.hypothesis, intent.intake_mode, _actor_json(intent.actor))).fetchone()
    if prior is not None:
        intent = replace(intent, intent_id=prior[0])
    persist_intent(conn, intent)

    ticket, reason = extract_intake_ticket(
        conn, client, hypothesis=body.hypothesis, catalog_source=body.catalog_source,
        roles=identity.role_claims, actor=identity)
    counters.incr(f"overlay.intake.{reason}")

    row = _intent_row(conn, intent.intent_id)
    # The pin is the USER's own typed name — record it without a click. A NEW pin may overwrite a
    # PRIOR pin (both are code-derived from the user's own current text; refusing left the stored
    # target on a column the catalog no longer resolves to while the screen showed the new one —
    # review fix 2026-08-10). A HUMAN-signed reading (human_confirmed / exploring) is never
    # clobbered: re-running intake on a decided intent stays a read.
    if (ticket.pinned and ticket.target_column
            and (row is None or row[1] in (None, "user_typed"))
            and (row is None or row[2] != ticket.target_column or row[1] is None)):
        record_target_reading(conn, intent_id=intent.intent_id, provenance="user_typed",
                              target_ref=ticket.target_column, confirmed_by=identity.subject)
        counters.incr("overlay.intake.pinned")

    def _column_detail(ref: str | None) -> dict | None:
        if not ref:
            return None
        detail = conn.execute(
            "SELECT catalog_source, concept, ai_summary FROM graph_node "
            "WHERE kind = 'column' AND object_ref = %s "
            "AND (%s::text IS NULL OR catalog_source = %s) LIMIT 1",
            (ref, body.catalog_source, body.catalog_source)).fetchone()
        if detail is None:
            return None
        return {"ref": ref, "catalog_source": detail[0], "concept": detail[1] or "",
                "ai_summary": detail[2] or ""}

    return {"intent_id": intent.intent_id, "reason": reason,
            "ticket": {"target_column": ticket.target_column,
                       "target_window_days": ticket.target_window_days,
                       "target_type": ticket.target_type,
                       "business_domain": list(ticket.business_domain),
                       "confidence": ticket.confidence, "pinned": ticket.pinned,
                       "contradiction": ticket.contradiction,
                       "runners_up": list(ticket.runners_up)},
            "target_detail": _column_detail(ticket.target_column),
            # the Change-it menu, ranked, with the same one-liner material as the main line
            "runner_up_details": [d for r in ticket.runners_up
                                  if (d := _column_detail(r)) is not None]}


@router.post("/contract/intake/target", dependencies=[Depends(require_feature_generate)])
def intake_target(body: IntakeTargetIn, conn: _Conn, identity: _Identity) -> dict:
    """Record the HUMAN's answer — the point where provenance flips to a person. Author-only: the
    reading governs the author's own leakage gate, so another principal cannot sign it. The signed
    ref is validated against the read-scoped catalog (never the ticket — the human may correct to
    any column they can see); the domain tokens are validated against the closed use-case
    vocabulary STRICTLY (a human decision is recorded verbatim or refused, never silently edited).
    The existing server-side leakage path (``intent_target_ref``) reads the same row, so the veto
    downstream runs on the signed value with no further wiring."""
    row = _intent_row(conn, body.intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such intent")
    if row[0] != identity.subject:
        raise HTTPException(status_code=403,
                            detail="only the intent's author can sign its target reading")
    if body.decision in ("confirmed", "corrected"):
        if not body.target_ref:
            raise HTTPException(status_code=422,
                                detail=f"decision '{body.decision}' requires target_ref")
        if not is_readable_column(conn, body.target_ref, roles=identity.role_claims,
                                  catalog_source=body.catalog_source):
            raise HTTPException(status_code=422,
                                detail="target_ref is not a readable column in this catalog")
    vocabulary = set(_use_case_vocabulary())
    off_vocab = [d for d in body.business_domain if d not in vocabulary]
    if off_vocab:
        raise HTTPException(
            status_code=422,
            detail=f"business_domain outside the use-case vocabulary: {sorted(off_vocab)}")
    provenance = "exploring" if body.decision == "exploring" else "human_confirmed"
    record_target_reading(
        conn, intent_id=body.intent_id, provenance=provenance, target_ref=body.target_ref,
        target_window_days=body.target_window_days, target_type=body.target_type,
        business_domain=tuple(body.business_domain), confirmed_by=identity.subject)
    counters.incr(f"overlay.intake.target_{body.decision}")
    return {"intent_id": body.intent_id, **(target_reading(conn, body.intent_id) or {}),
            "business_domain": sorted(body.business_domain)}


@router.post("/contract/draft", dependencies=[Depends(require_feature_generate)])
def draft(body: DraftReqIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Gate #1 → author. The chosen feature is reconstructed from the SERVER-persisted considered set
    (BLOCKER 1 — never an arbitrary client payload); the choice is recorded (audit); the leakage target
    is read SERVER-side (BLOCKER 2). Then draft + the critique→refine loop (MCV each pass)."""
    owned = conn.execute(
        "SELECT 1 FROM contract_intent WHERE intent_id = %s AND actor = %s::jsonb",
        (body.intent_id, _actor_json(identity.subject)),
    ).fetchone()
    if owned is None:
        raise HTTPException(status_code=404, detail="unknown intent")
    try:
        choice = select_and_record_gate1_choice(
            conn,
            body.intent_id,
            chosen_source=body.chosen_source,
            chosen_option_id=body.chosen_option_id,
            actor=identity.subject,
            why=body.why,
            expected_generation_run_id=body.expected_generation_run_id,
        )
    except UnknownConsideredOption as e:
        # A stale tab naming an option from a superseded revision is a CLIENT error, not evidence of
        # a corrupted record — same 422 the unscoped path returns for the same mistake, so the two
        # modes stay consistent and an integrity 409 keeps meaning integrity.
        raise HTTPException(
            status_code=422,
            detail="chosen option is not in the recorded considered set for this intent") from e
    except Gate1Error as e:
        if str(e) == "REGENERATE_FROM_CURRENT_CONSIDERED_SET":
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.warning(
            "considered revision verification failed for intent %s (run %s): %s",
            body.intent_id, body.expected_generation_run_id, e)
        raise HTTPException(status_code=409, detail="considered revision verification failed") from e
    if choice is None:
        raise HTTPException(status_code=422,
                            detail="chosen option is not in the recorded considered set for this intent")
    # SE-11 step 6: the option's sealed metadata snapshot (which carries the frozen semantic
    # context pin) is re-verified at the moment of choice — catalog drift since generation is a
    # typed 409 asking for regeneration, never a silent draft over a world that no longer
    # exists. "Unverifiable" is logged, not refused: compatibility snapshots (pre-C0 lineage,
    # kinds this build cannot re-derive) must not brick drafting — absence of proof is not
    # proof of drift.
    lineage_snapshot_id = (choice.snapshot_lineage or {}).get("snapshot_id")
    # A2 slice 2 — the activation fold at the durable write. An option with an A1b decision
    # row is a SEMANTIC option: its frozen facts + the current-state re-read go through
    # activation_decision, which owns EVERY create_contract rule (snapshot freshness included,
    # failing CLOSED on unverifiable — the semantic workflow has no compatibility debt).
    # An option WITHOUT a decision row is a legacy/free-form candidate: it keeps the standalone
    # drift check below until B1/E4 retire that path entirely.
    frozen = None
    if choice.considered_revision_id and choice.option_id:
        from featuregen.overlay.upload.semantic_option_decision import (
            assemble_current_activation_state,
            load_frozen_option_facts,
        )

        frozen = load_frozen_option_facts(
            conn, considered_revision_id=choice.considered_revision_id,
            option_id=choice.option_id)
    if frozen is not None:
        current = assemble_current_activation_state(
            conn, frozen=frozen, snapshot_id=lineage_snapshot_id,
            intent_id=body.intent_id)
        decision = activation_decision(frozen, current, "create_contract",
                                       actor=identity.subject)
        if not decision.allowed:
            raise HTTPException(status_code=409, detail={
                "code": "ACTIVATION_BLOCKED",
                "action": "create_contract",
                "blockers": [{"code": b.code, "next_step": b.next_step}
                             for b in decision.blockers],
            })
    elif lineage_snapshot_id:
        freshness = compare_snapshot_to_current(conn, lineage_snapshot_id)
        if freshness.status == "drifted":
            raise HTTPException(status_code=409, detail={
                "code": "SEMANTIC_SNAPSHOT_STALE",
                "message": "the catalog drifted since this considered set was generated; "
                           "regenerate from the current considered set",
                "reason": freshness.reason,
            })
        if freshness.status == "unverifiable":
            logger.warning("considered snapshot %s unverifiable at draft time: %s",
                           lineage_snapshot_id, freshness.reason)
    feature = choice.feature
    target = _target_for_generation(
        conn, intent_id=body.intent_id, snapshot_lineage=choice.snapshot_lineage)
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
    snapshot = choice.snapshot_lineage
    # H1b — expose the exact role bindings (role / column-ref / source / authority / warnings) + the
    # overall binding_hash the human is confirming. The confirm requires this hash and 409s if the
    # server's authoritative bindings drift before finalize (see /contract/confirm). Computed over the
    # SERVER-authoritative reconciled draft `d`, so it equals the confirm-time recompute unless the
    # underlying catalog state actually drifts. READ-ONLY (no global authority write).
    bindings = confirmed_role_bindings(conn, d)
    return {
        "draft": d,
        "unresolved": unresolved,
        "intent_id": body.intent_id,
        "choice_id": choice.choice_id,
        "snapshot": snapshot,
        "bindings": binding_exposure(bindings),
        "binding_hash": binding_hash(bindings),
    }


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
    owned = conn.execute(
        "SELECT 1 FROM contract_intent WHERE intent_id = %s AND actor = %s::jsonb",
        (body.intent_id, _actor_json(identity.subject)),
    ).fetchone()
    if owned is None:
        raise HTTPException(status_code=422, detail="unknown intent")
    if confirmation_required():
        if body.choice_id is None:
            raise HTTPException(
                status_code=422, detail="choice_id is required to govern a scoped contract")
        try:
            recorded_choice = recorded_gate1_choice_revision(
                conn,
                choice_id=body.choice_id,
                intent_id=body.intent_id,
                actor=identity.subject,
            )
        except Gate1Error as e:
            raise HTTPException(
                status_code=409, detail="considered revision verification failed") from e
    else:
        choice = gate1_choice(conn, body.intent_id)
        if choice is None:
            raise HTTPException(
                status_code=422,
                detail="no Gate #1 choice recorded for this intent — draft it first")
        try:
            recorded_choice = recorded_gate1_draft_choice(conn, body.intent_id)
        except Gate1Error as e:
            raise HTTPException(
                status_code=409, detail="considered revision verification failed") from e
    if recorded_choice is None:
        raise HTTPException(status_code=422,
                            detail="the chosen feature is not in the recorded considered set")
    # A2 slice 3 — the confirm re-check (draft-then-confirm race defense): the SAME activation
    # fold that gated the draft runs again at the GOVERNING write, over the same frozen
    # decision row and a FRESH current-state re-read. A review revoked, a policy moved, or a
    # snapshot drifted between draft and confirm blocks HERE, with the same typed shape.
    if recorded_choice.considered_revision_id and recorded_choice.option_id:
        from featuregen.overlay.upload.semantic_option_decision import (
            assemble_current_activation_state,
            load_frozen_option_facts,
        )

        frozen = load_frozen_option_facts(
            conn, considered_revision_id=recorded_choice.considered_revision_id,
            option_id=recorded_choice.option_id)
        if frozen is not None:
            current = assemble_current_activation_state(
                conn, frozen=frozen,
                snapshot_id=(recorded_choice.snapshot_lineage or {}).get("snapshot_id"),
                intent_id=body.intent_id)
            decision = activation_decision(frozen, current, "create_contract",
                                           actor=identity.subject)
            if not decision.allowed:
                raise HTTPException(status_code=409, detail={
                    "code": "ACTIVATION_BLOCKED",
                    "action": "create_contract",
                    "blockers": [{"code": b.code, "next_step": b.next_step}
                                 for b in decision.blockers],
                })
    chosen = recorded_choice.feature
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
    # E4b: `operand_roles` joins the same server-authoritative overwrite. The confirm-time MCV re-runs
    # the gauntlet from this draft, and the unit/currency needs-check is now role-aware, so without
    # them the GOVERNED contract would record NEEDS_EXTERNAL_VALIDATION for a feature Gate #1 showed as
    # DESIGN_CHECKED. They are taken ONLY from the server-reconstructed chosen candidate (restored from
    # the revision's private grounding context) — `DraftIn` has no such field, so a client can never
    # declare a role and suppress a unit check.
    # D14 (review F2): `personal_data_policy_revision_ids` joins the same server-authoritative
    # overwrite for the same reason — `DraftIn` has no such field, so a client can never DECLARE a
    # licence it was not granted. What lands on the contract is not even this value: the confirm-time
    # MCV below re-consults the gate against the LIVE policy store and `confirm_contract` persists
    # THAT answer, so a revocation (or a re-approval, which mints a different revision id) between
    # Gate #1 and confirm is recorded rather than papered over. This carries Gate #1's reading only
    # so the pre-MCV draft is honest about what it was drafted under.
    draft = replace(draft, grain_table=chosen.grain_table,
                    derives_from=list(chosen.derives_from),
                    as_of_column=_as_of_column(conn, chosen.grain_table, _grain_catalog),
                    operand_roles=chosen.operand_roles,
                    personal_data_policy_revision_ids=chosen.personal_data_policy_revision_ids)
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
        draft = replace(
            draft,
            join_path=tuple(_envelope_join_path(
                env.ordered_path,
                env.bridge_realization_dependencies,
            )),
        )
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
    target = _target_for_generation(
        conn, intent_id=body.intent_id, snapshot_lineage=recorded_choice.snapshot_lineage)
    # Delivery C0 Task 5: reload the SERVER snapshot lineage the considered set was authored against and
    # bind the governing write to it in the audit trail (a regulator can prove EXACTLY what catalog state
    # this contract was authored against). Reloaded from the server considered-set row — the confirm
    # request model (DraftIn) carries no client snapshot id, so no client value is ever trusted. ADDITIVE:
    # the confirm-time MCV re-run + Slice-3 tamper-fix (grain_table/derives_from/join_path above) are
    # UNCHANGED — re-sourcing the validator onto the snapshot is a later delivery (C2–C4/H).
    lineage = recorded_choice.snapshot_lineage
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
                                snapshot_lineage=lineage if confirmation_required() else None,
                                confirmed_binding_hash=current_binding_hash,
                                # H3c — the governed plan (from the SERVER-reconstructed chosen feature,
                                # never the client body): confirm_contract REBUILDS it against the current
                                # snapshot, requires the SAME physical_plan_id + declaration id + a fresh
                                # verdict, and persists its full read set as role-labelled lineage.
                                plan_envelope=env)
    except GovernedPlanDrift as e:   # H3c — the rebuilt plan drifted (id / freshness) → regenerate
        raise HTTPException(status_code=409, detail="plan drifted, regenerate") from e
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ContractPointerConflict as e:   # M-a: the pointer CAS lost a race -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract pointer conflict occurred; re-fetch and retry") from e
    except psycopg.errors.UniqueViolation as e:   # concurrent double-confirm -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract version conflict occurred; re-fetch and retry") from e

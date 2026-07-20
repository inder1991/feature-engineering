"""Phase 2 — Gate #1 bridge.

Runs the DISCOVERY loop from the redacted hypothesis into a *considered set* — the anchor (the
requester's definition, grounded + gauntlet-validated) alongside generated alternatives (also each
gauntlet-validated) plus an advisory recommendation — then records the human's confirmed choice
(who + why + the full considered set). This is the human-validation gate: **no contract is authored
without a recorded choice here**, in both definition and hypothesis-only modes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

from featuregen.idgen import mint_id
from featuregen.intake.llm import LLMClient
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract._serial import (
    requirements_from_json,
    requirements_to_json,
)
from featuregen.overlay.upload.contract.intake import Intent, redact_free_text
from featuregen.overlay.upload.feature_assist import (
    ExternalRequirementPreview,
    FeatureIdea,
    FeatureSet,
    RoleBinding,
    SetRecommendation,
    _candidate_columns,
    _validate_idea,
    recommend_feature_sets_report,
    recommend_features,
    recommend_set,
    set_signals,
)
from featuregen.overlay.upload.feature_metadata_snapshot import build_metadata_snapshot
from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    BindingPlanV1,
    ContractResolutionStatus,
    PathResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget, build_compiler_context
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.plan_envelope import (
    PlanEnvelopeV1,
    plan_envelope_from_result,
)
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.planner.shadow import COMPILE_BUDGET, MAX_COMPILES_PER_RUN
from featuregen.overlay.upload.taxonomy.applicability import ApplicabilityResult
from featuregen.overlay.upload.taxonomy.ranking_signals import binding_quality
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    GroundedFeature,
    Template,
    ground_all,
)

logger = logging.getLogger(__name__)

# 3C.2a — the fail-closed cross-catalog invariant. On a live entity-scoped run EVERY customer-visible
# cross-catalog feature must have a governed physical plan, so an LLM alternative whose derives span
# more than one catalog (which has NO such plan) can never be a recommendation — it is surfaced as a
# rejection carrying this reason string instead.
GOVERNED_CROSS_CATALOG_PLAN_REQUIRED = "governed_cross_catalog_plan_required"


class Gate1Error(Exception):
    """A malformed or out-of-set Gate #1 confirmation."""


@dataclass(frozen=True, slots=True)
class ConsideredSet:
    intent_id: str
    anchor: FeatureIdea | None                    # the requester's definition, validated (definition mode)
    alternatives: list[FeatureSet]                # generated, each fully gauntlet-validated
    recommendation: SetRecommendation | None      # advisory — fit vs hypothesis, not a performance claim
    rejections: list[dict] = field(default_factory=list)   # what the gauntlet threw out + why (Gate-#3
    #                                                        transparency the Workbench renders)
    applicability: ApplicabilityResult | None = None       # the ONE applicability decision that scoped
    #   grounding (Task 4), carried through so Task 5's disposition stage consumes the SAME object — not
    #   persisted here (the API layer owns scope-record lifecycle, Task 7).
    grounded_template_ids: frozenset[str] = field(default_factory=frozenset)   # template ids whose
    #   grounded candidate SURVIVED the gauntlet (the `ideas`) — the disposition stage's `grounded_ids`.
    rejected_template_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)   # template id ->
    #   the gauntlet reject codes for candidates it REFUSED (safety/leakage/units) — feeds `rejected`.
    binding_quality_by_template: dict[str, str] = field(default_factory=dict)   # template id ->
    #   BindingQuality.value for each SURVIVING grounded candidate (Task A3 Part A) — the ranker's
    #   binding-quality signal. Additive + read-only: grounding behaviour is unchanged and nothing else
    #   reads it (the ranker consumes it in the API layer only when FEATUREGEN_INTENT_RANKING is on).


def persist_intent(conn, intent: Intent, target_ref: str | None = None) -> None:
    """Durably record the intent — the mandatory hypothesis is the feature's premise (M6) and the
    `target_ref` is the SERVER's source of truth for the leakage gate (draft/confirm read it from here,
    not from a client-omittable field). Idempotent."""
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, definition, intake_mode, "
        "redacted_hypothesis, redacted_definition, actor, target_ref) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (intent_id) DO NOTHING",
        (intent.intent_id, intent.hypothesis, intent.definition, intent.intake_mode,
         intent.redacted_hypothesis, intent.redacted_definition, _actor_json(intent.actor),
         target_ref))


def intent_target_ref(conn, intent_id: str) -> str | None:
    """The server-recorded prediction target for an intent — the leakage gate's source of truth."""
    row = conn.execute("SELECT target_ref FROM contract_intent WHERE intent_id = %s",
                       (intent_id,)).fetchone()
    return row[0] if row else None


# ── B4: parametric templates as a second candidate source ─────────────────────────────────────────
# The grounded templates enter the considered set as an ALTERNATIVE lens, alongside the LLM's proposals
# — the two-source model (templates ∪ LLM). Grounding is deterministic (no LLM); each grounded candidate
# is run through the SAME per-idea gauntlet the LLM candidates cleared, so both sources are judged
# identically. Use-case recognition / regulatory filtering is B3 (out of scope here): the source is the
# whole ALL_TEMPLATES registry (every family), and grounding is the router — a family surfaces ONLY where
# its distinctive concepts exist in the catalog (a churn-shaped catalog yields exactly the churn lens).
_MAX_RATIONALE = 200


def _idea_from_grounded(gf: GroundedFeature, template: Template) -> FeatureIdea:
    """A B2 GroundedFeature -> a Gate-1 FeatureIdea in the SAME shape the LLM proposes, so both sources
    run the identical gauntlet and snapshot identically. Carries the transient DESIGN-CHECKED
    verification stamp the LLM candidates also carry (structurally safe; predictive value unverified)."""
    rationale = f"template {gf.template_id}: {template.intent}".strip()[:_MAX_RATIONALE]
    return FeatureIdea(
        name=gf.name, description=template.intent,
        derives_from=[ref for _src, ref in gf.derives_pairs],
        aggregation=gf.aggregation, grain_table=gf.grain_table,
        derives_pairs=gf.derives_pairs, verification="DESIGN-CHECKED",
        critic_note="", rationale=rationale)


def _template_candidates(conn, *, catalog_source: str, roles, target_ref: str | None, now,
                         templates: Sequence[Template] = ALL_TEMPLATES,
                         fresh_within: timedelta = timedelta(hours=24),
                         ) -> tuple[list[FeatureIdea], list[dict],
                                    frozenset[str], dict[str, tuple[str, ...]], dict[str, str]]:
    """Ground ``templates`` on this catalog and gauntlet-check each grounded candidate the SAME way LLM
    candidates are (feature_assist._validate_idea, over the identical read-scoped candidate universe).
    ``templates`` defaults to the whole ``ALL_TEMPLATES`` registry (today's behaviour); Phase-1B scoped
    grounding passes a pre-narrowed eligible subset instead (never widening — the subset is always ⊆
    ALL_TEMPLATES). Grounding is the router — a template family surfaces only where its distinctive
    concepts exist, so a churn-shaped catalog yields exactly the churn lens. Grounding refuses tagged
    leakage anchors by construction, but the intent's SPECIFIC target_ref may not be a tagged anchor —
    the reused gauntlet still rejects any candidate that binds it (plus freshness / additivity / PIT /
    units). Returns (surviving ideas, {name, reason, code} rejects, grounded template ids, rejected
    template ids -> reject codes). ``ground_all`` yields at most one grounded candidate per template, so
    every ``gf.template_id`` lands in exactly one of the two id collections — the disposition stage
    (Task 5) consumes them as its ``grounded_ids`` / ``rejected`` inputs. Additionally returns the
    per-SURVIVING-template ``binding_quality`` value (Task A3 Part A) — a read-only presentation signal
    the ranker consumes; grounding behaviour is unchanged by computing it."""
    grounded = ground_all(conn, templates, catalog_source=catalog_source, roles=roles)
    if not grounded:
        return [], [], frozenset(), {}, {}
    by_id = {t.id: t for t in templates}
    cols = _candidate_columns(conn, catalog_source, roles)   # the SAME candidate universe the LLM saw
    known = {c["object_ref"] for c in cols}
    src_of: dict[str, set[str]] = {}
    for c in cols:
        src_of.setdefault(c["object_ref"], set()).add(c["catalog_source"])
    ideas: list[FeatureIdea] = []
    rejections: list[dict] = []
    grounded_ids: set[str] = set()                       # templates whose candidate SURVIVED the gauntlet
    rejected_ids: dict[str, tuple[str, ...]] = {}        # templates the gauntlet REFUSED -> its code
    binding_by_id: dict[str, str] = {}                   # SURVIVING template -> BindingQuality.value
    for gf in grounded:
        idea = _idea_from_grounded(gf, by_id[gf.template_id])
        raw = {"name": idea.name, "description": idea.description,
               "derives_from": list(idea.derives_from), "aggregation": idea.aggregation,
               "grain_table": idea.grain_table, "rationale": idea.rationale}
        validated, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within,
                                        roles=roles)
        if rej is None:
            # [F9] keep the VALIDATOR's idea (carries status + requirements), then SERVER-STAMP the H1a
            # recipe provenance: generation_source + recipe_id come from the grounded TEMPLATE id (the
            # server's own knowledge of the recipe path), never from the LLM/candidate raw. recipe_id
            # then survives the Gate-1 considered-set round-trip (persist → reload) via the (de)serializers.
            ideas.append(replace(validated, generation_source="recipe", recipe_id=gf.template_id,
                                 planner_applicability="not_applicable_single_catalog"))
            grounded_ids.add(gf.template_id)
            binding_by_id[gf.template_id] = binding_quality(gf).value   # ranker's binding signal
        else:
            rejections.append({"name": idea.name, "reason": rej.message, "code": rej.code})
            rejected_ids[gf.template_id] = (rej.code,)
    return ideas, rejections, frozenset(grounded_ids), rejected_ids, binding_by_id


# ── Phase-1B Task 4: scoped grounding (flag-gated, default off) ─────────────────────────────────────
# When FEATUREGEN_INTENT_SCOPED_APPLICABILITY=1, a supplied ApplicabilityResult narrows the template
# universe grounding evaluates to the eligible recipe subset. The flag defaults OFF → grounding sees the
# whole ALL_TEMPLATES registry, byte-identical to today. The narrowing NEVER widens (the eligible set is
# ⊆ ALL_TEMPLATES) and NEVER relaxes safety (grounding still refuses leakage/protected columns by
# construction). Recognition/applicability is computed once in the API layer (Tasks 6/7); the builder is
# a pure consumer here. See docs/superpowers/plans/2026-07-10-phase1b-scoped-grounding.md Task 4.
def _intent_scoped_applicability_enabled() -> bool:
    """Scoped grounding is OFF by default — ``build_considered_set`` grounds ``ALL_TEMPLATES`` unchanged
    unless a deployment opts in with ``FEATUREGEN_INTENT_SCOPED_APPLICABILITY=1``."""
    return os.environ.get("FEATUREGEN_INTENT_SCOPED_APPLICABILITY", "0") == "1"


def _templates_to_ground(intent: Intent,
                         applicability: ApplicabilityResult | None) -> Sequence[Template]:
    """The template subset grounding evaluates for this run. Grounds only the applicability's
    ``eligible_ids`` when ALL of: the scoped-applicability flag is on; an ``applicability`` is supplied;
    the intent is NOT definition-mode (definition bypasses recognition/applicability, never grounding);
    and the applicability GENUINELY NARROWS — its eligible set is strictly smaller than the full registry
    (an unscoped/all-eligible result is not a narrowing and fails open to full grounding). Otherwise
    returns ``ALL_TEMPLATES`` — today's behaviour, byte-identical."""
    if (_intent_scoped_applicability_enabled()
            and applicability is not None
            and intent.intake_mode != "definition"
            and len(applicability.eligible_ids) < len(ALL_TEMPLATES)):
        return tuple(t for t in ALL_TEMPLATES if t.id in applicability.eligible_ids)
    return ALL_TEMPLATES


# ── Phase-3C.2a Task 5: the LIVE governed cross-catalog lens ───────────────────────────────────────
# On a flag-on-and-activation-approved entity-scoped run (no single catalog to ground on), the governed
# cross-catalog PLANNER — not the LLM — is the authority for cross-catalog features: every option it
# surfaces carries a governed physical plan, and every LLM alternative that spans >1 catalog is rejected
# (it has no such plan). Authority is a STRUCTURED FIELD on the idea (origin / path_authority), NEVER the
# lens name. The route resolves the flag; the builder is handed the resolved ``is_live`` boolean.
def _plan_read_set_pairs(plan: BindingPlanV1) -> tuple[tuple[str, str], ...]:
    """The (catalog_source, object_ref) pairs the governed plan READS — its physical read-set (every
    column the contract would touch: ingredients + join/bridge keys + anchors), falling back to the
    ingredient bindings when a plan carries no read-set. Deduped + sorted so the idea is deterministic."""
    if plan.physical_read_set is not None and plan.physical_read_set.columns:
        pairs = {(c.catalog_source, c.object_ref) for c in plan.physical_read_set.columns}
    else:
        pairs = {(b.bound_catalog_source, b.bound_object_ref) for b in plan.ingredient_bindings}
    return tuple(sorted(pairs))


def _governed_rejection_reason(result: BindingPlanningResultV1) -> str:
    """The primary reason a recipe has no SELECTED RESOLVED governed cross-catalog contract: the best
    compiled-but-unresolved plan's contract reason, else the fail-closed source→target REJECT reason,
    else a result-level assembler reason (the tier-1 selection reasons are stripped — they say nothing
    about the cross-catalog outcome), else the observed contract status."""
    pid = result.selected_contract_physical_plan_id
    if pid is not None:
        plan = next((p for p in result.candidate_plans if p.physical_plan_id == pid), None)
        if plan is not None and plan.contract_primary_reason_code is not None:
            return plan.contract_primary_reason_code.value
    for p in result.candidate_plans:
        if (p.path_resolution_status is PathResolutionStatus.source_to_target_rejected
                and p.primary_reason_code is not None):
            return p.primary_reason_code.value
    cross = [rc for rc in result.reason_codes
             if rc not in (ReasonCode.selected_best_single_catalog,
                           ReasonCode.ambiguous_multiple_equal_plans)]
    if cross:
        return cross[0].value
    return result.contract_result_status.value


def _governed_idea_from_result(result: BindingPlanningResultV1, template: Template,
                               target_entity: str) -> FeatureIdea | None:
    """A SELECTED RESOLVED governed contract plan → a Gate-#1 :class:`FeatureIdea` carrying the exact
    compiled plan envelope (so drafting reconstructs the governed path, never a permissive one) and the
    STRUCTURED provenance (``origin`` / ``path_authority``). None when the run has no resolved contract
    plan — the caller then surfaces a rejection instead."""
    if (result.contract_result_status is not ContractResolutionStatus.resolved
            or result.selected_contract_physical_plan_id is None):
        return None
    plan = next((p for p in result.candidate_plans
                 if p.physical_plan_id == result.selected_contract_physical_plan_id), None)
    if plan is None:
        return None
    envelope = plan_envelope_from_result(result)
    if envelope is None:   # a resolved contract always projects an envelope; fail closed if it cannot
        return None
    pairs = _plan_read_set_pairs(plan)
    rationale = (f"governed cross-catalog plan for {template.id} at {target_entity} grain")[:_MAX_RATIONALE]
    return FeatureIdea(
        name=template.id, description=template.intent,
        derives_from=[ref for _cs, ref in pairs], aggregation=template.aggregation,
        grain_table=None, derives_pairs=pairs, verification="DESIGN-CHECKED", critic_note="",
        rationale=rationale, plan_envelope=envelope,
        origin="governed_planner", path_authority="governed_cross_catalog",
        # H1a: the governed cross-catalog path is a RECIPE path with a compiled physical plan. Derive the
        # H1a metadata from the SERVER's envelope — planner_applicability is "applicable_cross_catalog"
        # BECAUSE a governed plan_envelope is present (the path_authority↔planner_applicability mapping).
        generation_source="recipe", recipe_id=envelope.recipe_id,
        planner_applicability="applicable_cross_catalog", physical_plan_id=envelope.physical_plan_id)


def _governed_cross_catalog_options(conn, *, target_entity: str, eligible_recipe_ids,
                                    roles=(), now, templates: Sequence[Template] | None = None,
                                    ) -> tuple[list[FeatureIdea], list[dict]]:
    """Resolve the run scope ONCE, compile each eligible recipe's binding plan (compile ON), and split
    the outcomes: a SELECTED RESOLVED contract plan becomes a governed :class:`FeatureIdea`; anything
    unresolved becomes a rejection dict ``{lens, reason, recipe_id}`` carrying its primary reason code.
    A per-recipe savepoint isolates a planner DB error (it becomes a rejection, never poisons the
    request txn nor 500s the whole considered set)."""
    roles = tuple(roles)
    tmpls = templates if templates is not None else ALL_TEMPLATES
    by_id = {t.id: t for t in tmpls}
    scope = resolve_catalog_scope(conn, roles=roles, target_entity=target_entity, now=now)
    compile_ctx = build_compiler_context(conn, scope, roles, now)
    budget = CompileBudget(remaining=MAX_COMPILES_PER_RUN,
                           deadline_monotonic=time.monotonic() + COMPILE_BUDGET.total_seconds(),
                           clock=time.monotonic)
    ideas: list[FeatureIdea] = []
    rejections: list[dict] = []
    for rid in sorted(eligible_recipe_ids):
        tmpl = by_id.get(rid)
        if tmpl is None:
            continue
        try:
            with conn.transaction():   # per-recipe savepoint — a planner DB error must not poison the txn
                result = plan_bindings(conn, template=tmpl, target_entity=target_entity, scope=scope,
                                       roles=roles, now=now, compile_ctx=compile_ctx, budget=budget)
        except Exception:   # a genuine DB/planner failure for ONE recipe is a rejection, never a 500
            logger.exception("governed cross-catalog planning failed for recipe %s", rid)
            rejections.append({"lens": "governed", "reason": ReasonCode.planner_internal_error.value,
                               "recipe_id": rid})
            continue
        idea = _governed_idea_from_result(result, tmpl, target_entity)
        if idea is not None:
            ideas.append(idea)
        else:
            rejections.append({"lens": "governed", "reason": _governed_rejection_reason(result),
                               "recipe_id": rid})
    return ideas, rejections


def _reject_cross_catalog_llm(alternatives: list[FeatureSet]) -> tuple[list[FeatureSet], list[dict]]:
    """Enforce the cross-catalog invariant over the LLM alternatives: an idea whose ``derives_pairs``
    span more than one distinct catalog_source has no governed physical plan, so it is REMOVED from its
    FeatureSet and returned as a rejection (reason ``governed_cross_catalog_plan_required``). Single-
    catalog ideas are untouched — the FeatureSet keeps them in order, membership byte-identical."""
    filtered: list[FeatureSet] = []
    rejections: list[dict] = []
    for s in alternatives:
        kept: list[FeatureIdea] = []
        for f in s.features:
            if len({cs for cs, _ref in f.derives_pairs}) > 1:
                rejections.append({"name": f.name, "reason": GOVERNED_CROSS_CATALOG_PLAN_REQUIRED,
                                   "code": GOVERNED_CROSS_CATALOG_PLAN_REQUIRED})
            else:
                kept.append(f)
        filtered.append(FeatureSet(lens=s.lens, features=kept))
    return filtered, rejections


# ── Delivery C0 Task 5: the immutable metadata snapshot at considered-set time ──────────────────────
# When the considered set is built on the feature-generation connection (REPEATABLE READ, C0-T2), mint a
# generation run, snapshot the in-scope catalog state the set derives from (C0-T3), and record the
# lineage on the contract_considered row so /contract/draft + /contract/confirm reload the SERVER
# snapshot the set was authored against. Gated on the connection ACTUALLY running under REPEATABLE READ:
# a plain READ COMMITTED caller (the direct-call gate1 unit tests, any non-feature-gen path) legitimately
# takes NO snapshot — the snapshot is only meaningful/possible under the torn-free feature-gen isolation,
# and ``build_metadata_snapshot`` hard-asserts it (so this guard is what keeps those callers additive
# rather than a hard SnapshotIsolationError). The route always uses the REPEATABLE READ feature-gen conn,
# so production ALWAYS snapshots.
_REPEATABLE_READ = "repeatable read"


def _on_repeatable_read(conn) -> bool:
    """True when this connection runs under REPEATABLE READ — the feature-generation isolation the C0
    snapshot requires. ``SHOW`` reflects the level the (already-started) transaction is running at."""
    return conn.execute("SHOW transaction_isolation").fetchone()[0] == _REPEATABLE_READ


def _candidate_refs(cs: ConsideredSet) -> list[tuple[str, str]]:
    """The union of ``(catalog_source, object_ref)`` the considered set's candidates DERIVE FROM — the
    anchor plus every alternative feature's ``derives_pairs`` — deduped + sorted so the snapshot's read
    scope is deterministic. This is exactly the in-scope catalog surface the set was built against."""
    refs: set[tuple[str, str]] = set()
    if cs.anchor is not None:
        refs.update(cs.anchor.derives_pairs)
    for s in cs.alternatives:
        for f in s.features:
            refs.update(f.derives_pairs)
    return sorted(refs)


def _run_actor(intent: Intent) -> dict:
    """The generation-run manifest actor as a jsonb dict, reusing the intent's actor serialization
    (``feature_generation_run.actor`` is NOT NULL). A scalar subject is wrapped; ``None`` → ``{}``."""
    raw = _actor_json(intent.actor)
    if raw is None:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {"subject": value}


def _persist_considered_snapshot(conn, cs: ConsideredSet, intent: Intent, *,
                                 generation_run_id: str | None, roles, catalog_source: str | None,
                                 is_live: bool) -> tuple[str | None, str | None, str | None]:
    """Mint the generation run (if not supplied), build the immutable catalog snapshot (C0-T3) over the
    considered set's candidate refs, and return the ``(generation_run_id, snapshot_id, content_hash)``
    lineage to record on the contract_considered row. Runs ONLY under REPEATABLE READ (returns all-None
    otherwise). Built BEFORE the considered-set INSERT so a projection-lagged view aborts the whole
    considered set (``CatalogProjectionUnavailable`` propagates to the route → 503) with NO row written —
    the snapshot and the considered set commit atomically in the one feature transaction."""
    if not _on_repeatable_read(conn):
        return None, None, None
    run_id = generation_run_id or mint_id("fgr")
    refs = _candidate_refs(cs)
    read_scope_hash = canonical_hash({
        "refs": [list(r) for r in refs],   # already sorted, deduped
        "roles": sorted(str(r) for r in roles),
    })
    snapshot = build_metadata_snapshot(
        conn, generation_run_id=run_id, refs=refs, read_scope_hash=read_scope_hash,
        actor=_run_actor(intent),
        flags={"intake_mode": intent.intake_mode, "catalog_source": catalog_source,
               "is_live": bool(is_live)})
    return run_id, snapshot.snapshot_id, snapshot.content_hash


def build_considered_set(conn, intent: Intent, client: LLMClient, *, entity: str | None = None,
                         catalog_source: str | None = None, roles=(), target_ref: str | None = None,
                         objective: str = "", feedback: str | None = None, now=None,
                         applicability: ApplicabilityResult | None = None,
                         is_live: bool = False, target_entity: str | None = None,
                         templates: Sequence[Template] | None = None,
                         generation_run_id: str | None = None) -> ConsideredSet:
    """Discovery loop → validated alternatives; the anchor is the requester's definition run through the
    same validated loop (definition mode only). Every option shown to the human has passed the gauntlet.
    Persists the intent + target_ref (M6, BLOCKER 2) and the considered-set snapshot (BLOCKER 1) when the
    flow reaches Gate #1.

    ``applicability`` is the ONE applicability decision (computed once in the API layer, Task 7). When
    scoped grounding is enabled it narrows the template lens to the eligible recipe subset; either way it
    is carried through on the returned :class:`ConsideredSet` for the disposition stage (Task 5). The
    builder is computation-only — it NEVER persists the confirmed scope (the API layer owns that).

    ``is_live`` (3C.2a) is the ROUTE-resolved live-activation boolean — the builder NEVER reads the env
    flag. On an entity-scoped run (``catalog_source is None``) with ``is_live`` set, the governed
    cross-catalog planner runs at ``target_entity`` grain: its resolved plans become options (each idea
    carrying ``origin='governed_planner'`` / ``path_authority='governed_cross_catalog'`` and the exact
    plan envelope), its unresolved ones and every cross-catalog LLM alternative become rejections. With
    ``is_live`` false the whole governed branch is skipped — byte-identical to today. ``templates``
    (default ``ALL_TEMPLATES``) narrows the recipe registry the governed lens plans over (tests inject
    a fixture template); it never affects the single-catalog template lens.

    ``generation_run_id`` (Delivery C0 Task 5) — when the caller already minted a run (the scoped route
    reuses its generation run), the C0 metadata snapshot is anchored to it; otherwise, on a REPEATABLE
    READ feature-generation connection, a fresh ``fgr`` run is minted. Either way the snapshot lineage is
    recorded on the contract_considered row (see :func:`_persist_considered_snapshot`). On a READ
    COMMITTED connection no snapshot is taken (additive — the lineage columns stay NULL)."""
    persist_intent(conn, intent, target_ref)
    # The prediction goal enriches the generation prompt (hypothesis = the causal premise; goal = what
    # we're predicting). Redacted with the same discipline as the hypothesis before it reaches the LLM,
    # so a required-but-ignored field (bug_003) now actually shapes generation.
    redacted_goal = redact_free_text(objective, label="prediction goal")
    gen_objective = (f"{intent.redacted_hypothesis}\n\nprediction goal: {redacted_goal}"
                     if redacted_goal else intent.redacted_hypothesis)
    report = recommend_feature_sets_report(
        conn, gen_objective, client, entity=entity, catalog_source=catalog_source,
        roles=roles, target_ref=target_ref, feedback=feedback, now=now)
    alternatives = list(report.sets)
    rejections = list(report.rejections)
    grounded_template_ids: frozenset[str] = frozenset()   # per-template grounding outcome for Task 5's
    rejected_template_ids: dict[str, tuple[str, ...]] = {}   # disposition stage (empty on a no-catalog run)
    binding_quality_by_template: dict[str, str] = {}   # per-template binding signal for the ranker (A3)
    # B4 two-source model: seed the considered set with grounded parametric templates alongside the LLM
    # alternatives — but only where a single catalog is in scope to ground them (an entity-only,
    # cross-catalog run has no one source to ground on). A template that clears the SAME gauntlet joins
    # as its own "templates" lens; one that fails (e.g. it binds the intent's target_ref -> leakage) is
    # surfaced in the rejections, not silently dropped. Everything downstream treats it as one more lens.
    if catalog_source is not None:
        # Phase-1B scoped grounding: ground only the eligible recipe subset when scoping is on (else the
        # whole registry — byte-identical to today). Definition-mode + unscoped results bypass here.
        (template_ideas, template_rejections, grounded_template_ids, rejected_template_ids,
         binding_quality_by_template) = (
            _template_candidates(
                conn, catalog_source=catalog_source, roles=roles, target_ref=target_ref, now=now,
                templates=_templates_to_ground(intent, applicability)))
        if template_ideas:
            alternatives.append(FeatureSet(lens="templates", features=template_ideas))
        rejections.extend(template_rejections)
    elif is_live:
        # 3C.2a — the LIVE governed cross-catalog lens (entity-scoped: no single catalog to ground on).
        # FIRST enforce the invariant over the LLM alternatives (a cross-catalog LLM idea has no governed
        # plan → rejected), THEN append the governed planner's resolved plans as their own lens. The
        # governed ideas each carry a resolved plan envelope (a governed plan MAY be single-catalog), so
        # they are appended AFTER the filter for safety regardless — never subjected to it. Authority
        # rides on the ideas (origin/path_authority), not the lens name. This whole branch is skipped
        # when the flag is off (is_live=False) — byte-identical.
        alternatives, cross_catalog_rejections = _reject_cross_catalog_llm(alternatives)
        rejections.extend(cross_catalog_rejections)
        if target_entity is not None:   # a governed plan needs a target grain to plan toward
            eligible = (applicability.eligible_ids if applicability is not None
                        else frozenset(t.id for t in
                                       (templates if templates is not None else ALL_TEMPLATES)))
            governed_ideas, governed_rejections = _governed_cross_catalog_options(
                conn, target_entity=target_entity, eligible_recipe_ids=eligible, roles=roles,
                now=now, templates=templates)
            if governed_ideas:
                alternatives.append(FeatureSet(lens="templates", features=governed_ideas))
            rejections.extend(governed_rejections)
    anchor: FeatureIdea | None = None
    if intent.intake_mode == "definition":
        ideas = recommend_features(
            conn, intent.redacted_definition, client, entity=entity, catalog_source=catalog_source,
            roles=roles, target_ref=target_ref, now=now, target=1)
        # H1a: the definition anchor is the USER's own definition run through the validated loop — the
        # server-assigned generation_source for the user-anchor path is "user_defined" (distinct from the
        # LLM alternatives' "llm_freeform" and the recipe lens's "recipe"). Never read from LLM output.
        anchor = replace(ideas[0], generation_source="user_defined") if ideas else None
        # 3C.2a fail-closed: on a live entity-scoped run (catalog_source is None) the definition anchor is
        # generated over the WHOLE cross-catalog candidate pool, so it CAN span >1 catalog with NO
        # governed physical plan. Mirror the alternatives filter: drop such an anchor (it must never be
        # customer-visible / choosable at Gate #1) and surface it as the same rejection. A single-catalog
        # anchor is untouched. (Routing the definition through the governed planner is 3C.2b, not here.)
        if is_live and anchor is not None and len({cs for cs, _ref in anchor.derives_pairs}) > 1:
            rejections.append({"name": anchor.name, "reason": GOVERNED_CROSS_CATALOG_PLAN_REQUIRED,
                               "code": GOVERNED_CROSS_CATALOG_PLAN_REQUIRED})
            anchor = None
    recommendation = (recommend_set(conn, alternatives, intent.redacted_hypothesis, client)
                      if any(s.features for s in alternatives) else None)
    cs = ConsideredSet(intent.intent_id, anchor, alternatives, recommendation, rejections,
                       applicability=applicability,
                       grounded_template_ids=grounded_template_ids,
                       rejected_template_ids=rejected_template_ids,
                       binding_quality_by_template=binding_quality_by_template)
    # Delivery C0 Task 5: build the immutable catalog snapshot the set was authored against BEFORE the
    # considered-set INSERT — a projection-lagged view raises here (→ route 503) with NO considered-set
    # row written, and the snapshot + considered set commit atomically in the one feature transaction.
    snap_run_id, snap_id, snap_hash = _persist_considered_snapshot(
        conn, cs, intent, generation_run_id=generation_run_id, roles=roles,
        catalog_source=catalog_source, is_live=is_live)
    conn.execute(   # persist the validated set so /contract/draft reconstructs the chosen feature here
        "INSERT INTO contract_considered "
        "(intent_id, considered, generation_run_id, snapshot_id, snapshot_content_hash) "
        "VALUES (%s, %s::jsonb, %s, %s, %s) "
        "ON CONFLICT (intent_id) DO UPDATE SET considered = EXCLUDED.considered, "
        "generation_run_id = EXCLUDED.generation_run_id, snapshot_id = EXCLUDED.snapshot_id, "
        "snapshot_content_hash = EXCLUDED.snapshot_content_hash",
        (intent.intent_id, json.dumps(_snapshot(conn, cs)), snap_run_id, snap_id, snap_hash))
    return cs


def _alternative_ids(cs: ConsideredSet) -> set[str]:
    return {f.name for s in cs.alternatives for f in s.features}


def _option_ids(cs: ConsideredSet) -> set[str]:
    ids = _alternative_ids(cs)
    if cs.anchor is not None:
        ids.add(cs.anchor.name)
    return ids


def _idea_json(f: FeatureIdea | None) -> dict | None:
    if f is None:
        return None
    d = {"name": f.name, "derives_from": f.derives_from, "aggregation": f.aggregation,
         "grain_table": f.grain_table,   # keep grain — it disambiguates same-named options
         "verification": f.verification,   # honest §14.5 stamp surfaced at Gate #1 (item 4)
         "critic_note": f.critic_note,     # advisory residual critic note — the human weighs it
         "rationale": f.rationale,         # §14.2 one-line causal 'why' — audit the logic first
         "validation_status": f.validation_status,   # 3A-ii honest tri-state (NEW axis)
         "requirements": requirements_to_json(f.requirements),
         "derives_pairs": [list(p) for p in f.derives_pairs],   # for server-side reconstruction
         # 3C.2a carry-forward: provenance + the governed plan envelope (null for LLM/single-catalog
         # options), persisted with the considered set so drafting reconstructs the EXACT plan.
         "origin": f.origin, "path_authority": f.path_authority,
         "plan_envelope": f.plan_envelope.to_json() if f.plan_envelope else None}
    # H1a carry-through: emitted ONLY when non-default so a pre-H1a idea's persisted bytes are
    # byte-identical (mirrors the C2-C3 requirement-`params` and 3C.2a plan-envelope only-when-present
    # strategy). recipe_id MUST round-trip here — it is what survives the Gate-1 considered-set reload.
    if f.generation_source != "llm_freeform":
        d["generation_source"] = f.generation_source
    if f.recipe_id is not None:
        d["recipe_id"] = f.recipe_id
    if f.candidate_status:
        d["candidate_status"] = f.candidate_status
    if f.input_role_bindings:
        d["input_role_bindings"] = [b.to_json() for b in f.input_role_bindings]
    if f.external_requirement_previews:
        d["external_requirement_previews"] = [p.to_json() for p in f.external_requirement_previews]
    if f.metadata_snapshot_id is not None:
        d["metadata_snapshot_id"] = f.metadata_snapshot_id
    if f.metadata_input_fingerprint is not None:
        d["metadata_input_fingerprint"] = f.metadata_input_fingerprint
    if f.binding_fact_keys:
        d["binding_fact_keys"] = list(f.binding_fact_keys)
    if f.planner_applicability != "not_applicable_nonrecipe":
        d["planner_applicability"] = f.planner_applicability
    if f.physical_plan_id is not None:
        d["physical_plan_id"] = f.physical_plan_id
    if f.planner_declaration_id is not None:
        d["planner_declaration_id"] = f.planner_declaration_id
    return d


def _snapshot(conn, cs: ConsideredSet) -> dict:
    return {
        "anchor": _idea_json(cs.anchor),
        "alternatives": [{"lens": s.lens, "features": [_idea_json(f) for f in s.features],
                          "signals": set_signals(conn, s)}   # deterministic ranking signals (item 1b)
                         for s in cs.alternatives],
        "recommendation": None if cs.recommendation is None else {
            "recommended_lens": cs.recommendation.recommended_lens,
            "reasoning": cs.recommendation.reasoning, "caveat": cs.recommendation.caveat},
    }


def confirm_gate1(conn, considered: ConsideredSet, *, chosen_source: str, chosen_option_id: str,
                  actor, why: str = "") -> str:
    """Record the human's validated choice (who + why + the full considered set). Rejects a choice not
    in the set, or an 'anchor' source that isn't the anchor. Returns the chosen feature id."""
    if chosen_source not in ("anchor", "alternative"):
        raise Gate1Error(f"chosen_source must be 'anchor' or 'alternative', got {chosen_source!r}")
    if chosen_option_id not in _option_ids(considered):
        raise Gate1Error(f"chosen_option_id {chosen_option_id!r} is not in the considered set")
    if chosen_source == "anchor" and (
            considered.anchor is None or considered.anchor.name != chosen_option_id):
        raise Gate1Error("chosen_source 'anchor' but the chosen option is not the anchor")
    if chosen_source == "alternative" and chosen_option_id not in _alternative_ids(considered):
        raise Gate1Error("chosen_source 'alternative' but the chosen option is not an alternative")
    conn.execute(
        "INSERT INTO contract_gate1_choice "
        "(intent_id, chosen_source, chosen_option_id, why, actor, considered) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb) "
        "ON CONFLICT (intent_id) DO UPDATE SET chosen_source = EXCLUDED.chosen_source, "
        "chosen_option_id = EXCLUDED.chosen_option_id, why = EXCLUDED.why, actor = EXCLUDED.actor, "
        "considered = EXCLUDED.considered",
        (considered.intent_id, chosen_source, chosen_option_id, why,
         _actor_json(actor), json.dumps(_snapshot(conn, considered))))
    return chosen_option_id


def _idea_from_json(d: dict) -> FeatureIdea:
    return FeatureIdea(
        name=d["name"], description="", derives_from=list(d.get("derives_from", [])),
        aggregation=d.get("aggregation"), grain_table=d.get("grain_table"),
        derives_pairs=tuple(tuple(p) for p in d.get("derives_pairs", [])),
        verification=d.get("verification", "DESIGN-CHECKED"),      # was dropped pre-3A-ii
        critic_note=d.get("critic_note", ""),                      # was dropped pre-3A-ii
        rationale=d.get("rationale", ""),                          # was dropped pre-3A-ii
        validation_status=d.get("validation_status", "DESIGN_CHECKED"),   # 3A-ii honest state
        requirements=requirements_from_json(d.get("requirements", [])),
        # 3C.2a: absent keys (pre-3C snapshots) deserialize to the defaults — behaviour-neutral.
        origin=d.get("origin", "llm"), path_authority=d.get("path_authority", "single_or_llm"),
        plan_envelope=PlanEnvelopeV1.from_json(d["plan_envelope"]) if d.get("plan_envelope") else None,
        # H1a: absent keys (pre-H1a snapshots) deserialize to the defaults — behaviour-neutral. recipe_id
        # is restored here so a recipe-sourced idea keeps its registry id across the Gate-1 round-trip.
        generation_source=d.get("generation_source", "llm_freeform"),
        recipe_id=d.get("recipe_id"),
        candidate_status=d.get("candidate_status", ""),
        input_role_bindings=tuple(RoleBinding.from_json(b) for b in d.get("input_role_bindings", ())),
        external_requirement_previews=tuple(
            ExternalRequirementPreview.from_json(p)
            for p in d.get("external_requirement_previews", ())),
        metadata_snapshot_id=d.get("metadata_snapshot_id"),
        metadata_input_fingerprint=d.get("metadata_input_fingerprint"),
        binding_fact_keys=tuple(str(k) for k in d.get("binding_fact_keys", ())),
        planner_applicability=d.get("planner_applicability", "not_applicable_nonrecipe"),
        physical_plan_id=d.get("physical_plan_id"),
        planner_declaration_id=d.get("planner_declaration_id"))


def chosen_feature(conn, intent_id: str, chosen_source: str,
                   chosen_option_id: str) -> FeatureIdea | None:
    """Reconstruct the human's chosen feature from the SERVER-persisted considered set (BLOCKER 1) — the
    draft is authored from HERE, never from a client-supplied feature. Returns None if the choice isn't
    in the recorded set (so a fabricated / not-offered feature can't be drafted)."""
    row = conn.execute("SELECT considered FROM contract_considered WHERE intent_id = %s",
                       (intent_id,)).fetchone()
    if row is None:
        return None
    snap = row[0]
    if chosen_source == "anchor":
        a = snap.get("anchor")
        return _idea_from_json(a) if a and a.get("name") == chosen_option_id else None
    # Collect EVERY alternative matching the name. If two lenses emitted the same name with different
    # structure (derives/aggregation), the choice is genuinely AMBIGUOUS — reconstructing the "first"
    # would govern a feature the human may not have picked, so fail closed (caller -> 422).
    matches = [f for s in snap.get("alternatives", []) for f in s.get("features", [])
               if f.get("name") == chosen_option_id]
    if not matches:
        return None
    first = matches[0]
    key = (first.get("aggregation"), [tuple(p) for p in first.get("derives_pairs", [])])
    if any((m.get("aggregation"), [tuple(p) for p in m.get("derives_pairs", [])]) != key
           for m in matches[1:]):
        return None   # ambiguous same-name options — cannot safely reconstruct
    return _idea_from_json(first)


def considered_snapshot_lineage(conn, intent_id: str) -> dict | None:
    """The SERVER-persisted C0 metadata-snapshot lineage recorded on the considered set for this intent
    (Delivery C0 Task 5): the ``generation_run_id`` + immutable ``snapshot_id`` / ``content_hash`` the
    set was authored against. /contract/draft + /contract/confirm reload THIS server value — a client
    never supplies a snapshot id (the draft/confirm request models carry none, so there is nothing to
    trust). Returns None when no snapshot was recorded (a READ COMMITTED / pre-C0 considered set)."""
    row = conn.execute(
        "SELECT generation_run_id, snapshot_id, snapshot_content_hash "
        "FROM contract_considered WHERE intent_id = %s", (intent_id,)).fetchone()
    if row is None or row[1] is None:
        return None
    return {"generation_run_id": row[0], "snapshot_id": row[1], "content_hash": row[2]}


def record_gate1_choice(conn, intent_id: str, *, chosen_source: str, chosen_option_id: str,
                        actor, why: str = "") -> None:
    """Record the human's Gate #1 choice (audit) against the persisted considered set."""
    row = conn.execute("SELECT considered FROM contract_considered WHERE intent_id = %s",
                       (intent_id,)).fetchone()
    conn.execute(
        "INSERT INTO contract_gate1_choice (intent_id, chosen_source, chosen_option_id, why, actor, "
        "considered) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb) "
        "ON CONFLICT (intent_id) DO UPDATE SET chosen_source = EXCLUDED.chosen_source, "
        "chosen_option_id = EXCLUDED.chosen_option_id, why = EXCLUDED.why, actor = EXCLUDED.actor",
        (intent_id, chosen_source, chosen_option_id, why, _actor_json(actor),
         json.dumps(row[0] if row else {})))


def gate1_choice(conn, intent_id: str) -> dict | None:
    """The human's RECORDED Gate #1 choice for an intent, or None if none was recorded. Used by
    /contract/confirm to prove a governed feature was actually chosen from the considered set."""
    row = conn.execute(
        "SELECT chosen_source, chosen_option_id FROM contract_gate1_choice WHERE intent_id = %s",
        (intent_id,)).fetchone()
    return {"chosen_source": row[0], "chosen_option_id": row[1]} if row else None

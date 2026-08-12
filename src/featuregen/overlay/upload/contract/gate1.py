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
from featuregen.overlay.upload.contract.intake_ticket import signed_reading_for
from featuregen.overlay.upload.contract.near_label_critic import annotate_near_label
from featuregen.overlay.upload.contract.param_choice import choose_params
from featuregen.overlay.upload.generation_semantic_context import (
    build_generation_semantic_context,
)
from featuregen.overlay.upload.contract.scope_mode import confirmation_required
from featuregen.overlay.upload.enrich_batch import CallLedger
from featuregen.overlay.upload.feature_assist import (
    ExternalRequirementPreview,
    FeatureIdea,
    FeatureSet,
    Rejection,
    RoleBinding,
    SetRecommendation,
    _candidate_columns,
    _validate_idea,
    recommend_feature_sets_report,
    recommend_features,
    recommend_set,
    set_signals,
)
from featuregen.overlay.upload.feature_metadata_snapshot import (
    build_metadata_snapshot,
    capture_column_snapshot,
    ensure_generation_run,
)
from featuregen.overlay.upload.grounding_trace import (
    GROUNDING_CANDIDATE_SET,
    READ_SCOPE,
    SuggestionDependencyClass,
    build_trace,
    dependency_pin,
)
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
    plan_dependency_pins,
    plan_envelope_from_result,
    plan_operand_roles,
    plan_relationship_dependencies,
)
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.planner.shadow import COMPILE_BUDGET, MAX_COMPILES_PER_RUN
from featuregen.overlay.upload.read_scope import allowed_classes, read_scope_rule_content_hash
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    build_recipe_grounding_context,
)
from featuregen.overlay.upload.taxonomy.applicability import (
    ApplicabilityResult,
    ConfirmedScope,
)
from featuregen.overlay.upload.taxonomy.ranking_signals import binding_quality
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    GroundedFeature,
    GroundingStatus,
    Template,
)
from featuregen.overlay.upload.templates import (
    ground_all_outcomes as _ground_all_default,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

# Preserve both historical test/integration seams while callers migrate to the explicit name.
ground_all_outcomes = _ground_all_default
ground_all = _ground_all_default


def _ground_template_outcomes(*args, **kwargs):
    if ground_all_outcomes is not _ground_all_default:
        return ground_all_outcomes(*args, **kwargs)
    return ground_all(*args, **kwargs)


# 3C.2a — the fail-closed cross-catalog invariant. On a live entity-scoped run EVERY customer-visible
# cross-catalog feature must have a governed physical plan, so an LLM alternative whose derives span
# more than one catalog (which has NO such plan) can never be a recommendation — it is surfaced as a
# rejection carrying this reason string instead.
GOVERNED_CROSS_CATALOG_PLAN_REQUIRED = "governed_cross_catalog_plan_required"
CONSIDERED_CANONICALIZATION_VERSION = "contract-considered-v2"


class Gate1Error(Exception):
    """A malformed or out-of-set Gate #1 confirmation."""


class UnknownConsideredOption(Gate1Error):
    """The caller named an option id that this verified revision does not contain.

    Deliberately a SEPARATE type from its parent. Every other ``Gate1Error`` out of the revision
    readers means the STORED record failed verification — a hash mismatch, an inconsistent option
    map, a broken lineage — i.e. corruption or tampering, which is an operator-grade event. This one
    means only that the client sent an option id the server does not recognise, which is what a
    stale browser tab does after a regenerate. Collapsing the two makes a routine client retry
    indistinguishable from an integrity failure in the logs, so the route maps this to 422 and the
    rest to 409.
    """


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
    incomplete_template_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    binding_quality_by_template: dict[str, str] = field(default_factory=dict)   # template id ->
    #   BindingQuality.value for each SURVIVING grounded candidate (Task A3 Part A) — the ranker's
    #   binding-quality signal. Additive + read-only: grounding behaviour is unchanged and nothing else
    #   reads it (the ranker consumes it in the API layer only when FEATUREGEN_INTENT_RANKING is on).
    recipe_grounding_context_by_candidate_key: dict[
        str, RecipeGroundingContextV1
    ] = field(default_factory=dict)
    recipe_candidate_keys_by_recipe_id: dict[
        str, tuple[str, ...]
    ] = field(default_factory=dict)
    option_ids_by_path: dict[str, str] = field(default_factory=dict)
    considered_revision_id: str | None = None
    considered_content_hash: str | None = None


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
# Task 3: the near-label critic's per-build spend ceiling. Verdicts are content-addressed, so a
# steady-state build replays for free; the ceiling only bites on a cold, unusually wide set — where
# the overflow candidates abstain honestly rather than dispatch unboundedly.
_NEAR_LABEL_MAX_CALLS = 24


# Pre-live simplification (2026-08-11): Task 4b's hypothesis-chosen parameters run whenever a
# client is present — the FEATUREGEN_PARAM_CHOICE flag is retired (fail-soft by construction:
# any dispatch failure falls back to the authored defaults).


def _param_alternatives_line(template: Template, bound: dict) -> str:
    """The card's "also available" line (Task 4b emission policy): each multi-value param with its
    chosen value marked. "" for a recipe with nothing to choose — the field stays absent."""
    parts = []
    for key, allowed in sorted(template.params.items()):
        if len(allowed) <= 1:
            continue
        rendered = "/".join(f"[{v}]" if v == bound.get(key) else str(v) for v in allowed)
        parts.append(f"{key}: {rendered}")
    return "; ".join(parts)


def _operand_roles(gf: GroundedFeature) -> tuple[tuple[str, str], ...]:
    """The (object_ref, role) pairs the TEMPLATE declared for this candidate's bound operands.

    Read verbatim off ``binding_resolutions``, which grounding already built as
    ``resolutions[need.role] = GroundedNeedResolution(role=need.role, selected_object_ref=col…)`` —
    so the role is the recipe author's hand-written ``Need.role`` and nothing else. A resolution with
    no ``selected_object_ref`` (missing / ambiguous / truncated need) labels no operand, so it is
    SKIPPED rather than guessed at. Sorted + deduped for determinism.
    """
    return tuple(sorted({
        (resolution.selected_object_ref, resolution.role)
        for resolution in gf.binding_resolutions
        if resolution.selected_object_ref is not None}))


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
        critic_note="", rationale=rationale,
        # `derives_pairs` is bare (catalog_source, object_ref) — the role the template DECLARED for
        # each operand was dropped here. Carry it; nothing reads it yet.
        operand_roles=_operand_roles(gf))


@dataclass(frozen=True, slots=True)
class RejectionRecordV1:
    """One refused grounded candidate, WITH the template that produced it (Task 2A, freeze 0F-7 P3).

    The V1 wire rejection is a ``{name, reason, code}`` dict and carries no template id, so a
    consumer that wanted one had to match on the rendered NAME — the re-attribution guesswork
    per-table grounding removed. This record carries the id the engine already knew, and the typed
    `Rejection` (with its own decision trace) rather than a lossy projection of it.
    """

    template_id: str
    candidate_name: str
    rejection: Rejection


@dataclass(frozen=True, slots=True)
class TemplateCandidatesResult:
    """What one grounding pass produced — the SAME eight objects the 8-tuple carried, under their
    established names, plus the rejection records (Task 2A, freeze 0F-7 P3).

    This replaced a positionally-unpacked 8-tuple, deliberately and BREAKINGLY: a ninth member
    would have been the fourth silent widening of a shape whose callers all index it positionally,
    and the trace's whole point is that a later consumer can name what it reads. The surviving
    candidates' traces ride INSIDE ``ideas`` (``FeatureIdea.grounding_trace``); the refused ones
    ride on ``rejection_records[].rejection.trace``.
    """

    ideas: list[FeatureIdea]
    rejections: list[dict]                          # the V1 wire shape, byte-identical
    grounded_ids: frozenset[str]
    rejected_ids: dict[str, tuple[str, ...]]
    binding_by_id: dict[str, str]
    incomplete_ids: dict[str, tuple[str, ...]]
    contexts: dict[str, RecipeGroundingContextV1]
    keys_by_recipe: dict[str, tuple[str, ...]]
    rejection_records: tuple[RejectionRecordV1, ...] = ()


def _template_candidates(conn, *, catalog_source: str, roles, target_ref: str | None, now,
                         templates: Sequence[Template] = ALL_TEMPLATES,
                         fresh_within: timedelta = timedelta(hours=24),
                         table: str | None = None,
                         also_tables: Sequence[str] = (),
                         params_by_id: dict | None = None,
                         ) -> TemplateCandidatesResult:
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
    the ranker consumes; grounding behaviour is unchanged by computing it.

    ``table`` narrows GROUNDING to one table's columns (never the gauntlet's candidate universe, which
    stays catalog-wide so a cross-catalog/cross-table judgement is unchanged). It is OPT-IN and used by
    the per-table suggestions screen ONLY: this pass yields at most one candidate per template, so
    catalog-wide the first table to bind a recipe uses it up and every other table shows nothing for it.
    The feature-generation flow (``build_considered_set``) asks the CATALOG-wide question and must keep
    asking it, so the default is ``None`` — unchanged, one candidate per template, whole catalog.

    ``also_tables`` widens that narrowing to the sibling tables a CLEARING join reaches from ``table``
    (the suggestions screen resolves a BOUNDED set of them via ``join_path.clearing_neighbourhood``),
    so a cross-table candidate a governed join authorises can ground. It is INERT when ``table is None``:
    the catalog-wide pass already considers every table and must stay byte-identical."""
    # The kwargs are passed ONLY when narrowing: `_ground_template_outcomes` is a long-standing
    # substitution seam (tests and the measurement harness replace it), so the catalog-wide call must
    # stay argument-for-argument what it has always been — and an un-widened per-table call must stay
    # argument-for-argument what IT has been since P4.
    narrowing: dict = {}
    if table is not None:
        narrowing["table"] = table
        if also_tables:
            narrowing["also_tables"] = tuple(also_tables)
    # Task 4b: hypothesis-chosen parameter overrides, passed ONLY when present for the same
    # seam-stability reason — the flag-off / abstain call stays argument-for-argument identical.
    if params_by_id:
        narrowing["params_by_id"] = params_by_id
    outcomes = _ground_template_outcomes(
        conn, templates, catalog_source=catalog_source, roles=roles, **narrowing)
    grounded = [
        outcome.feature for outcome in outcomes if outcome.feature is not None]
    incomplete_ids = {
        outcome.template_id: outcome.reason_codes
        for outcome in outcomes
        if outcome.status is GroundingStatus.BUDGET_TRUNCATED
    }
    if not grounded:
        return TemplateCandidatesResult(
            ideas=[], rejections=[], grounded_ids=frozenset(), rejected_ids={}, binding_by_id={},
            incomplete_ids=incomplete_ids, contexts={}, keys_by_recipe={})
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
    contexts: dict[str, RecipeGroundingContextV1] = {}
    keys_by_recipe: dict[str, list[str]] = {}
    rejection_records: list[RejectionRecordV1] = []
    for gf in grounded:
        idea = _idea_from_grounded(gf, by_id[gf.template_id])
        raw = {"name": idea.name, "description": idea.description,
               "derives_from": list(idea.derives_from), "aggregation": idea.aggregation,
               "grain_table": idea.grain_table, "rationale": idea.rationale}
        # Task 2A: the candidate's own identity, built BEFORE the gauntlet so it can be threaded in
        # and the trace can be minted AT the decision points rather than stitched on afterwards.
        # `build_recipe_grounding_context` is pure (template + grounded feature, no DB) and the
        # object is reused verbatim below, so this moves no work and adds no read — it only makes
        # the key available to a REFUSED candidate too, which is precisely the case V2 must explain.
        context = build_recipe_grounding_context(by_id[gf.template_id], gf)
        # The TEMPLATE-DECLARED operand roles ride into the gauntlet alongside `raw` (which is bare
        # refs): they narrow which operands the unit/currency needs-check may ask about. Passed as a
        # kwarg rather than folded into `raw` so the LLM's raw shape — and therefore the LLM path —
        # is untouched.
        validated, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within,
                                        roles=roles, operand_roles=idea.operand_roles,
                                        candidate_key=context.recipe_candidate_key,
                                        template_id=gf.template_id)
        if rej is None:
            # [F9] keep the VALIDATOR's idea (carries status + requirements), then SERVER-STAMP the H1a
            # recipe provenance: generation_source + recipe_id come from the grounded TEMPLATE id (the
            # server's own knowledge of the recipe path), never from the LLM/candidate raw. recipe_id
            # then survives the Gate-1 considered-set round-trip (persist → reload) via the (de)serializers.
            # `operand_roles` rides along in the SAME server-stamp: the validator builds its own idea
            # from `raw` (which is bare refs), so without this the template-declared roles would be
            # dropped again one line after being carried. Additive — the validator's status +
            # requirements are untouched.
            # `replace` preserves `grounding_trace` by construction, so the server stamp cannot
            # drop the trace the validator just minted.
            # Task 4b's "also available" line — unconditional since the flag retired (pre-live).
            alternatives_line = _param_alternatives_line(by_id[gf.template_id], gf.params)
            ideas.append(replace(validated, generation_source="recipe", recipe_id=gf.template_id,
                                 planner_applicability="not_applicable_single_catalog",
                                 operand_roles=idea.operand_roles,
                                 param_alternatives=alternatives_line))
            grounded_ids.add(gf.template_id)
            binding_by_id[gf.template_id] = binding_quality(gf).value   # ranker's binding signal
            contexts[context.recipe_candidate_key] = context
            keys_by_recipe.setdefault(gf.template_id, []).append(context.recipe_candidate_key)
        else:
            rejections.append({"name": idea.name, "reason": rej.message, "code": rej.code})
            rejected_ids[gf.template_id] = (rej.code,)
            # The SAME rejection, carrying its template id and its trace — the V1 dict above stays
            # exactly the three keys the wire has always had.
            rejection_records.append(RejectionRecordV1(gf.template_id, idea.name, rej))
    return TemplateCandidatesResult(
        ideas=ideas,
        rejections=rejections,
        grounded_ids=frozenset(grounded_ids),
        rejected_ids=rejected_ids,
        binding_by_id=binding_by_id,
        incomplete_ids=incomplete_ids,
        contexts=contexts,
        keys_by_recipe={recipe_id: tuple(keys) for recipe_id, keys in keys_by_recipe.items()},
        rejection_records=tuple(rejection_records),
    )


# ── Delivery 0: server-enforced scoped grounding ────────────────────────────────────────────────────
# Normal release mode always narrows to the supplied applicability. The old applicability flag is read
# only in the explicit legacy_unscoped emergency mode. The narrowing never widens and never relaxes
# grounding safety.
def order_ideas_by_use_case(ideas: list[FeatureIdea],
                            domains: tuple[str, ...]) -> list[FeatureIdea]:
    """Task 4's whole ranking step — deterministic set intersection, NO model. Stable descending
    sort on |template.use_cases ∪ {family} ∩ signed business_domain|: equal-overlap ideas keep
    today's registry order EXACTLY, so an unmappable hypothesis (no signed domains, or nothing
    overlaps) provably falls back to today's order. ORDERS, never removes — THE RULE: the
    hypothesis may remove a recipe only for being unsafe, never for being irrelevant. Shadow
    counters always fire; the reordered list is returned only under the flag."""
    domain_set = frozenset(domains)
    by_id = {t.id: t for t in ALL_TEMPLATES}

    def _overlap(idea: FeatureIdea) -> int:
        template = by_id.get(idea.recipe_id) if idea.recipe_id else None
        if template is None:
            return 0
        return len((set(template.use_cases) | {template.family}) & domain_set)

    if not domain_set:
        counters.incr("overlay.use_case_order.unmappable")
        return ideas
    ordered = sorted(ideas, key=lambda i: -_overlap(i))   # stable: ties keep registry order
    if all(_overlap(i) == 0 for i in ideas):
        counters.incr("overlay.use_case_order.unmappable")
        return ideas
    changed = [i.name for i in ordered] != [i.name for i in ideas]
    counters.incr(f"overlay.use_case_order.{'changed' if changed else 'unchanged'}")
    # Pre-live simplification (2026-08-11): the ordering APPLIES unconditionally — the
    # FEATUREGEN_USE_CASE_ORDERING flag is retired. The unmappable fallback above already
    # guarantees a hypothesis with no signed domains keeps the registry order exactly.
    return ordered


def _intent_scoped_applicability_enabled() -> bool:
    """Release mode always enforces scoped applicability.

    The historical flag remains only for the explicitly selected ``legacy_unscoped`` emergency mode;
    it can no longer disable a confirmed scope during normal operation.
    """
    if confirmation_required():
        return True
    return os.environ.get("FEATUREGEN_INTENT_SCOPED_APPLICABILITY", "0") == "1"


def _templates_to_ground(intent: Intent,
                         applicability: ApplicabilityResult | None) -> Sequence[Template]:
    """Return the governed recipe universe for this run.

    Release mode applies a confirmed narrowing in both intake modes. Legacy mode preserves the
    historical definition bypass and old flag semantics for emergency rollback.
    """
    if (_intent_scoped_applicability_enabled()
            and applicability is not None
            and (confirmation_required() or intent.intake_mode != "definition")
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


def _governed_plan_trace(plan: BindingPlanV1, *, roles, pairs: tuple[tuple[str, str], ...],
                         validation_status: str):
    """The cross-catalog candidate's decision trace (Task 2A).

    The governed planner IS this candidate's decision, so its trace is minted here, from the plan
    the compiler already produced: the ordered crossings it selected (never re-planned), one pin per
    crossing carrying the exact realization revision, the contract resolution that admitted it, the
    read scope the compile ran under and the read set it resolved. No gauntlet rule runs on this
    path, so ``validation_rule_content_hashes`` is honestly empty — this candidate's authority is
    the governed contract, not the tri-state gauntlet.
    """
    pins = [
        dependency_pin(
            dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY,
            dependency_kind=READ_SCOPE, dependency_key="read-scope",
            content={"allowed_classes": allowed_classes(roles)}),
        dependency_pin(
            dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY,
            dependency_kind=GROUNDING_CANDIDATE_SET,
            dependency_key="physical_read_set",
            content={"resolved_object_refs": [ref for _cs, ref in pairs]}),
        *plan_dependency_pins(plan),
    ]
    return build_trace(
        candidate_key=plan.physical_plan_id,
        ordered_operand_roles=plan_operand_roles(plan),
        ordered_relationship_path=plan_relationship_dependencies(plan),
        validation_status=validation_status, requirements=(), dependency_pins=pins,
        validation_rule_content_hashes=(),
        read_scope_rule_content_hashes=(read_scope_rule_content_hash(),))


def _governed_idea_from_result(result: BindingPlanningResultV1, template: Template,
                               target_entity: str, *, roles=()) -> FeatureIdea | None:
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
        planner_applicability="applicable_cross_catalog", physical_plan_id=envelope.physical_plan_id,
        # The governed path's own decision trace — the crossings the compiler SELECTED, retained so
        # nothing downstream has to re-plan to explain this option (freeze 0F-7 / rule 15).
        grounding_trace=_governed_plan_trace(plan, roles=roles, pairs=pairs,
                                             validation_status="DESIGN_CHECKED"))


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
    # Delivery H3b: on the REPEATABLE READ feature-generation connection (C0-T2) the planner's candidate
    # discovery reads columns from the C0 immutable snapshot — a frozen ``_load_columns`` capture over the
    # SAME torn-free graph_node view the C0 metadata snapshot seals — never a fresh live read. Byte-
    # identical to live ``_load_columns`` for the frozen state, so physical_plan_id is unchanged. A READ
    # COMMITTED caller (direct gate1 unit tests) takes NO snapshot and keeps the live read (additive).
    column_source = (capture_column_snapshot(conn, scope.authorized_catalog_sources, roles)
                     if _on_repeatable_read(conn) else None)
    compile_ctx = build_compiler_context(conn, scope, roles, now, column_source=column_source)
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
        idea = _governed_idea_from_result(result, tmpl, target_entity, roles=roles)
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
                                 is_live: bool,
                                 semantic_context=None) -> tuple[str | None, str | None, str | None]:
    """Mint the generation run (if not supplied), build the immutable catalog snapshot (C0-T3) over the
    considered set's candidate refs, and return the ``(generation_run_id, snapshot_id, content_hash)``
    lineage to record on the contract_considered row. Runs ONLY under REPEATABLE READ (returns all-None
    otherwise). Built BEFORE the considered-set INSERT so a projection-lagged view aborts the whole
    considered set (``CatalogProjectionUnavailable`` propagates to the route → 503) with NO row written —
    the snapshot and the considered set commit atomically in the one feature transaction."""
    if not _on_repeatable_read(conn):
        # Compatibility/unit-test callers may not use the production REPEATABLE READ connection.
        # Preserve an explicitly supplied scoped run id so the immutable considered revision and
        # exact-choice token still exist; only the catalog snapshot lineage is unavailable.
        return generation_run_id, None, None
    run_id = generation_run_id or mint_id("fgr")
    # Bind a directly-built run to its intent before the snapshot helper's compatibility ensure call.
    # The production route already creates this binding; keeping it here makes the builder's immutable
    # revision and exact-choice lineage self-consistent for every caller.
    ensure_generation_run(
        conn,
        run_id,
        _run_actor(intent),
        {"intake_mode": intent.intake_mode, "catalog_source": catalog_source,
         "is_live": bool(is_live)},
        intent_id=intent.intent_id,
    )
    refs = _candidate_refs(cs)
    read_scope_hash = canonical_hash({
        "refs": [list(r) for r in refs],   # already sorted, deduped
        "roles": sorted(str(r) for r in roles),
    })
    # SE-2: the frozen Layer-A context's identity pin joins the sealed item set — the run can
    # prove which semantic state it consumed, and the freshness check can detect drift from it.
    extra_items = ()
    if semantic_context is not None:
        from featuregen.overlay.upload.generation_semantic_context import context_snapshot_item

        extra_items = (context_snapshot_item(semantic_context),)
    snapshot = build_metadata_snapshot(
        conn, generation_run_id=run_id, refs=refs, read_scope_hash=read_scope_hash,
        actor=_run_actor(intent),
        flags={"intake_mode": intent.intake_mode, "catalog_source": catalog_source,
               "is_live": bool(is_live)},
        extra_items=extra_items)
    return run_id, snapshot.snapshot_id, snapshot.content_hash


def _semantic_shadow_compare(conn, *, catalog_source: str, roles, scope: ConfirmedScope,
                             grounded_ids: frozenset[str],
                             rejected_ids: dict[str, tuple[str, ...]],
                             semantic_context=None,
                             generation_run_id: str | None = None) -> None:
    """SE-7 part 2 — the shadow half of the semantic-planning rollout: the V2 lens runs beside
    the legacy template lens and the divergence is LOGGED, never served. Fail-soft under a
    savepoint: a shadow failure must not poison the user's request transaction (the same rule
    the shadow planner obeys), and the response is byte-identical either way."""
    from collections import Counter

    from featuregen.overlay.upload.recipe_planning_lens import v2_recipe_candidates

    context_hash = semantic_context.context_hash() if semantic_context is not None else ""
    try:
        with conn.transaction():                      # savepoint — shadow reads stay isolated
            candidates = v2_recipe_candidates(
                conn, catalog_source=catalog_source, roles=roles, scope=scope,
                context=semantic_context)
            # SE-10 slice 1: the observations become ROWS (append-only, migration 1062) —
            # fleet metrics query them; the savepoint still shields the user's request.
            if semantic_context is not None:
                from featuregen.overlay.upload.semantic_candidate_store import (
                    persist_semantic_candidates,
                )

                persist_semantic_candidates(
                    conn, generation_run_id=generation_run_id or "unattributed",
                    context=semantic_context, candidates=candidates)
        by_state = Counter(candidate.binding_state for candidate in candidates)
        logger.info(
            "semantic-shadow: eligible=%d bound=%d ambiguous=%d missing=%d blocked=%d "
            "review_current=%d temporal_blocked=%d | legacy grounded=%d rejected=%d "
            "context=%s",
            len(candidates), by_state.get("bound", 0), by_state.get("ambiguous", 0),
            by_state.get("missing", 0), by_state.get("blocked", 0),
            sum(1 for c in candidates if c.review_current),
            sum(1 for c in candidates if c.temporal_blocker),
            len(grounded_ids), len(rejected_ids), context_hash[:16] or "unassembled")
        # SE-10 slice 2: assembly runs in shadow too — merge + designed order observed on real
        # catalogs before anything serves it. Pure fold over the candidates already in hand.
        from featuregen.overlay.upload.candidate_assembly import assemble_candidates

        assembled = assemble_candidates(candidates)
        merged = sum(len(a.corroborations) for a in assembled.ranked + assembled.actionable)
        logger.info(
            "semantic-shadow assembly: ranked=%d actionable=%d merged_twins=%d top=%s",
            len(assembled.ranked), len(assembled.actionable), merged,
            ",".join(a.candidate.recipe_id for a in assembled.ranked[:5]) or "-")
    except Exception:
        logger.exception("semantic-shadow comparison failed (response unaffected)")


def build_considered_set(conn, intent: Intent, client: LLMClient, *, entity: str | None = None,
                         catalog_source: str | None = None, roles=(), target_ref: str | None = None,
                         objective: str = "", feedback: str | None = None, now=None,
                         applicability: ApplicabilityResult | None = None,
                         is_live: bool = False, target_entity: str | None = None,
                         templates: Sequence[Template] | None = None,
                         generation_run_id: str | None = None,
                         scope: ConfirmedScope | None = None,
                         semantic_mode: str = "legacy") -> ConsideredSet:
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
    # The intake build's SIGNED reading, fetched once for its two consumers here: the use-case
    # ordering reads business_domain (Task 4), the near-label critic reads the window (Task 3).
    # None on the legacy path / unsigned round — both consumers degrade (today's order; abstain).
    signed_reading = signed_reading_for(
        conn, hypothesis=intent.hypothesis, actor_json=_actor_json(intent.actor))
    # The prediction goal enriches the generation prompt (hypothesis = the causal premise; goal = what
    # we're predicting). Redacted with the same discipline as the hypothesis before it reaches the LLM,
    # so a required-but-ignored field (bug_003) now actually shapes generation.
    redacted_goal = redact_free_text(objective, label="prediction goal")
    gen_objective = (f"{intent.redacted_hypothesis}\n\nprediction goal: {redacted_goal}"
                     if redacted_goal else intent.redacted_hypothesis)
    # SE-2: freeze the Layer-A semantic context BEFORE any model dispatch — the identity every
    # downstream decision (and the shadow lens) can be tied to. Assembled on this connection
    # (REPEATABLE READ on the production path), sealed into the metadata snapshot below.
    semantic_context = None
    if catalog_source is not None:
        semantic_context = build_generation_semantic_context(
            conn, catalog_source=catalog_source, roles=roles)
    report = recommend_feature_sets_report(
        conn, gen_objective, client, entity=entity, catalog_source=catalog_source,
        roles=roles, target_ref=target_ref, feedback=feedback, now=now)
    alternatives = list(report.sets)
    rejections = list(report.rejections)
    grounded_template_ids: frozenset[str] = frozenset()   # per-template grounding outcome for Task 5's
    rejected_template_ids: dict[str, tuple[str, ...]] = {}   # disposition stage (empty on a no-catalog run)
    incomplete_template_ids: dict[str, tuple[str, ...]] = {}
    binding_quality_by_template: dict[str, str] = {}   # per-template binding signal for the ranker (A3)
    recipe_grounding_context_by_candidate_key: dict[str, RecipeGroundingContextV1] = {}
    recipe_candidate_keys_by_recipe_id: dict[str, tuple[str, ...]] = {}
    # B4 two-source model: seed the considered set with grounded parametric templates alongside the LLM
    # alternatives — but only where a single catalog is in scope to ground them (an entity-only,
    # cross-catalog run has no one source to ground on). A template that clears the SAME gauntlet joins
    # as its own "templates" lens; one that fails (e.g. it binds the intent's target_ref -> leakage) is
    # surfaced in the rejections, not silently dropped. Everything downstream treats it as one more lens.
    if catalog_source is not None and semantic_mode == "semantic_v1" and scope is not None:
        # SE-7 — the ENFORCED projection: the recipe lens is served from the semantic engine
        # (frozen context → capability binder → eligibility fold → assembly → typed gauntlet),
        # not from legacy template grounding. One engine, one eligibility policy. The LLM lens
        # and everything downstream (near-label critic, ordering, recommendation) are unchanged
        # and origin-blind. Observations persist in the SAME transaction — the audit rows and
        # the response commit together on the serving path.
        from featuregen.overlay.upload.candidate_assembly import assemble_candidates
        from featuregen.overlay.upload.recipe_planning_lens import v2_recipe_candidates
        from featuregen.overlay.upload.semantic_candidate_store import (
            persist_semantic_candidates,
        )
        from featuregen.overlay.upload.semantic_projection import project_assembled_set

        v2_candidates = v2_recipe_candidates(
            conn, catalog_source=catalog_source, roles=roles, scope=scope,
            context=semantic_context)
        # SE-6 wire-up: the LLM proposes ABSTRACT intents (one audited structured call over the
        # physically-blind capability inventory) and the SAME binder decides which columns
        # serve. Recipe and intent candidates assemble TOGETHER — the semantic signature merges
        # twins, so an LLM intent that re-derives an authored recipe becomes corroboration on
        # the recipe's card, never a duplicate. Fail-soft: an intent-generation failure serves
        # the recipe lens alone (logged), because the engine's recipes never depend on a model.
        intent_rejections: list = []
        all_candidates = list(v2_candidates)
        if client is not None and semantic_context is not None:
            try:
                from featuregen.overlay.upload.recipe_planning_lens import (
                    llm_intent_candidates,
                )

                intent_cands, intent_rejections = llm_intent_candidates(
                    conn, client, context=semantic_context,
                    scope_leaves=(scope.primary, *scope.secondary) if scope.primary
                                 else (),
                    redacted_hypothesis=intent.redacted_hypothesis)
                all_candidates.extend(intent_cands)
            except Exception:
                logger.exception("semantic-v1 intent generation failed "
                                 "(recipe lens serves alone)")
        if semantic_context is not None:
            persist_semantic_candidates(
                conn, generation_run_id=generation_run_id or "unattributed",
                context=semantic_context, candidates=all_candidates)
        projection = project_assembled_set(
            assemble_candidates(all_candidates),
            catalog_source=catalog_source, target_ref=target_ref)
        rejections.extend({"name": r.get("detail", "intent"), "reason": r.get("detail", ""),
                           "code": r.get("code", "INTENT_REJECTED")}
                          for r in intent_rejections)
        grounded_template_ids = projection.grounded_ids
        rejected_template_ids = projection.rejected_ids
        binding_quality_by_template = projection.binding_by_id
        if projection.ideas:
            alternatives.append(FeatureSet(lens="templates", features=order_ideas_by_use_case(
                projection.ideas,
                signed_reading["business_domain"] if signed_reading else ())))
        rejections.extend(projection.rejections)
        logger.info(
            "semantic-v1 served: ideas=%d rejections=%d grounded=%s",
            len(projection.ideas), len(projection.rejections),
            ",".join(sorted(projection.grounded_ids)) or "-")
    elif catalog_source is not None:
        # Phase-1B scoped grounding: ground only the eligible recipe subset when scoping is on (else the
        # whole registry — byte-identical to today). Definition-mode + unscoped results bypass here.
        to_ground = _templates_to_ground(intent, applicability)
        # Task 4b: hypothesis-chosen parameters — a closed selection from the authored tuples,
        # dispatched ONCE per build for the cache misses only, applied at grounding (BEFORE the
        # gauntlet and the near-label critic — the walkthrough ordering fix). Abstain / no client
        # = empty overrides = the historical first-allowed-value defaults. Unconditional since
        # the flag retired (pre-live, 2026-08-11).
        param_overrides: dict[str, dict] = {}
        if client is not None:
            param_overrides = choose_params(
                conn, client, templates=to_ground,
                redacted_hypothesis=intent.redacted_hypothesis,
                call_ledger=CallLedger(max_provider_calls=1))
        candidates = _template_candidates(
            conn, catalog_source=catalog_source, roles=roles, target_ref=target_ref, now=now,
            templates=to_ground, params_by_id=param_overrides or None)
        grounded_template_ids = candidates.grounded_ids
        rejected_template_ids = candidates.rejected_ids
        binding_quality_by_template = candidates.binding_by_id
        incomplete_template_ids = candidates.incomplete_ids
        recipe_grounding_context_by_candidate_key = candidates.contexts
        recipe_candidate_keys_by_recipe_id = candidates.keys_by_recipe
        if candidates.ideas:
            # Task 4: the template lens is ORDERED by the signed reading's business_domain —
            # deterministic set intersection, shadow-counted always, applied only under
            # FEATUREGEN_USE_CASE_ORDERING. Removes nothing by construction.
            alternatives.append(FeatureSet(lens="templates", features=order_ideas_by_use_case(
                candidates.ideas,
                signed_reading["business_domain"] if signed_reading else ())))
        rejections.extend(candidates.rejections)
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
    # SE-7 part 2 — the semantic_shadow observation: run the V2 planning lens beside the
    # template lens and log the divergence. Read-only, fail-soft, response-invisible; the mode
    # is ROUTE-resolved (the builder never reads env), same discipline as ``is_live``.
    if semantic_mode == "semantic_shadow" and catalog_source is not None and scope is not None:
        _semantic_shadow_compare(conn, catalog_source=catalog_source, roles=roles, scope=scope,
                                 grounded_ids=grounded_template_ids,
                                 rejected_ids=rejected_template_ids,
                                 semantic_context=semantic_context,
                                 generation_run_id=generation_run_id)
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
    # Task 3 — the near-label critic, FLAG-ONLY and ORIGIN-BLIND: every surviving candidate (anchor
    # included, template and LLM alike) gets a {no_finding | too_close | abstain} annotation;
    # nothing is removed (relevance is ORDER, safety is REMOVAL — and this pass is advisory until
    # the explicit refusal decision). Unconditional since the flag retired (pre-live,
    # 2026-08-11). The label window is the intake build's SIGNED reading — no signed window
    # means every verdict abstains at zero model cost.
    window = signed_reading["target_window_days"] if signed_reading else None
    ledger = CallLedger(max_provider_calls=_NEAR_LABEL_MAX_CALLS)
    alternatives = [
        replace(fs, features=annotate_near_label(
            conn, client, ideas=fs.features,
            redacted_hypothesis=intent.redacted_hypothesis,
            label_window_days=window, call_ledger=ledger))
        for fs in alternatives]
    if anchor is not None:
        anchor = annotate_near_label(
            conn, client, ideas=[anchor], redacted_hypothesis=intent.redacted_hypothesis,
            label_window_days=window, call_ledger=ledger)[0]
    recommendation = (recommend_set(conn, alternatives, intent.redacted_hypothesis, client)
                      if any(s.features for s in alternatives) else None)
    cs = ConsideredSet(intent.intent_id, anchor, alternatives, recommendation, rejections,
                       applicability=applicability,
                       grounded_template_ids=grounded_template_ids,
                       rejected_template_ids=rejected_template_ids,
                       incomplete_template_ids=incomplete_template_ids,
                       binding_quality_by_template=binding_quality_by_template,
                       recipe_grounding_context_by_candidate_key=(
                           recipe_grounding_context_by_candidate_key
                       ),
                       recipe_candidate_keys_by_recipe_id=recipe_candidate_keys_by_recipe_id)
    logger.info("considered-set built: intent=%s catalog=%s roles=%s → lenses=%s, %d rejected, "
                "anchor=%s, recommended_lens=%s",
                intent.intent_id, catalog_source, tuple(roles),
                {a.lens: len(a.features) for a in alternatives}, len(rejections),
                bool(anchor), recommendation.recommended_lens if recommendation else None)
    # Delivery C0 Task 5: build the immutable catalog snapshot the set was authored against BEFORE the
    # considered-set INSERT — a projection-lagged view raises here (→ route 503) with NO considered-set
    # row written, and the snapshot + considered set commit atomically in the one feature transaction.
    snap_run_id, snap_id, snap_hash = _persist_considered_snapshot(
        conn, cs, intent, generation_run_id=generation_run_id, roles=roles,
        semantic_context=semantic_context,
        catalog_source=catalog_source, is_live=is_live)
    cs = _with_option_ids(cs, snap_run_id or f"legacy:{intent.intent_id}")
    revision_id, considered_hash = _persist_considered_revision(
        conn,
        cs,
        generation_run_id=snap_run_id,
        metadata_snapshot_id=snap_id,
        metadata_snapshot_content_hash=snap_hash,
    )
    cs = replace(
        cs,
        considered_revision_id=revision_id,
        considered_content_hash=considered_hash,
    )
    conn.execute(   # persist the validated set so /contract/draft reconstructs the chosen feature here
        "INSERT INTO contract_considered "
        "(intent_id, considered, generation_run_id, snapshot_id, snapshot_content_hash, "
        "considered_revision_id, considered_content_hash) "
        "VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s) "
        "ON CONFLICT (intent_id) DO UPDATE SET considered = EXCLUDED.considered, "
        "generation_run_id = EXCLUDED.generation_run_id, snapshot_id = EXCLUDED.snapshot_id, "
        "snapshot_content_hash = EXCLUDED.snapshot_content_hash, "
        "considered_revision_id = EXCLUDED.considered_revision_id, "
        "considered_content_hash = EXCLUDED.considered_content_hash",
        (
            intent.intent_id,
            json.dumps(_public_considered_snapshot(conn, cs)),
            snap_run_id,
            snap_id,
            snap_hash,
            revision_id,
            considered_hash,
        ))
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
    d = {"name": f.name, "description": f.description,
         "derives_from": f.derives_from, "aggregation": f.aggregation,
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
    # D14 (review F2): only-when-non-empty, exactly like `binding_fact_keys` above. This dict is
    # hashed into `option_id` and `considered_content_hash`, so a candidate that licensed nothing —
    # every pre-D14 snapshot and the overwhelming majority of candidates — keeps byte-identical
    # bytes. A candidate that DID need a policy could not have existed before the gate could clear
    # one, so there are no pre-existing bytes for the non-empty case to break.
    if f.personal_data_policy_revision_ids:
        d["personal_data_policy_revision_ids"] = list(f.personal_data_policy_revision_ids)
    if f.planner_applicability != "not_applicable_nonrecipe":
        d["planner_applicability"] = f.planner_applicability
    if f.physical_plan_id is not None:
        d["physical_plan_id"] = f.physical_plan_id
    if f.planner_declaration_id is not None:
        d["planner_declaration_id"] = f.planner_declaration_id
    # Task 3: only-when-present, same byte-identity strategy as every additive key above. The
    # verdict is ADVISORY card metadata; it round-trips so the Gate-1 reload shows what the
    # reviewer saw, and it participates in option identity exactly because it is part of the
    # reviewed artifact.
    if f.near_label_verdict is not None:
        d["near_label_verdict"] = f.near_label_verdict
        d["near_label_rationale"] = f.near_label_rationale
    # Task 4b: only-when-present (populated only under FEATUREGEN_PARAM_CHOICE, so flag-off
    # snapshots keep their historical bytes).
    if f.param_alternatives:
        d["param_alternatives"] = f.param_alternatives
    return d


def _option_positions(cs: ConsideredSet):
    if cs.anchor is not None:
        yield "anchor", "anchor", "anchor", cs.anchor
    for set_index, feature_set in enumerate(cs.alternatives):
        for feature_index, feature in enumerate(feature_set.features):
            yield (
                f"alternative:{set_index}:{feature_index}",
                "alternative",
                feature_set.lens,
                feature,
            )


def _candidate_identity(
    *,
    path: str,
    source: str,
    lens: str,
    feature: FeatureIdea,
) -> dict:
    return {
        "version": "considered-candidate-v2",
        "path": path,
        "source": source,
        "lens": lens,
        "feature": _idea_json(feature),
    }


def _with_option_ids(cs: ConsideredSet, generation_identity: str) -> ConsideredSet:
    option_ids: dict[str, str] = {}
    seen: set[str] = set()
    for path, source, lens, feature in _option_positions(cs):
        identity_hash = canonical_hash(
            _candidate_identity(path=path, source=source, lens=lens, feature=feature))
        option_id = "opt_" + canonical_hash({
            "version": "considered-option-id-v2",
            "generation_identity": generation_identity,
            "candidate_identity_hash": identity_hash,
        })[:32]
        if option_id in seen:
            raise Gate1Error("duplicate considered option identity")
        seen.add(option_id)
        option_ids[path] = option_id
    return replace(cs, option_ids_by_path=option_ids)


def _idea_with_option_id(feature: FeatureIdea | None, option_id: str | None) -> dict | None:
    body = _idea_json(feature)
    if body is not None and option_id is not None:
        body["option_id"] = option_id
    return body


def _recipe_candidate_key(cs: ConsideredSet, feature: FeatureIdea) -> str | None:
    if feature.generation_source != "recipe" or feature.recipe_id is None:
        return None
    keys = cs.recipe_candidate_keys_by_recipe_id.get(feature.recipe_id, ())
    return keys[0] if len(keys) == 1 else None


def _public_considered_snapshot(conn, cs: ConsideredSet) -> dict:
    return {
        "anchor": _idea_with_option_id(cs.anchor, cs.option_ids_by_path.get("anchor")),
        "alternatives": [
            {
                "lens": feature_set.lens,
                "features": [
                    _idea_with_option_id(
                        feature,
                        cs.option_ids_by_path.get(
                            f"alternative:{set_index}:{feature_index}"),
                    )
                    for feature_index, feature in enumerate(feature_set.features)
                ],
                "signals": set_signals(conn, feature_set),
            }
            for set_index, feature_set in enumerate(cs.alternatives)
        ],
        "recommendation": None if cs.recommendation is None else {
            "recommended_lens": cs.recommendation.recommended_lens,
            "reasoning": cs.recommendation.reasoning, "caveat": cs.recommendation.caveat},
    }


def _private_considered_revision_snapshot(conn, cs: ConsideredSet) -> dict:
    """Canonical public projection plus server-only recipe replay context."""
    options_by_id: dict[str, dict] = {}
    for path, source, lens, feature in _option_positions(cs):
        option_id = cs.option_ids_by_path.get(path)
        if option_id is None or option_id in options_by_id:
            raise Gate1Error("considered option map is incomplete or duplicated")
        identity = _candidate_identity(
            path=path, source=source, lens=lens, feature=feature)
        options_by_id[option_id] = {
            "source": source,
            "lens": lens,
            "canonical_candidate_identity": identity,
            "canonical_candidate_identity_hash": canonical_hash(identity),
            "recipe_candidate_key": _recipe_candidate_key(cs, feature),
        }
    return {
        "version": CONSIDERED_CANONICALIZATION_VERSION,
        "public": {
            **_public_considered_snapshot(conn, cs),
            "rejections": cs.rejections,
        },
        "options_by_id": {
            option_id: options_by_id[option_id] for option_id in sorted(options_by_id)
        },
        "recipe_grounding_context_by_candidate_key": {
            key: context.to_json()
            for key, context in sorted(cs.recipe_grounding_context_by_candidate_key.items())
        },
        "recipe_candidate_keys_by_recipe_id": {
            recipe_id: list(keys)
            for recipe_id, keys in sorted(cs.recipe_candidate_keys_by_recipe_id.items())
        },
    }


def _snapshot(conn, cs: ConsideredSet) -> dict:
    """Compatibility alias for the public-only considered snapshot."""
    return _public_considered_snapshot(conn, cs)


def _persist_considered_revision(
    conn,
    cs: ConsideredSet,
    *,
    generation_run_id: str | None,
    metadata_snapshot_id: str | None,
    metadata_snapshot_content_hash: str | None,
) -> tuple[str | None, str | None]:
    """Persist one immutable, hash-sealed revision for a generation run."""
    if generation_run_id is None:
        return None, None
    considered = _private_considered_revision_snapshot(conn, cs)
    envelope = {
        "version": CONSIDERED_CANONICALIZATION_VERSION,
        "intent_id": cs.intent_id,
        "generation_run_id": generation_run_id,
        "metadata_snapshot_id": metadata_snapshot_id,
        "metadata_snapshot_content_hash": metadata_snapshot_content_hash,
        "considered": considered,
    }
    digest = canonical_hash(envelope)
    revision_id = mint_id("crv")
    conn.execute(
        "INSERT INTO contract_considered_revision "
        "(considered_revision_id, intent_id, generation_run_id, metadata_snapshot_id, "
        "metadata_snapshot_content_hash, considered_json, considered_content_hash, "
        "canonicalization_version) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
        "ON CONFLICT (intent_id, generation_run_id) DO NOTHING",
        (
            revision_id,
            cs.intent_id,
            generation_run_id,
            metadata_snapshot_id,
            metadata_snapshot_content_hash,
            json.dumps(considered),
            digest,
            CONSIDERED_CANONICALIZATION_VERSION,
        ),
    )
    row = conn.execute(
        "SELECT considered_revision_id, considered_content_hash "
        "FROM contract_considered_revision WHERE intent_id = %s AND generation_run_id = %s",
        (cs.intent_id, generation_run_id),
    ).fetchone()
    if row is None or row[1] != digest:
        raise Gate1Error("considered revision conflicts with an existing generation run")
    return row[0], row[1]


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
        "(intent_id, chosen_source, chosen_option_id, why, actor, considered, "
        "considered_revision_id, considered_content_hash) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
        "ON CONFLICT (intent_id) DO UPDATE SET chosen_source = EXCLUDED.chosen_source, "
        "chosen_option_id = EXCLUDED.chosen_option_id, why = EXCLUDED.why, actor = EXCLUDED.actor, "
        "considered = EXCLUDED.considered, "
        "considered_revision_id = EXCLUDED.considered_revision_id, "
        "considered_content_hash = EXCLUDED.considered_content_hash",
        (considered.intent_id, chosen_source, chosen_option_id, why,
         _actor_json(actor), json.dumps(_public_considered_snapshot(conn, considered)),
         considered.considered_revision_id, considered.considered_content_hash))
    return chosen_option_id


def _idea_from_json(d: dict) -> FeatureIdea:
    return FeatureIdea(
        name=d["name"], description=d.get("description", ""),
        derives_from=list(d.get("derives_from", [])),
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
        personal_data_policy_revision_ids=tuple(
            str(r) for r in d.get("personal_data_policy_revision_ids", ())),
        planner_applicability=d.get("planner_applicability", "not_applicable_nonrecipe"),
        physical_plan_id=d.get("physical_plan_id"),
        planner_declaration_id=d.get("planner_declaration_id"),
        near_label_verdict=d.get("near_label_verdict"),
        near_label_rationale=d.get("near_label_rationale", ""),
        param_alternatives=d.get("param_alternatives", ""))


def _chosen_feature_from_snapshot(
    snap: dict,
    chosen_source: str,
    chosen_option_id: str,
) -> FeatureIdea | None:
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


# ── E4b: re-attaching the template-declared operand roles at reload ───────────────────────────────
# The roles are DELIBERATELY off every wire shape (`_idea_json` is hashed into `option_id` and
# `considered_content_hash`), so they cannot be read back off the candidate. They do not need to be:
# the private revision ALREADY persists the exact per-role column bindings under
# `recipe_grounding_context_by_candidate_key` (`GroundedNeedBinding.role` + `.graph_object_ref`, the
# same grounding that produced `binding_resolutions`). Re-deriving from THAT is a pure read of
# already-persisted, already-hash-sealed server state — no new field, no new bytes, no identity change.
def _revision_recipe_candidate_key(considered: dict, *, option_id: str | None,
                                   feature: FeatureIdea | None) -> str | None:
    """The grounding-context key for a chosen option: its own recorded key when the caller resolved an
    exact option id, else the recipe's key when that recipe contributed EXACTLY ONE candidate (the same
    unambiguous rule ``_recipe_candidate_key`` applied when writing it). Ambiguity yields None — the
    roles are then absent and the gauntlet falls back, never guesses."""
    record = (considered.get("options_by_id") or {}).get(option_id) if option_id else None
    if isinstance(record, dict) and record.get("recipe_candidate_key"):
        return str(record["recipe_candidate_key"])
    if feature is not None and feature.recipe_id:
        keys = (considered.get("recipe_candidate_keys_by_recipe_id") or {}).get(feature.recipe_id)
        if isinstance(keys, list) and len(keys) == 1:
            return str(keys[0])
    return None


def _operand_roles_from_revision(considered: dict, feature: FeatureIdea | None, *,
                                 option_id: str | None = None) -> FeatureIdea | None:
    """``feature`` with its template-declared ``operand_roles`` restored from the revision's private
    recipe grounding context (sorted + deduped, matching ``_operand_roles``). Unchanged when there is
    no context for it — an LLM candidate, a legacy revision, or an ambiguous recipe key."""
    if feature is None:
        return None
    key = _revision_recipe_candidate_key(considered, option_id=option_id, feature=feature)
    context = (considered.get("recipe_grounding_context_by_candidate_key") or {}).get(key) if key \
        else None
    if not isinstance(context, dict):
        return feature
    pairs = tuple(sorted({
        (str(b["graph_object_ref"]), str(b["role"]))
        for b in context.get("need_bindings", ())
        if isinstance(b, dict) and b.get("graph_object_ref") and b.get("role")}))
    return replace(feature, operand_roles=pairs) if pairs else feature


def _public_option_entries(public: dict) -> list[tuple[str, str, dict]]:
    entries: list[tuple[str, str, dict]] = []
    anchor = public.get("anchor")
    if anchor is not None:
        entries.append(("anchor", "anchor", anchor))
    for feature_set in public.get("alternatives", []):
        lens = feature_set.get("lens")
        for feature in feature_set.get("features", []):
            entries.append(("alternative", lens, feature))
    return entries


def _chosen_option_from_revision(
    considered: dict,
    option_id: str,
) -> tuple[FeatureIdea, str, str]:
    """Resolve one opaque option from a verified v2 revision and cross-check its public projection."""
    if considered.get("version") != CONSIDERED_CANONICALIZATION_VERSION:
        raise Gate1Error("considered revision does not support exact option identity")
    options = considered.get("options_by_id")
    if not isinstance(options, dict):
        raise Gate1Error("considered revision option map is missing")
    public_entries = _public_option_entries(considered.get("public", {}))
    public_ids = [entry[2].get("option_id") for entry in public_entries]
    if (
        any(not isinstance(public_id, str) for public_id in public_ids)
        or len(public_ids) != len(set(public_ids))
        or set(public_ids) != set(options)
    ):
        raise Gate1Error("considered revision option identities are inconsistent")
    record = options.get(option_id)
    if not isinstance(record, dict):
        # The revision itself verified fine above; the caller simply named something not in it.
        raise UnknownConsideredOption("unknown considered option")
    identity = record.get("canonical_candidate_identity")
    identity_hash = record.get("canonical_candidate_identity_hash")
    if not isinstance(identity, dict) or canonical_hash(identity) != identity_hash:
        raise Gate1Error("considered option identity hash mismatch")
    matches = [
        (source, lens, feature)
        for source, lens, feature in public_entries
        if feature.get("option_id") == option_id
    ]
    if len(matches) != 1:
        raise Gate1Error("considered option is not unique in the public projection")
    source, lens, public_feature = matches[0]
    feature_without_option = {
        key: value for key, value in public_feature.items() if key != "option_id"}
    if (
        identity.get("source") != source
        or identity.get("lens") != lens
        or identity.get("feature") != feature_without_option
        or record.get("source") != source
        or record.get("lens") != lens
    ):
        raise Gate1Error("considered option projection does not match its private identity")
    feature = _operand_roles_from_revision(
        considered, _idea_from_json(feature_without_option), option_id=option_id)
    return feature, source, identity_hash


def chosen_feature(conn, intent_id: str, chosen_source: str,
                   chosen_option_id: str) -> FeatureIdea | None:
    """Legacy compatibility reader over the mutable latest considered-set pointer."""
    row = conn.execute("SELECT considered FROM contract_considered WHERE intent_id = %s",
                       (intent_id,)).fetchone()
    return (
        _chosen_feature_from_snapshot(row[0], chosen_source, chosen_option_id)
        if row is not None
        else None
    )


@dataclass(frozen=True, slots=True)
class DraftChoice:
    feature: FeatureIdea
    snapshot_lineage: dict | None
    considered_revision_id: str | None
    considered_content_hash: str | None
    choice_id: str | None = None
    generation_run_id: str | None = None
    option_id: str | None = None
    canonical_candidate_identity_hash: str | None = None


def _verified_considered_revision_payload(
    conn,
    intent_id: str,
    generation_run_id: str,
) -> tuple[dict, dict, str, str]:
    row = conn.execute(
        "SELECT considered_revision_id, intent_id, generation_run_id, metadata_snapshot_id, "
        "metadata_snapshot_content_hash, considered_json, considered_content_hash, "
        "canonicalization_version "
        "FROM contract_considered_revision "
        "WHERE intent_id = %s AND generation_run_id = %s FOR SHARE",
        (intent_id, generation_run_id),
    ).fetchone()
    if row is None:
        raise Gate1Error("REGENERATE_FROM_CURRENT_CONSIDERED_SET")
    revision_id, stored_intent, stored_run, snapshot_id, snapshot_hash, considered, digest, version = row
    if (
        stored_intent != intent_id
        or stored_run != generation_run_id
        or considered.get("version") != version
    ):
        raise Gate1Error("considered revision lineage is inconsistent")
    envelope = {
        "version": version,
        "intent_id": stored_intent,
        "generation_run_id": stored_run,
        "metadata_snapshot_id": snapshot_id,
        "metadata_snapshot_content_hash": snapshot_hash,
        "considered": considered,
    }
    if canonical_hash(envelope) != digest:
        raise Gate1Error("considered revision content hash mismatch")
    # The immutable considered revision always has generation lineage. Snapshot fields are nullable
    # only for compatibility callers that do not run under the production REPEATABLE READ boundary.
    lineage = {
        "generation_run_id": stored_run,
        "snapshot_id": snapshot_id,
        "content_hash": snapshot_hash,
    }
    return considered, lineage, revision_id, digest


def verified_considered_revision_by_id(conn, considered_revision_id: str) -> dict:
    """SE-11 step 4 — load ONE immutable considered revision by id, verified the same way the
    Gate-1 choice path verifies it (envelope re-hash against the stored digest), and return the
    full stored payload. The caller serves option detail FROM THIS STORED REVISION ONLY — never
    a wider live catalog read to decorate it."""
    row = conn.execute(
        "SELECT considered_revision_id, intent_id, generation_run_id, metadata_snapshot_id, "
        "metadata_snapshot_content_hash, considered_json, considered_content_hash, "
        "canonicalization_version "
        "FROM contract_considered_revision WHERE considered_revision_id = %s",
        (considered_revision_id,),
    ).fetchone()
    if row is None:
        raise Gate1Error("UNKNOWN_CONSIDERED_REVISION")
    revision_id, intent_id, run_id, snapshot_id, snapshot_hash, considered, digest, version = row
    if considered.get("version") != version:
        raise Gate1Error("considered revision lineage is inconsistent")
    envelope = {
        "version": version,
        "intent_id": intent_id,
        "generation_run_id": run_id,
        "metadata_snapshot_id": snapshot_id,
        "metadata_snapshot_content_hash": snapshot_hash,
        "considered": considered,
    }
    if canonical_hash(envelope) != digest:
        raise Gate1Error("considered revision content hash mismatch")
    return {
        "considered_revision_id": revision_id,
        "intent_id": intent_id,
        "generation_run_id": run_id,
        "considered_content_hash": digest,
        "considered": considered,
    }


def _verified_considered_revision(
    conn,
    intent_id: str,
    generation_run_id: str,
) -> tuple[dict, dict, str, str]:
    """Compatibility reader returning the verified public projection."""
    considered, lineage, revision_id, digest = _verified_considered_revision_payload(
        conn, intent_id, generation_run_id)
    return considered["public"], lineage, revision_id, digest


def _record_exact_choice(
    conn,
    *,
    intent_id: str,
    generation_run_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    option_id: str,
    canonical_candidate_identity_hash: str,
    actor,
    why: str,
) -> str:
    choice_id = mint_id("g1c")
    actor_body = _actor_json(actor)
    if actor_body is None:
        raise Gate1Error("an actor is required for an exact Gate #1 choice")
    actor_value = json.loads(actor_body)
    conn.execute(
        "INSERT INTO contract_gate1_choice_revision "
        "(choice_id, intent_id, generation_run_id, considered_revision_id, "
        "considered_content_hash, option_id, canonical_candidate_identity_hash, actor, why) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) "
        "ON CONFLICT (generation_run_id, considered_revision_id, option_id) DO NOTHING",
        (
            choice_id,
            intent_id,
            generation_run_id,
            considered_revision_id,
            considered_content_hash,
            option_id,
            canonical_candidate_identity_hash,
            actor_body,
            why,
        ),
    )
    row = conn.execute(
        "SELECT choice_id, intent_id, considered_content_hash, "
        "canonical_candidate_identity_hash, actor, why "
        "FROM contract_gate1_choice_revision "
        "WHERE generation_run_id = %s AND considered_revision_id = %s AND option_id = %s",
        (generation_run_id, considered_revision_id, option_id),
    ).fetchone()
    if row is None or (
        row[1] != intent_id
        or row[2] != considered_content_hash
        or row[3] != canonical_candidate_identity_hash
        or row[4] != actor_value
        or row[5] != why
    ):
        raise Gate1Error("exact Gate #1 choice conflicts with an existing selection")
    return row[0]


def select_and_record_gate1_choice(
    conn,
    intent_id: str,
    *,
    chosen_source: str,
    chosen_option_id: str,
    actor,
    why: str = "",
    expected_generation_run_id: str | None = None,
) -> DraftChoice | None:
    """Atomically select and record a choice from the exact immutable revision the user saw."""
    if confirmation_required():
        if expected_generation_run_id is None:
            raise Gate1Error("REGENERATE_FROM_CURRENT_CONSIDERED_SET")
        considered, exact_lineage, exact_revision_id, exact_revision_hash = (
            _verified_considered_revision_payload(
                conn, intent_id, expected_generation_run_id))
        exact_feature, _source, candidate_identity_hash = _chosen_option_from_revision(
            considered, chosen_option_id)
        choice_id = _record_exact_choice(
            conn,
            intent_id=intent_id,
            generation_run_id=expected_generation_run_id,
            considered_revision_id=exact_revision_id,
            considered_content_hash=exact_revision_hash,
            option_id=chosen_option_id,
            canonical_candidate_identity_hash=candidate_identity_hash,
            actor=actor,
            why=why,
        )
        return DraftChoice(
            exact_feature,
            exact_lineage,
            exact_revision_id,
            exact_revision_hash,
            choice_id=choice_id,
            generation_run_id=expected_generation_run_id,
            option_id=chosen_option_id,
            canonical_candidate_identity_hash=candidate_identity_hash,
        )
    public: dict
    lineage: dict | None
    revision_id: str | None
    revision_hash: str | None
    revision: dict | None = None   # the verified PRIVATE payload, when one exists (E4b role source)
    if expected_generation_run_id is not None:
        revision, lineage, revision_id, revision_hash = _verified_considered_revision_payload(
            conn, intent_id, expected_generation_run_id)
        public = revision["public"]
    else:
        row = conn.execute(
            "SELECT considered, generation_run_id, snapshot_id, snapshot_content_hash, "
            "considered_revision_id, considered_content_hash "
            "FROM contract_considered WHERE intent_id = %s FOR SHARE",
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        # Scope confirmation is a one-way authority transition for an intent. An emergency
        # legacy-unscoped regeneration may replace the mutable latest pointer, but it must not
        # downgrade an intent that previously entered confirmed-scope execution back to an
        # unpinned draft. The exact generation token remains mandatory thereafter.
        scope_governed_intent = conn.execute(
            "SELECT 1 FROM confirmed_generation_scope WHERE intent_id = %s LIMIT 1",
            (intent_id,),
        ).fetchone() is not None
        if scope_governed_intent:
            raise Gate1Error("REGENERATE_FROM_CURRENT_CONSIDERED_SET")
        public = row[0]
        lineage = (
            {"generation_run_id": row[1], "snapshot_id": row[2], "content_hash": row[3]}
            if row[2] is not None
            else None
        )
        revision_id, revision_hash = None, None

    feature = _chosen_feature_from_snapshot(public, chosen_source, chosen_option_id)
    if revision is not None:
        feature = _operand_roles_from_revision(revision, feature)
    if feature is None:
        return None
    conn.execute(
        "INSERT INTO contract_gate1_choice "
        "(intent_id, chosen_source, chosen_option_id, why, actor, considered, "
        "considered_revision_id, considered_content_hash) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
        "ON CONFLICT (intent_id) DO UPDATE SET chosen_source = EXCLUDED.chosen_source, "
        "chosen_option_id = EXCLUDED.chosen_option_id, why = EXCLUDED.why, "
        "actor = EXCLUDED.actor, considered = EXCLUDED.considered, "
        "considered_revision_id = EXCLUDED.considered_revision_id, "
        "considered_content_hash = EXCLUDED.considered_content_hash",
        (
            intent_id,
            chosen_source,
            chosen_option_id,
            why,
            _actor_json(actor),
            json.dumps(public),
            revision_id,
            revision_hash,
        ),
    )
    return DraftChoice(feature, lineage, revision_id, revision_hash)


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
    result = select_and_record_gate1_choice(
        conn,
        intent_id,
        chosen_source=chosen_source,
        chosen_option_id=chosen_option_id,
        actor=actor,
        why=why,
    )
    if result is None:
        raise Gate1Error("chosen option is not in the recorded considered set")


def gate1_choice(conn, intent_id: str) -> dict | None:
    """The human's RECORDED Gate #1 choice for an intent, or None if none was recorded. Used by
    /contract/confirm to prove a governed feature was actually chosen from the considered set."""
    row = conn.execute(
        "SELECT chosen_source, chosen_option_id, considered_revision_id, considered_content_hash "
        "FROM contract_gate1_choice WHERE intent_id = %s",
        (intent_id,)).fetchone()
    return (
        {
            "chosen_source": row[0],
            "chosen_option_id": row[1],
            "considered_revision_id": row[2],
            "considered_content_hash": row[3],
        }
        if row
        else None
    )


def recorded_gate1_draft_choice(conn, intent_id: str) -> DraftChoice | None:
    """Reload the chosen feature and lineage from the immutable revision recorded at draft time."""
    choice = gate1_choice(conn, intent_id)
    if choice is None:
        return None
    revision_id = choice["considered_revision_id"]
    if revision_id is None:
        feature = chosen_feature(
            conn, intent_id, choice["chosen_source"], choice["chosen_option_id"])
        return (
            DraftChoice(feature, considered_snapshot_lineage(conn, intent_id), None, None)
            if feature is not None
            else None
        )
    row = conn.execute(
        "SELECT generation_run_id FROM contract_considered_revision "
        "WHERE considered_revision_id = %s AND intent_id = %s",
        (revision_id, intent_id),
    ).fetchone()
    if row is None:
        raise Gate1Error("recorded considered revision is missing")
    revision, lineage, verified_id, verified_hash = _verified_considered_revision_payload(
        conn, intent_id, row[0])
    if (
        verified_id != revision_id
        or verified_hash != choice["considered_content_hash"]
    ):
        raise Gate1Error("recorded choice revision hash mismatch")
    feature = _operand_roles_from_revision(revision, _chosen_feature_from_snapshot(
        revision["public"], choice["chosen_source"], choice["chosen_option_id"]))
    return (
        DraftChoice(feature, lineage, verified_id, verified_hash)
        if feature is not None
        else None
    )


def recorded_gate1_choice_revision(
    conn,
    *,
    choice_id: str,
    intent_id: str,
    actor,
) -> DraftChoice | None:
    """Reload one append-only scoped choice by its exact id, actor, run and verified revision."""
    row = conn.execute(
        "SELECT generation_run_id, considered_revision_id, considered_content_hash, option_id, "
        "canonical_candidate_identity_hash "
        "FROM contract_gate1_choice_revision "
        "WHERE choice_id = %s AND intent_id = %s AND actor = %s::jsonb",
        (choice_id, intent_id, _actor_json(actor)),
    ).fetchone()
    if row is None:
        return None
    generation_run_id, revision_id, revision_hash, option_id, candidate_hash = row
    considered, lineage, verified_id, verified_hash = _verified_considered_revision_payload(
        conn, intent_id, generation_run_id)
    if verified_id != revision_id or verified_hash != revision_hash:
        raise Gate1Error("recorded choice revision hash mismatch")
    feature, _source, verified_candidate_hash = _chosen_option_from_revision(
        considered, option_id)
    if verified_candidate_hash != candidate_hash:
        raise Gate1Error("recorded choice candidate identity hash mismatch")
    return DraftChoice(
        feature,
        lineage,
        verified_id,
        verified_hash,
        choice_id=choice_id,
        generation_run_id=generation_run_id,
        option_id=option_id,
        canonical_candidate_identity_hash=candidate_hash,
    )

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
from dataclasses import dataclass, field
from datetime import timedelta

from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract.intake import Intent, redact_free_text
from featuregen.overlay.upload.feature_assist import (
    FeatureIdea,
    FeatureSet,
    SetRecommendation,
    _candidate_columns,
    _validate_idea,
    recommend_feature_sets_report,
    recommend_features,
    recommend_set,
    set_signals,
)
from featuregen.overlay.upload.taxonomy.applicability import (
    in_scope_recipes,
    scope_from_recognition,
)
from featuregen.overlay.upload.taxonomy.recognizer import recognize
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    GroundedFeature,
    Template,
    ground_all,
)

logger = logging.getLogger(__name__)


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
                         fresh_within: timedelta = timedelta(hours=24),
                         ) -> tuple[list[FeatureIdea], list[dict]]:
    """Ground ALL_TEMPLATES on this catalog and gauntlet-check each grounded candidate the SAME way LLM
    candidates are (feature_assist._validate_idea, over the identical read-scoped candidate universe).
    Grounding is the router — a template family surfaces only where its distinctive concepts exist, so a
    churn-shaped catalog yields exactly the churn lens. Grounding refuses tagged leakage anchors by
    construction, but the intent's SPECIFIC target_ref may not be a tagged anchor — the reused gauntlet
    still rejects any candidate that binds it (plus freshness / additivity / PIT / units). Returns
    (surviving ideas, {name, reason, code} rejects)."""
    grounded = ground_all(conn, ALL_TEMPLATES, catalog_source=catalog_source, roles=roles)
    if not grounded:
        return [], []
    by_id = {t.id: t for t in ALL_TEMPLATES}
    cols = _candidate_columns(conn, catalog_source, roles)   # the SAME candidate universe the LLM saw
    known = {c["object_ref"] for c in cols}
    src_of: dict[str, set[str]] = {}
    for c in cols:
        src_of.setdefault(c["object_ref"], set()).add(c["catalog_source"])
    ideas: list[FeatureIdea] = []
    rejections: list[dict] = []
    for gf in grounded:
        idea = _idea_from_grounded(gf, by_id[gf.template_id])
        raw = {"name": idea.name, "description": idea.description,
               "derives_from": list(idea.derives_from), "aggregation": idea.aggregation,
               "grain_table": idea.grain_table, "rationale": idea.rationale}
        _, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)
        if rej is None:
            ideas.append(idea)   # keep the converted idea (identical to the gauntlet's rebuild)
        else:
            rejections.append({"name": idea.name, "reason": rej.message, "code": rej.code})
    return ideas, rejections


def build_considered_set(conn, intent: Intent, client: LLMClient, *, entity: str | None = None,
                         catalog_source: str | None = None, roles=(), target_ref: str | None = None,
                         objective: str = "", feedback: str | None = None, now=None) -> ConsideredSet:
    """Discovery loop → validated alternatives; the anchor is the requester's definition run through the
    same validated loop (definition mode only). Every option shown to the human has passed the gauntlet.
    Persists the intent + target_ref (M6, BLOCKER 2) and the considered-set snapshot (BLOCKER 1) when the
    flow reaches Gate #1."""
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
    # B4 two-source model: seed the considered set with grounded parametric templates alongside the LLM
    # alternatives — but only where a single catalog is in scope to ground them (an entity-only,
    # cross-catalog run has no one source to ground on). A template that clears the SAME gauntlet joins
    # as its own "templates" lens; one that fails (e.g. it binds the intent's target_ref -> leakage) is
    # surfaced in the rejections, not silently dropped. Everything downstream treats it as one more lens.
    if catalog_source is not None:
        template_ideas, template_rejections = _template_candidates(
            conn, catalog_source=catalog_source, roles=roles, target_ref=target_ref, now=now)
        if template_ideas:
            alternatives.append(FeatureSet(lens="templates", features=template_ideas))
        rejections.extend(template_rejections)
    anchor: FeatureIdea | None = None
    if intent.intake_mode == "definition":
        ideas = recommend_features(
            conn, intent.redacted_definition, client, entity=entity, catalog_source=catalog_source,
            roles=roles, target_ref=target_ref, now=now, target=1)
        anchor = ideas[0] if ideas else None
    recommendation = (recommend_set(conn, alternatives, intent.redacted_hypothesis, client)
                      if any(s.features for s in alternatives) else None)
    cs = ConsideredSet(intent.intent_id, anchor, alternatives, recommendation, rejections)
    conn.execute(   # persist the validated set so /contract/draft reconstructs the chosen feature here
        "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (intent_id) DO UPDATE SET considered = EXCLUDED.considered",
        (intent.intent_id, json.dumps(_snapshot(conn, cs))))
    _shadow_recognition(client, intent, redacted_goal, cs)   # flag-gated, LOG-ONLY; never filters cs
    return cs


# ── Phase-1A: in-flow shadow recognizer (flag-gated, log-only) ─────────────────────────────────────
# When FEATUREGEN_INTENT_RECOGNITION_SHADOW=1, run the LLM-only use-case recognizer over the SAME
# redacted hypothesis/goal that drove generation and LOG what it WOULD scope to — the proposed
# primary/secondary objectives + the would-be in-scope recipe count vs the templates actually grounded.
# It is behaviour-neutral by construction: default OFF (recognize is never called, no extra LLM call,
# zero behaviour change), and even when ON it only logs — it NEVER filters or alters `cs`, never
# persists (that is Phase 1B), and never raises (any error is logged and swallowed so shadow can't
# break generation). See docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md Task 6.
def _intent_recognition_shadow_enabled() -> bool:
    """The in-flow shadow recognizer is OFF by default — grounding is untouched unless a deployment
    opts in with FEATUREGEN_INTENT_RECOGNITION_SHADOW=1 (dev/measurement only)."""
    return os.environ.get("FEATUREGEN_INTENT_RECOGNITION_SHADOW", "0") == "1"


def _grounded_template_count(cs: ConsideredSet) -> int:
    """The number of grounded parametric-template candidates that actually reached the considered set
    (the "templates" lens features) — the shadow log's real-world comparison point for the recognizer's
    would-be in-scope recipe count. Read-only; never mutates `cs`."""
    return sum(len(s.features) for s in cs.alternatives if s.lens == "templates")


def _shadow_recognition(client: LLMClient, intent: Intent, redacted_goal: str,
                        cs: ConsideredSet) -> None:
    """Flag-gated, LOG-ONLY shadow recognition. When the flag is OFF (default) this is a true no-op —
    `recognize` is NOT called, so there is no extra LLM call and zero behaviour change. When ON, it
    recognises the SAME redacted hypothesis/goal that drove generation, maps the result to a would-be
    in-scope recipe set, and logs the proposed scope alongside the grounded-template count. It NEVER
    uses the result to filter `cs`, NEVER persists, and NEVER raises — the whole block is wrapped so an
    unexpected error is logged and swallowed rather than breaking generation."""
    if not _intent_recognition_shadow_enabled():
        return
    try:
        result = recognize(
            client, redacted_hypothesis=intent.redacted_hypothesis,
            redacted_goal=redacted_goal or None)
        scope = scope_from_recognition(result)
        primary_scoped, _ = in_scope_recipes(scope)
        logger.info(
            "intent-recognition shadow: intent_id=%s status=%s primary=%s secondary=%s "
            "in_scope_recipes=%d grounded_templates=%d (log-only; grounding unchanged)",
            intent.intent_id, result.status.value, scope.primary, list(scope.secondary),
            len(primary_scoped), _grounded_template_count(cs))
    except Exception:   # shadow must never break generation — log and swallow
        logger.exception(
            "intent-recognition shadow failed (swallowed; grounding unaffected) for intent_id=%s",
            intent.intent_id)


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
    return {"name": f.name, "derives_from": f.derives_from, "aggregation": f.aggregation,
            "grain_table": f.grain_table,   # keep grain — it disambiguates same-named options
            "verification": f.verification,   # honest §14.5 stamp surfaced at Gate #1 (item 4)
            "critic_note": f.critic_note,     # advisory residual critic note — the human weighs it
            "rationale": f.rationale,         # §14.2 one-line causal 'why' — audit the logic first
            "derives_pairs": [list(p) for p in f.derives_pairs]}   # for server-side reconstruction


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
        derives_pairs=tuple(tuple(p) for p in d.get("derives_pairs", [])))


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

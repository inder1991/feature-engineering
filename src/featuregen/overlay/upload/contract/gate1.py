"""Phase 2 — Gate #1 bridge.

Runs the DISCOVERY loop from the redacted hypothesis into a *considered set* — the anchor (the
requester's definition, grounded + gauntlet-validated) alongside generated alternatives (also each
gauntlet-validated) plus an advisory recommendation — then records the human's confirmed choice
(who + why + the full considered set). This is the human-validation gate: **no contract is authored
without a recorded choice here**, in both definition and hypothesis-only modes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from featuregen.contracts.identity import identity_to_jsonb
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.contract.intake import Intent
from featuregen.overlay.upload.feature_assist import (
    FeatureIdea,
    FeatureSet,
    SetRecommendation,
    recommend_feature_sets,
    recommend_features,
    recommend_set,
)


class Gate1Error(Exception):
    """A malformed or out-of-set Gate #1 confirmation."""


@dataclass(frozen=True, slots=True)
class ConsideredSet:
    intent_id: str
    anchor: FeatureIdea | None                    # the requester's definition, validated (definition mode)
    alternatives: list[FeatureSet]                # generated, each fully gauntlet-validated
    recommendation: SetRecommendation | None      # advisory — fit vs hypothesis, not a performance claim


def persist_intent(conn, intent: Intent) -> None:
    """Durably record the intent — the mandatory hypothesis is the feature's premise (M6). Idempotent."""
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, definition, intake_mode, "
        "redacted_hypothesis, redacted_definition, actor) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (intent_id) DO NOTHING",
        (intent.intent_id, intent.hypothesis, intent.definition, intent.intake_mode,
         intent.redacted_hypothesis, intent.redacted_definition, _actor_json(intent.actor)))


def build_considered_set(conn, intent: Intent, client: LLMClient, *, entity: str | None = None,
                         catalog_source: str | None = None, roles=(), target_ref: str | None = None,
                         now=None) -> ConsideredSet:
    """Discovery loop → validated alternatives; the anchor is the requester's definition run through the
    same validated loop (definition mode only). Every option shown to the human has passed the gauntlet.
    Persists the intent (M6) — the hypothesis is durably recorded when the flow reaches Gate #1."""
    persist_intent(conn, intent)
    alternatives = recommend_feature_sets(
        conn, intent.redacted_hypothesis, client, entity=entity, catalog_source=catalog_source,
        roles=roles, target_ref=target_ref, now=now)
    anchor: FeatureIdea | None = None
    if intent.intake_mode == "definition":
        ideas = recommend_features(
            conn, intent.redacted_definition, client, entity=entity, catalog_source=catalog_source,
            roles=roles, target_ref=target_ref, now=now, target=1)
        anchor = ideas[0] if ideas else None
    recommendation = (recommend_set(conn, alternatives, intent.redacted_hypothesis, client)
                      if any(s.features for s in alternatives) else None)
    return ConsideredSet(intent.intent_id, anchor, alternatives, recommendation)


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
            "grain_table": f.grain_table}   # keep grain — it disambiguates same-named options


def _snapshot(cs: ConsideredSet) -> dict:
    return {
        "anchor": _idea_json(cs.anchor),
        "alternatives": [{"lens": s.lens, "features": [_idea_json(f) for f in s.features]}
                         for s in cs.alternatives],
        "recommendation": None if cs.recommendation is None else {
            "recommended_lens": cs.recommendation.recommended_lens,
            "reasoning": cs.recommendation.reasoning, "caveat": cs.recommendation.caveat},
    }


def _actor_json(actor) -> str | None:
    if actor is None:
        return None                            # -> SQL NULL ("unknown actor"), not the string "None"
    if isinstance(actor, str):
        return json.dumps(actor)
    try:
        return json.dumps(identity_to_jsonb(actor))
    except Exception:
        return json.dumps({"repr": str(actor)})   # structured, parseable JSON — not a repr string


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
         _actor_json(actor), json.dumps(_snapshot(considered))))
    return chosen_option_id

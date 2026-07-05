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

from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract.intake import Intent
from featuregen.overlay.upload.feature_assist import (
    FeatureIdea,
    FeatureSet,
    SetRecommendation,
    recommend_feature_sets,
    recommend_features,
    recommend_set,
    set_signals,
)


class Gate1Error(Exception):
    """A malformed or out-of-set Gate #1 confirmation."""


@dataclass(frozen=True, slots=True)
class ConsideredSet:
    intent_id: str
    anchor: FeatureIdea | None                    # the requester's definition, validated (definition mode)
    alternatives: list[FeatureSet]                # generated, each fully gauntlet-validated
    recommendation: SetRecommendation | None      # advisory — fit vs hypothesis, not a performance claim


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


def build_considered_set(conn, intent: Intent, client: LLMClient, *, entity: str | None = None,
                         catalog_source: str | None = None, roles=(), target_ref: str | None = None,
                         now=None) -> ConsideredSet:
    """Discovery loop → validated alternatives; the anchor is the requester's definition run through the
    same validated loop (definition mode only). Every option shown to the human has passed the gauntlet.
    Persists the intent + target_ref (M6, BLOCKER 2) and the considered-set snapshot (BLOCKER 1) when the
    flow reaches Gate #1."""
    persist_intent(conn, intent, target_ref)
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
    cs = ConsideredSet(intent.intent_id, anchor, alternatives, recommendation)
    conn.execute(   # persist the validated set so /contract/draft reconstructs the chosen feature here
        "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (intent_id) DO UPDATE SET considered = EXCLUDED.considered",
        (intent.intent_id, json.dumps(_snapshot(conn, cs))))
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
    return {"name": f.name, "derives_from": f.derives_from, "aggregation": f.aggregation,
            "grain_table": f.grain_table,   # keep grain — it disambiguates same-named options
            "verification": f.verification,   # honest §14.5 stamp surfaced at Gate #1 (item 4)
            "critic_note": f.critic_note,     # advisory residual critic note — the human weighs it
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
    for s in snap.get("alternatives", []):
        for f in s.get("features", []):
            if f.get("name") == chosen_option_id:
                return _idea_from_json(f)
    return None


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

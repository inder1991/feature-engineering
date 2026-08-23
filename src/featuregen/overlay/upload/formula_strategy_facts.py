"""The FACTS ASSEMBLER for formula-strategy resolution — one durable read plus pure registry probes.

**What this is, and what it is not.** ``resolve_formula_strategy`` is pure and decides; THIS module
reads. It answers, for one (considered_revision, option): where did the idea come from, what kind of
computation is it, does a reviewed registry entry cover it and at which generation, did the engine's
binding succeed — all from durable rows and pure registries, never from a request body.

▲ **The authority order is deliberate.** `semantic_option_decision` (the append-only per-option
decision row) is the server-assigned record and wins when present. An option with NO decision row —
pre-A1b revisions, non-semantic options — falls back to the FROZEN idea's own provenance fields,
which is honest absence handled explicitly rather than a KeyError three calls later.

▲ **Vocabulary is normalized HERE, once.** The decision row spells ``user_defined``; the planning
contract and migration 1104's CHECK spell ``user_definition``; the legacy free-form default is
``llm_freeform``. Three spellings crossing module boundaries is how a CHECK constraint refuses a row
two layers above the code that chose the word.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.overlay.upload.formula_strategy import FormulaStrategyFactsV1

__all__ = [
    "DETERMINISTIC_AUTHORING_AVAILABLE",
    "AssembledStrategyFactsV1",
    "assemble_strategy_facts",
    "current_author_contract_hash",
]

#: ▲ THE DETERMINISTIC LANE'S EXECUTION POSTURE — ON, since the correction that earned it. The
#: premise that turned it off was wrong: grounding contexts ARE persisted, per candidate key, into
#: `considered_json["recipe_grounding_context_by_candidate_key"]` at generation — so the worker
#: binds against the SAME bytes the serving run bound with, and the loader is
#: `formula_draft_worker._frozen_grounding_context`. Bindability remains PER CANDIDATE below: a
#: legacy revision frozen before the engine pass, or an ambiguous candidate key, has no frozen
#: context and routes to the LLM with `REVIEWED_LANE_UNAVAILABLE` recorded — a history gap, never
#: dressed as `REVIEWED_BLUEPRINT_NOT_EXECUTABLE`, which the WORKER reserves for a blueprint that
#: genuinely fails to bind.
DETERMINISTIC_AUTHORING_AVAILABLE = True

#: decision-row / legacy spellings -> the planning-contract vocabulary 1104's CHECK enforces.
_ORIGIN_BY_SOURCE = {
    "recipe": "recipe",
    "llm_intent": "llm_intent",
    "user_defined": "user_definition",
    "user_definition": "user_definition",
    #: The legacy free-form default. An LLM-proposed idea with no recipe is an LLM intent.
    "llm_freeform": "llm_intent",
}


@dataclass(frozen=True, slots=True)
class AssembledStrategyFactsV1:
    """The resolver's input plus the PLAN-ROW material the resolver itself does not need."""

    facts: FormulaStrategyFactsV1
    #: For `formula_draft_authoring_plan` when the strategy is REVIEWED — the derived blueprint's
    #: own identity, taken at assembly so the worker can verify nothing moved underneath it.
    reviewed_blueprint_revision: str | None
    reviewed_blueprint_hash: str | None


def current_author_contract_hash() -> str:
    """The FROZEN author contract this deployment would send — where prompt identity actually lives.

    Computed from the same freeze the evaluation contract uses, so the identity a draft is minted
    under and the identity an evaluation certifies are one value. ▲ Deployment-constant by
    construction (settings + prompts + schemas), so calling it per request re-derives the same hash
    — the cost is hashing, not IO.
    """
    from featuregen.formula.audited import current_formula_generation_settings
    from featuregen.formula.frozen_configuration import freeze_current_configuration_v2
    from featuregen.overlay.upload.recipe_formula_evaluation_contract import (
        FORMULA_WIRE_SCHEMA_VERSION_V3,
    )

    frozen = freeze_current_configuration_v2(
        generation_settings=current_formula_generation_settings(),
        formula_schema_version=FORMULA_WIRE_SCHEMA_VERSION_V3)
    return frozen.author.contract_hash


def _origin_and_kind(conn, *, considered_revision_id: str, option_id: str, idea: Any):
    """(origin, computation_kind, recipe_id, recipe_revision_hash, binding_state) — decision row
    first, frozen idea as the honest fallback."""
    from featuregen.overlay.upload.semantic_option_decision import load_frozen_option_facts

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=considered_revision_id, option_id=option_id)
    if frozen is not None:
        origin = _ORIGIN_BY_SOURCE.get(frozen.generation_source, "llm_intent")
        recipe_id = frozen.source_definition_id if frozen.generation_source == "recipe" else None
        return (origin, frozen.computation_kind, recipe_id,
                frozen.recipe_revision_hash or None, frozen.binding_state)

    # No decision row. The frozen idea's own provenance is all there is — and `recipe_id` is set
    # ONLY for recipe origin (`semantic_projection._served_idea`), so it is the discriminator.
    source = getattr(idea, "generation_source", "llm_freeform") or "llm_freeform"
    origin = _ORIGIN_BY_SOURCE.get(source, "llm_intent")
    recipe_id = getattr(idea, "recipe_id", None) if origin == "recipe" else None
    # A free-form or intent idea headed for a formula IS a deterministic-formula candidate — the
    # KIND describes what would be computed, not who thought of it. Conceptual/model kinds only
    # arrive through the decision row, which is the layer that classifies them.
    return origin, "deterministic_formula", recipe_id, None, "bound"


def _frozen_context_present(conn, *, considered_revision_id: str, option_id: str, idea) -> bool:
    """Whether the SERVING run froze a grounding context for this option — the request-time half of
    the same read the worker's loader performs, so the two can never disagree about availability."""
    from featuregen.overlay.upload.contract.gate1 import _revision_recipe_candidate_key

    row = conn.execute(
        "SELECT considered_json FROM contract_considered_revision "
        "WHERE considered_revision_id = %s", (considered_revision_id,)).fetchone()
    considered = row[0] if row and isinstance(row[0], dict) else {}
    key = _revision_recipe_candidate_key(considered, option_id=option_id, feature=idea)
    return bool(key) and isinstance(
        (considered.get("recipe_grounding_context_by_candidate_key") or {}).get(key), dict)


def assemble_strategy_facts(
    conn, *, considered_revision_id: str, option_id: str, idea: Any,
    catalog_snapshot_hash: str, binding_plan_hash: str | None = None,
    method_override_revision_id: str | None = None,
) -> AssembledStrategyFactsV1:
    """Everything `resolve_formula_strategy` needs, read once and normalized."""
    from featuregen.overlay.upload.recipe_formula_blueprint_derivation import (
        BlueprintDerivationRefusal,
        derive_blueprint_v2,
    )
    from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
        expectation_content_hash_v2,
    )
    from featuregen.overlay.upload.recipe_formula_expectations import (
        RECIPE_FORMULA_EXPECTATIONS,
    )
    from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
        RECIPE_FORMULA_V2_EXPECTATIONS,
    )
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    origin, kind, recipe_id, recipe_revision_hash, binding_state = _origin_and_kind(
        conn, considered_revision_id=considered_revision_id, option_id=option_id, idea=idea)

    expectation_ref: str | None = None
    generation: str | None = None
    reviewed_current = False
    derivable = False
    blueprint_revision: str | None = None
    blueprint_hash: str | None = None

    definition = v2_recipe_by_id(recipe_id) if recipe_id else None
    if definition is not None and definition.formula is not None:
        expectation_ref = definition.formula.expectation_ref
        schema = definition.formula.formula_schema_version
        generation = ("v2" if schema == "formula-v2"
                      else "v1" if schema == "formula-v1" else None)
        # ▲ THE RIGHT REGISTRY PER GENERATION, never the union. `has_reviewed_expectation` probes
        # ONE string against a recipe-id-keyed dict AND a ref-keyed dict — membership cannot say
        # which side matched, and two of the three reviewed recipes are Formula V1, so the union
        # alone would launder V1 review into V3 execution authority.
        if generation == "v2":
            reviewed_current = expectation_ref in RECIPE_FORMULA_V2_EXPECTATIONS
        elif generation == "v1":
            reviewed_current = recipe_id in RECIPE_FORMULA_EXPECTATIONS

        derived = derive_blueprint_v2(definition)
        if not isinstance(derived, BlueprintDerivationRefusal):
            derivable = True
            blueprint_revision = derived.recipe_id
            blueprint_hash = expectation_content_hash_v2(derived)

    facts = FormulaStrategyFactsV1(
        candidate_origin=origin,
        computation_kind=kind,
        recipe_id=recipe_id,
        recipe_revision_hash=recipe_revision_hash,
        expectation_ref=expectation_ref,
        expectation_generation=generation,
        reviewed_expectation_current=reviewed_current,
        blueprint_derivable=derivable,
        # ▲ The ENGINE's frozen verdict AND a frozen context to re-bind with. Both are durable
        # facts about THIS candidate; either absent means the worker could not execute the
        # deterministic lane for it, and the resolver records why rather than discovering it later.
        blueprint_bindable=(
            binding_state == "bound"
            and derivable
            and _frozen_context_present(
                conn, considered_revision_id=considered_revision_id, option_id=option_id,
                idea=idea)),
        deterministic_lane_available=DETERMINISTIC_AUTHORING_AVAILABLE,
        reviewed_blueprint_revision=blueprint_revision,
        reviewed_blueprint_hash=blueprint_hash,
        provider_contract_hash=None,   # filled by the caller for the LLM strategy
        catalog_snapshot_hash=catalog_snapshot_hash,
        binding_plan_hash=binding_plan_hash,
        considered_revision_id=considered_revision_id,
        option_id=option_id,
        method_override_revision_id=method_override_revision_id,
    )
    return AssembledStrategyFactsV1(
        facts=facts, reviewed_blueprint_revision=blueprint_revision,
        reviewed_blueprint_hash=blueprint_hash)

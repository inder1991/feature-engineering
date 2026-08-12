"""SE-6 — abstract intent generation: the model proposes MEANING; columns are never its call.

The generation seam for `FeatureIntentV1`: one audited structured call whose input is the
BOUNDED semantic capability inventory (controlled vocabulary only — concepts with column
counts, entities, operation classes, the confirmed objectives, the OFFERED model specs; no
object refs, no table names, no prose dumps) and whose output items are each re-parsed through
the STRICT intent parser independently — a malformed sibling never fails the batch, a physical
key is a NAMED refusal, an out-of-scope objective or an un-offered model spec is rejected
before anything binds. Provenance is OURS: whatever the model writes in
``generation_provenance`` is overwritten with the call's real prompt/schema/model/call refs.

Cost discipline: nothing in the platform calls this yet. In `semantic_v1` it REPLACES the
physical-column generation call (no net new spend); a sampled shadow comparison is a deliberate
operator action, never a default — the plan's own rule and the standing spend-approval rule.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.feature_intent import FeatureIntentV1, parse_feature_intent
from featuregen.overlay.upload.feature_planning_contracts import PlanningContractError
from featuregen.overlay.upload.generation_semantic_context import GenerationSemanticContextV1
from featuregen.overlay.upload.recipe_contract_v2 import (
    RESULT_CLASS_ADDITIVITY,
    RecipeContractError,
)

FEATURE_INTENT_TASK = "overlay.feature.intents"
FEATURE_INTENT_PROMPT_ID = "feature_intents"
FEATURE_INTENT_PROMPT_VERSION = 1
FEATURE_INTENT_SCHEMA_ID = "feature_intents"

#: Inventory bounds — the inventory must stay smaller than the per-column prose it replaces.
_MAX_INVENTORY_CONCEPTS = 200

_INSTRUCTION = (
    "Propose feature INTENTS for the analyst's hypothesis: what should be computed, never "
    "which physical data computes it. Use ONLY the controlled vocabulary in "
    "capability_inventory — concepts for operands, entities for grains, the closed "
    "operation_class set for deterministic computations, and objectives from the confirmed "
    "list. Each intent is ONE atomic output. NEVER name tables, columns, SQL, or policy ids — "
    "any physical reference is rejected. If the closed operation vocabulary cannot express an "
    "idea, emit it as computation_kind=conceptual_pattern with an honest conceptual_reason "
    "instead of forcing a misleading aggregation. A governed_model_output must reference one "
    "of the OFFERED model_feature_refs; never invent one.")

#: Rejection codes — per ITEM, so the batch survives its worst member.
INTENT_REJECTED_PARSE = "INTENT_REJECTED_PARSE"
INTENT_OBJECTIVE_OUT_OF_SCOPE = "INTENT_OBJECTIVE_OUT_OF_SCOPE"
INTENT_MODEL_SPEC_NOT_OFFERED = "INTENT_MODEL_SPEC_NOT_OFFERED"
INTENT_GENERATION_UNAVAILABLE = "INTENT_GENERATION_UNAVAILABLE"


def semantic_capability_inventory(context: GenerationSemanticContextV1, *,
                                  scope_leaves, model_feature_refs=()) -> dict:
    """The bounded, physically-blind inventory the model plans against.

    Controlled tokens only: concept names with their column COUNTS (a count is capability
    evidence; a ref would be a physical leak), the entities present, the closed operation
    classes, the confirmed objectives, and the offered model specs. Truncation is counted,
    never silent."""
    from featuregen.overlay.upload.taxonomy.dimensions import known_entities

    concept_counts = sorted(
        ((name, len(refs)) for name, refs in context.concept_index.items()),
        key=lambda item: (-item[1], item[0]))
    truncated = max(0, len(concept_counts) - _MAX_INVENTORY_CONCEPTS)
    # Entities intersect the PLATFORM's governed entity vocabulary — graph entity strings are
    # uploader-supplied, and the inventory's egress class is "structural/closed", so an
    # uploader-invented label never rides it.
    governed = set(known_entities())
    entities = sorted({c.entity for c in context.columns
                       if c.entity and c.entity in governed}
                      | {c.entity.lower() for c in context.columns
                         if c.entity and c.entity.lower() in governed})
    return {
        "concepts": [{"concept": name, "columns": count}
                     for name, count in concept_counts[:_MAX_INVENTORY_CONCEPTS]],
        "concepts_truncated": truncated,
        "entities": entities,
        "has_event_timestamps": any(
            c.concept == "event_timestamp" or (c.data_type or "").startswith("timestamp")
            for c in context.columns),
        "has_as_of_columns": any(c.is_as_of for c in context.columns),
        "operation_classes": sorted(RESULT_CLASS_ADDITIVITY),
        "objectives": sorted(scope_leaves),
        "model_feature_refs": sorted(model_feature_refs),
    }


@dataclass(frozen=True, slots=True)
class IntentGenerationResult:
    intents: tuple[FeatureIntentV1, ...]
    rejections: tuple[dict, ...]          # {index, code, detail} — per item, never batch-fatal
    llm_call_ref: str | None
    provider_calls: int


def generate_feature_intents(conn, client, *, context: GenerationSemanticContextV1,
                             scope_leaves, redacted_hypothesis: str,
                             model_feature_refs=(), actor=None) -> IntentGenerationResult:
    """One audited structured call → validated intents + per-item rejections."""
    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    inventory = semantic_capability_inventory(
        context, scope_leaves=scope_leaves, model_feature_refs=model_feature_refs)
    call = drive_audited_structured_call(
        conn, client, task=FEATURE_INTENT_TASK,
        prompt_id=f"{FEATURE_INTENT_PROMPT_ID}_v{FEATURE_INTENT_PROMPT_VERSION}",
        schema_id=FEATURE_INTENT_SCHEMA_ID,
        catalog_metadata={"objective": redacted_hypothesis,
                          "capability_inventory": inventory},
        instruction=_INSTRUCTION, actor=actor, record_egress_block=True)
    if call.output is None:
        return IntentGenerationResult(
            intents=(), rejections=({"index": -1, "code": INTENT_GENERATION_UNAVAILABLE,
                                     "detail": "the intent call produced no validated output"},),
            llm_call_ref=call.llm_call_ref, provider_calls=call.provider_calls)

    provenance = {
        "prompt_ref": f"{FEATURE_INTENT_PROMPT_ID}_v{FEATURE_INTENT_PROMPT_VERSION}",
        "output_schema_version": f"{FEATURE_INTENT_SCHEMA_ID}@1",
        "model": getattr(client, "model", None) or "unknown",
        "call_ref": call.llm_call_ref or "unrecorded",
        "confirmed_scope_hash": context.context_hash(),
    }
    offered_specs = set(model_feature_refs)
    scope = set(scope_leaves)
    intents: list[FeatureIntentV1] = []
    rejections: list[dict] = []
    for index, item in enumerate(call.output.get("intents", [])):
        doc = {**item, "generation_provenance": dict(provenance)}   # OURS, always — overwrite
        try:
            intent = parse_feature_intent(doc)
        # FeatureIntentError + nested operand/output/temporal validation share the
        # PlanningContractError base; the typed spec constructors (OutputSpecV2 et al) raise
        # RecipeContractError. BOTH are per-item facts about ONE proposed intent — a malformed
        # unit_kind must reject that item, never kill the batch.
        except (PlanningContractError, RecipeContractError) as error:
                                                 # output validators' refusals — one shared base

            rejections.append({"index": index, "code": INTENT_REJECTED_PARSE,
                               "detail": str(error)})
            continue
        if intent.primary_objective not in scope or any(
                objective not in scope for objective in intent.supporting_objectives):
            rejections.append({"index": index, "code": INTENT_OBJECTIVE_OUT_OF_SCOPE,
                               "detail": f"{intent.primary_objective!r} is outside the "
                                         "human-confirmed scope"})
            continue
        if (intent.computation_kind == "governed_model_output"
                and intent.model_feature_ref not in offered_specs):
            rejections.append({"index": index, "code": INTENT_MODEL_SPEC_NOT_OFFERED,
                               "detail": f"model spec {intent.model_feature_ref!r} was not "
                                         "offered — the model may not invent one"})
            continue
        intents.append(intent)
    return IntentGenerationResult(
        intents=tuple(intents), rejections=tuple(rejections),
        llm_call_ref=call.llm_call_ref, provider_calls=call.provider_calls)


__all__ = ["FEATURE_INTENT_SCHEMA_ID", "FEATURE_INTENT_TASK",
           "INTENT_GENERATION_UNAVAILABLE", "INTENT_MODEL_SPEC_NOT_OFFERED",
           "INTENT_OBJECTIVE_OUT_OF_SCOPE", "INTENT_REJECTED_PARSE",
           "IntentGenerationResult", "generate_feature_intents",
           "semantic_capability_inventory"]

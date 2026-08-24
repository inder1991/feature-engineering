"""SE-6 — abstract intent generation: the model proposes MEANING; columns are never its call.

The generation seam for `FeatureIntentV1`: one audited structured call whose input is the
BOUNDED semantic capability inventory (controlled vocabulary only — concepts with column
counts, entities, operation classes, the confirmed objectives, the OFFERED model specs; no
object refs, no table names, no prose dumps) and whose output items are each re-parsed through
the STRICT intent parser independently — a malformed sibling never fails the batch, a physical
key is a NAMED refusal, an out-of-scope objective or an un-offered model spec is rejected
before anything binds. Provenance is OURS: whatever the model writes in
``generation_provenance`` is overwritten with the call's real prompt/schema/model/call refs.

The vocabulary contract (T1): the schema this seam dispatches PROMISES exactly what the parser
enforces. v1 did not — seven closed vocabularies rode as bare strings — and on 2026-08-24 the
provider answered `unit_kind: days` eight times over and every item was refused. v2 publishes the
contract's own tuples on the wire; the repairs below recover the shapes already recorded; and a
value the vocabulary genuinely lacks refuses as a NAMED gap, because extending a governed
vocabulary is an owner's decision and an anonymous parse error hides that it is even needed.

Cost discipline: since the E4 cutover this REPLACES the physical-column generation call rather
than adding to it — that call no longer exists — so the engine's intent round is no net new
spend, and any comparison run is a deliberate operator action under the standing spend rule.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.overlay.upload.feature_intent import FeatureIntentV1, parse_feature_intent
from featuregen.overlay.upload.feature_planning_contracts import PlanningContractError
from featuregen.overlay.upload.generation_semantic_context import GenerationSemanticContextV1
from featuregen.overlay.upload.recipe_contract_v2 import (
    OPERAND_CLASSES,
    OUTPUT_TYPES,
    RESULT_CLASS_ADDITIVITY,
    TEMPORAL_ANCHOR_KINDS,
    UNIT_KINDS,
    WINDOW_UNITS,
    RecipeContractError,
)

logger = logging.getLogger(__name__)

FEATURE_INTENT_TASK = "overlay.feature.intents"
FEATURE_INTENT_PROMPT_ID = "feature_intents"
FEATURE_INTENT_PROMPT_VERSION = 1
FEATURE_INTENT_SCHEMA_ID = "feature_intents"
#: v2 — the version whose WIRE schema publishes the closed vocabularies this module then enforces
#: (enrich_llm `_feature_intents_with_closed_vocabularies`). v1 stays registered and byte-frozen:
#: it is the contract every recorded call egressed under, and the stamp must not relabel history.
FEATURE_INTENT_SCHEMA_VERSION = 2

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
#: NOT a parse failure. The answer was well-formed and the model meant something real — the
#: governed vocabulary has no entry for it. Only an owner may add one, so this outcome names the
#: missing entry instead of filing the item under "malformed" where nobody can act on it.
INTENT_VOCABULARY_GAP = "INTENT_VOCABULARY_GAP"

#: Mis-spellings the 2026-08-24 AML run actually returned, mapped to the entry the governed
#: vocabulary already has for them. A BELT, not the fix: `feature_intents` v2 puts these
#: vocabularies on the wire, so a compliant provider cannot spell them again. This recovers the
#: shapes already recorded, and any answer from a provider that ignores the wire vocabulary.
#: Nothing speculative rides here — every key is a rejection from that run's audit, and each maps
#: to the ONE governed member that can express what the model wrote.
_UNIT_KIND_SPELLINGS = {
    "days": "duration_days",          # a relationship tenure, in days
    "count_rate": "rate",             # transactions per unit of time
}
#: A compound spelling whose head IS the governed token, and no other member expresses that
#: anchor: an as-of snapshot can only be `as_of`, an event window can only be `event`. The
#: recorded blocks corroborate their own repair — their `window_basis` reads `as_of_date` and
#: `event_timestamp` respectively.
_ANCHOR_KIND_SPELLINGS = {"as_of_snapshot": "as_of", "event_window": "event"}
#: Singular for plural. The unit is a COUNT of days either way (289 of 317 governed recipes).
_WINDOW_UNIT_SPELLINGS = {"day": "days"}


#: How many DISTINCT CONCEPTS a group's unanimity must span before it may determine a class.
#: Three, because one concept is a single authoring decision (however many recipes reuse it) and
#: two can still be one pack authored in one sitting — three is the smallest number that cannot be
#: either. Measured against the reviewed registry today: `flag` clears it (8 concepts across 11
#: recipes); `currency` (2), `eligibility` (1), `crypto` (1), `geographic` (1) and `label` (1) do
#: not, and unanimity over one operand is one data point wearing the word.
_GROUP_SUPPORT_FLOOR = 3


def _determined_group_classes() -> dict[str, str]:
    """Concept GROUP → the ONE operand class its reviewed usage determines, for groups whose
    agreement is wide enough to mean something.

    Derived from the reviewed V2 registry — the same provenance rule `concept_operand_classes` is
    built on (the registry's authored usage is the reviewed truth; a hand-typed parallel table
    would drift). Two conditions, both required: every reviewed use agrees on one class, AND the
    agreement spans at least `_GROUP_SUPPORT_FLOOR` distinct concepts.

    Imported inside the function on purpose: `recipe_registry_v2` builds 317 recipes at import,
    and this module is imported by the generation path whether or not a repair is ever needed."""
    global _GROUP_CLASSES
    if _GROUP_CLASSES is None:
        from featuregen.overlay.upload.concepts import concept as registered_concept
        from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

        classes: dict[str, set[str]] = {}
        concepts: dict[str, set[str]] = {}
        for recipe in V2_RECIPES:
            for operand in recipe.operands:
                registered = registered_concept(operand.concept)
                if registered is None:     # unregistered: no group, so it supports no claim
                    continue
                group = registered.group or ""
                classes.setdefault(group, set()).add(operand.operand_class)
                concepts.setdefault(group, set()).add(operand.concept)
        _GROUP_CLASSES = {
            group: next(iter(seen)) for group, seen in classes.items()
            if group and len(seen) == 1 and len(concepts[group]) >= _GROUP_SUPPORT_FLOOR}
    return _GROUP_CLASSES


_GROUP_CLASSES: dict[str, str] | None = None


def _resolve_operand_class(concept: str, grain_entity: str) -> tuple[str, str] | None:
    """The operand class the GOVERNED REGISTRY determines for this concept, with its reason — or
    None when the registry does not determine one.

    The model wrote a word from the wrong vocabulary (`identifier`, `temporal`, `attribute` — the
    concept GROUP names, not the operand classes). Translating that word would be a guess:
    `attribute` could be a dimension, a status or a policy input, and the platform has no
    `attribute` class to translate it INTO. So the spelling is not translated at all — it is only
    the signal that this field needs resolving, and the class is read from what the platform
    already knows about the CONCEPT:

    * the governed concept→class map allows exactly one class (`event_timestamp`);
    * the concept's registered ``pit_role`` is ``as_of``/``event`` — the two timestamp classes
      exist to carry precisely that distinction (`as_of_date` 88 of 90 reviewed uses,
      `event_timestamp` 180 of 180), and it settles concepts the map leaves ambiguous
      (`origination_date` is used both ways; its pit_role is not);
    * the concept is a registered IDENTIFIER (it has a namespace) of the output grain's own
      entity — an identifier of the population being computed is that population's key;
    * every reviewed use of the concept's GROUP agrees on one class, over enough distinct
      concepts to mean it (`flag` → `status`; see `_determined_group_classes`).

    That last rung is the weakest and it is the one that fires where the concept's OWN registry
    presence is zero — `pep_flag` and `new_to_bank_flag` are registered concepts that no reviewed
    recipe has ever used as an operand, so nothing more specific than their group can speak for
    them. It carries a support floor for exactly that reason.

    An UNREGISTERED concept determines nothing and returns None here — never an exception. The
    model that invents an operand-class word is the same model that invents concept names, so
    this path is ordinary traffic, and a raise would take the whole batch down with one item."""
    from featuregen.overlay.upload.concept_operand_classes import allowed_operand_classes
    from featuregen.overlay.upload.concepts import concept as registered_concept

    allowed = allowed_operand_classes(concept)
    if allowed and len(allowed) == 1:
        return allowed[0], "the only class the governed concept map allows"
    registered = registered_concept(concept)
    if registered is None:             # `concept()` answers None for an unknown name
        return None
    if registered.pit_role == "as_of":
        return "as_of_timestamp", "the concept's registered as-of point-in-time role"
    if registered.pit_role == "event":
        return "event_timestamp", "the concept's registered event point-in-time role"
    if registered.namespace and registered.entity_link and grain_entity \
            and registered.entity_link.lower() == grain_entity.lower():
        return "entity_key", "a registered identifier of the output grain's own entity"
    determined = _determined_group_classes().get(registered.group or "")
    if determined is not None:
        return determined, f"the only class any reviewed {registered.group} concept serves"
    return None


def _normalize_intent_vocabulary(doc: Mapping[str, Any]) -> tuple[dict, tuple[dict, ...]]:
    """Repair the recorded mis-spellings BEFORE the closed-vocabulary checks, and say what changed.

    Three walls, all from the same run, all the same defect — a well-formed answer written in a
    vocabulary the parser does not hold:

    * the OUTPUT block: a governed SPELLING of a unit that exists (``days`` → ``duration_days``),
      and a member of one closed vocabulary written into the OTHER's field — ``unit_kind:
      boolean`` (3 intents) and ``output_type: count`` (1). The swapped pair is repairable only
      because the corpus is unanimous about the missing half: all 25 boolean-typed governed
      outputs declare ``unit_kind="count"``, and 74 of 77 non-boolean count outputs declare
      ``output_type="integer"``. Applied ONLY where the receiving field is absent or already
      agrees, so a value the model actually meant is never overwritten;
    * the TEMPORAL block: compound anchor spellings and a singular window unit;
    * each OPERAND's class, resolved from the governed registry (see `_resolve_operand_class`),
      never translated from the word the model used.

    What is deliberately NOT repaired is as load-bearing as what is. ``unit_kind:
    monetary_change`` looks one substitution from ``monetary`` — but the recorded block carries no
    ``currency_policy``, so the substitution was executed and produces ``a monetary output
    requires a currency policy``: it converts a NAMED gap an owner can act on into an anonymous
    refusal. A repair that only moves where the failure lands is not a repair.

    Returns the rewritten document and one entry per application — ``{field, from, to, reason}``,
    the per-item dict shape this module's rejections already use — because a served intent that
    differs from what the model wrote must be able to say where."""
    fixed = dict(doc)
    applied: list[dict] = []

    def _apply(block: dict, path: str, field: str, value: str, reason: str) -> None:
        applied.append({"field": f"{path}{field}", "from": block.get(field, ""), "to": value,
                        "reason": reason})
        block[field] = value

    output = dict(doc["output"]) if isinstance(doc.get("output"), Mapping) else None
    if output is not None:
        fixed["output"] = output
        unit, output_type = output.get("unit_kind"), output.get("output_type")
        if isinstance(unit, str) and unit in _UNIT_KIND_SPELLINGS:
            _apply(output, "output.", "unit_kind", _UNIT_KIND_SPELLINGS[unit],
                   "the governed spelling of this unit")
        elif unit == "boolean" and output_type in (None, "", "boolean"):
            if output_type != "boolean":
                _apply(output, "output.", "output_type", "boolean",
                       "the output type, written into the unit's field")
            _apply(output, "output.", "unit_kind", "count",
                   "the unit every governed boolean output declares")
        if output_type == "count" and output.get("unit_kind") in (None, "", "count"):
            if output.get("unit_kind") != "count":
                _apply(output, "output.", "unit_kind", "count",
                       "the unit kind, written into the output type's field")
            _apply(output, "output.", "output_type", "integer",
                   "the output type every governed count declares")

    if isinstance(doc.get("temporal"), Mapping):
        temporal = dict(doc["temporal"])
        fixed["temporal"] = temporal
        anchor, window_unit = temporal.get("anchor_kind"), temporal.get("window_unit")
        if isinstance(anchor, str) and anchor in _ANCHOR_KIND_SPELLINGS:
            _apply(temporal, "temporal.", "anchor_kind", _ANCHOR_KIND_SPELLINGS[anchor],
                   "the governed token for this anchor")
        if isinstance(window_unit, str) and window_unit in _WINDOW_UNIT_SPELLINGS:
            _apply(temporal, "temporal.", "window_unit", _WINDOW_UNIT_SPELLINGS[window_unit],
                   "the governed spelling of this window unit")

    if isinstance(doc.get("operands"), list):
        grain_entity = doc.get("output_grain_entity") or ""
        operands = []
        for index, item in enumerate(doc["operands"]):
            if not isinstance(item, Mapping):
                operands.append(item)
                continue
            operand = dict(item)
            declared = operand.get("operand_class")
            if isinstance(declared, str) and declared not in OPERAND_CLASSES:
                resolved = _resolve_operand_class(str(operand.get("concept", "")),
                                                  str(grain_entity))
                if resolved is not None:
                    _apply(operand, f"operands[{index}].", "operand_class", *resolved)
            operands.append(operand)
        fixed["operands"] = operands
    return fixed, tuple(applied)


def _vocabulary_gaps(doc: Mapping[str, Any]) -> str:
    """The refusal for values the governed vocabulary does not HAVE — or "" when there are none.

    ``output_type='categorical'`` is not a malformed answer: an ordinal rating IS a real feature
    shape, and OUTPUT_TYPES has no entry for it. Saying so — naming the entry, calling it a gap —
    is the difference between something an owner can decide and an anonymous parse rejection.
    EVERY missing entry is named, across all three blocks, so one field's gap never hides
    another's and an owner sees the whole bill for one intent at once."""
    missing: list[str] = []
    output = doc.get("output") if isinstance(doc.get("output"), Mapping) else {}
    temporal = doc.get("temporal") if isinstance(doc.get("temporal"), Mapping) else {}
    for block, field, vocabulary in (
            (output, "output_type", OUTPUT_TYPES), (output, "unit_kind", UNIT_KINDS),
            (temporal, "anchor_kind", TEMPORAL_ANCHOR_KINDS),
            (temporal, "window_unit", WINDOW_UNITS)):
        value = block.get(field)
        if isinstance(value, str) and value and value not in vocabulary:
            missing.append(f"{field} {value!r} has no entry in {vocabulary}")
    if isinstance(doc.get("operands"), list):
        for item in doc["operands"]:
            if not isinstance(item, Mapping):
                continue
            declared = item.get("operand_class")
            if isinstance(declared, str) and declared and declared not in OPERAND_CLASSES:
                missing.append(
                    f"operand {item.get('role', '?')!r} declares operand_class {declared!r}, "
                    f"which has no entry in {OPERAND_CLASSES} and which the governed registry "
                    f"does not determine for concept {item.get('concept', '?')!r}")
    if not missing:
        return ""
    return ("vocabulary gap — " + "; ".join(missing) + ". Extending a governed vocabulary is an "
            "owner decision, not a parse repair.")


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
    #: The rejection-FREE half of the same per-item trace: every vocabulary repair applied to an
    #: intent that went on to be accepted — {index, field, from, to, reason}. A repair nobody can
    #: see is a silent edit of the model's answer, which is the thing this seam exists to prevent.
    normalizations: tuple[dict, ...] = ()


def generate_feature_intents(conn, client, *, context: GenerationSemanticContextV1,
                             scope_leaves, redacted_hypothesis: str,
                             model_feature_refs=(), actor=None,
                             confirmed_scope_hash: str = "") -> IntentGenerationResult:
    """One audited structured call → validated intents + per-item rejections."""
    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    inventory = semantic_capability_inventory(
        context, scope_leaves=scope_leaves, model_feature_refs=model_feature_refs)
    call = drive_audited_structured_call(
        conn, client, task=FEATURE_INTENT_TASK,
        prompt_id=f"{FEATURE_INTENT_PROMPT_ID}_v{FEATURE_INTENT_PROMPT_VERSION}",
        schema_id=FEATURE_INTENT_SCHEMA_ID, schema_version=FEATURE_INTENT_SCHEMA_VERSION,
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
        "output_schema_version": f"{FEATURE_INTENT_SCHEMA_ID}@{FEATURE_INTENT_SCHEMA_VERSION}",
        "model": getattr(client, "model", None) or "unknown",
        "call_ref": call.llm_call_ref or "unrecorded",
        # B3 (GEN-04 closed): the scope hash is the SCOPE's hash — two human scopes over one
        # catalog are two identities. The catalog context hash is its own provenance key.
        "confirmed_scope_hash": confirmed_scope_hash,
        "semantic_context_hash": context.context_hash(),
    }
    offered_specs = set(model_feature_refs)
    scope = set(scope_leaves)
    intents: list[FeatureIntentV1] = []
    rejections: list[dict] = []
    normalizations: list[dict] = []
    for index, item in enumerate(call.output.get("intents", [])):
        doc = {**item, "generation_provenance": dict(provenance)}   # OURS, always — overwrite
        # T1: repair the recorded mis-spellings, then separate a MISSING VOCABULARY ENTRY from a
        # malformed answer — both BEFORE the parser closes the vocabularies, because after it
        # every one of them reads as the same anonymous rejection.
        doc, applied = _normalize_intent_vocabulary(doc)
        for entry in applied:
            # The trace on the result is for callers; this line is for the operator reading logs
            # while a run is live. Both exist because a repair nobody can see is a silent edit.
            logger.info("intent vocabulary repair: item=%d %s %r → %r (%s)",
                        index, entry["field"], entry["from"], entry["to"], entry["reason"])
        gap = _vocabulary_gaps(doc)
        if gap:
            rejections.append({"index": index, "code": INTENT_VOCABULARY_GAP, "detail": gap})
            continue
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
        # C8: model prose naming PHYSICAL catalog objects is a per-item parse failure — the
        # inventory it saw was physically blind, so the name is invented or leaked.
        from featuregen.overlay.upload.feature_intent import prose_physical_references

        physical = prose_physical_references(
            (intent.display_name, intent.business_definition, intent.rationale,
             intent.conceptual_reason), context.columns)
        if physical:
            rejections.append({"index": index, "code": INTENT_REJECTED_PARSE,
                               "detail": "model prose names physical objects: "
                                         + ", ".join(physical)})
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
        normalizations.extend({"index": index, **entry} for entry in applied)
        intents.append(intent)
    return IntentGenerationResult(
        intents=tuple(intents), rejections=tuple(rejections),
        llm_call_ref=call.llm_call_ref, provider_calls=call.provider_calls,
        normalizations=tuple(normalizations))


__all__ = ["FEATURE_INTENT_SCHEMA_ID", "FEATURE_INTENT_SCHEMA_VERSION", "FEATURE_INTENT_TASK",
           "INTENT_GENERATION_UNAVAILABLE", "INTENT_MODEL_SPEC_NOT_OFFERED",
           "INTENT_OBJECTIVE_OUT_OF_SCOPE", "INTENT_REJECTED_PARSE", "INTENT_VOCABULARY_GAP",
           "IntentGenerationResult", "generate_feature_intents",
           "semantic_capability_inventory"]

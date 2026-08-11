"""SE-3 — the capability compiler: Layer B, one typed statement of what a column may contribute.

Compiled ONLY for shortlist members (the concept-closure rule bounds Layer B by roles ×
candidates, never catalog width), from the FROZEN Layer-A context plus ONE batched
field-evidence read for authority pins. The axes, each honest about its source:

* **structural** — declared type and its family, grain/as-of markers: connector-attested
  facts, derivable safely ("varchar is non-numeric"); never a banking meaning parsed from
  prose or a column name.
* **semantic** — the controlled concept WITH its evidence authority (``producer/strength``
  from active field evidence; ``graph_hint`` when only the display value exists), the
  registry's identifier namespace, and the governed operand-class possibilities from
  :mod:`concept_operand_classes` — an unmapped concept yields ``()`` and a NAMED marker,
  never a guess.
* **operational** — additivity/currency display values with their evidence authority where
  evidence exists.
* **absent axes are FACTS** — dataset profiles, relationship state and use-policy posture are
  not compiled yet (they arrive with SE-8's planners); each absence is a ``missing_context``
  marker so no consumer can mistake "not compiled" for "known clear".

Prose (definition / AI summary / semantic terms) rides ``retrieval_text`` ONLY — retrieval and
display material, never an input to any capability decision (plan invariant 10).
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.concept_operand_classes import (
    OPERAND_CLASS_MAP_VERSION,
    allowed_operand_classes,
)
from featuregen.overlay.upload.generation_semantic_context import (
    ColumnIndexV1,
    GenerationSemanticContextV1,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.recipe_operand_policy import _type_family

#: The evidence fields whose authority pins ride the capability (one batched read).
_PINNED_FIELDS = ("concept", "entity", "additivity", "currency")

#: The honest absences of this compiler version — axes SE-8 compiles later.
_ABSENT_AXES = ("dataset_profile_absent", "relationship_state_absent", "use_policy_absent")


@dataclass(frozen=True, slots=True)
class ColumnCapabilityV1:
    """One shortlisted column's typed capability statement."""

    object_ref: str
    table: str
    column: str
    # structural
    declared_type: str | None
    type_family: str                      # numeric | temporal | text | boolean | other | unknown
    is_grain: bool
    is_as_of: bool
    # semantic
    concept: str | None
    concept_authority: str                # "producer/strength" | "graph_hint" | "absent"
    identifier_namespace: str | None
    identifier_like: bool
    possible_operand_classes: tuple[str, ...]
    operand_class_map_version: str
    # operational (display value + its evidence authority; "absent" = no active evidence)
    entity: str | None
    entity_authority: str
    additivity: str | None
    additivity_authority: str
    currency: str | None
    currency_authority: str
    # honesty
    missing_context: tuple[str, ...]
    # retrieval-only prose — NEVER a capability input
    retrieval_text: str


def _authority(pins: dict[tuple[str, str], str], logical_ref: str, field: str,
               display_value) -> str:
    pinned = pins.get((logical_ref, field))
    if pinned is not None:
        return pinned
    return "graph_hint" if display_value not in (None, "") else "absent"


def compile_capabilities(conn, context: GenerationSemanticContextV1,
                         object_refs) -> dict[str, ColumnCapabilityV1]:
    """Compile capabilities for the requested shortlist members — ONE query regardless of how
    many refs are asked for; everything else comes from the frozen context and pure registries.
    An unknown ref (absent from the frozen universe) is silently absent from the result: the
    context IS the visibility authority, and this compiler cannot widen it."""
    wanted = [ref for ref in dict.fromkeys(object_refs)]
    by_ref: dict[str, ColumnIndexV1] = {c.object_ref: c for c in context.columns}
    members = [(ref, by_ref[ref]) for ref in wanted if ref in by_ref]

    logical_by_ref = {
        ref: normalize_ref(context.catalog_source, col.schema_name, col.table, col.column)
        for ref, col in members}
    pins: dict[tuple[str, str], str] = {}
    if members:
        rows = conn.execute(
            "SELECT logical_ref, field_name, producer, strength "
            "FROM field_evidence "
            "WHERE lifecycle = 'active' AND field_name = ANY(%s) AND logical_ref = ANY(%s) "
            "ORDER BY created_at, evidence_id",
            (list(_PINNED_FIELDS), list(logical_by_ref.values()))).fetchall()
        for logical_ref, field_name, producer, strength in rows:
            pins[(logical_ref, field_name)] = f"{producer}/{strength}"   # newest active wins

    from featuregen.overlay.upload.concepts import concept as registered_concept

    capabilities: dict[str, ColumnCapabilityV1] = {}
    for ref, col in members:
        logical_ref = logical_by_ref[ref]
        namespace = None
        if col.concept:
            try:
                registered = registered_concept(col.concept)
            except Exception:
                registered = None
            namespace = registered.namespace if registered is not None else None
        classes = allowed_operand_classes(col.concept) if col.concept else None
        missing = list(_ABSENT_AXES)
        if col.concept and classes is None:
            missing.append("concept_not_in_operand_class_map")
        capabilities[ref] = ColumnCapabilityV1(
            object_ref=ref, table=col.table, column=col.column,
            declared_type=col.data_type, type_family=_type_family(col.data_type),
            is_grain=col.is_grain, is_as_of=col.is_as_of,
            concept=col.concept,
            concept_authority=_authority(pins, logical_ref, "concept", col.concept),
            identifier_namespace=namespace,
            identifier_like=namespace is not None,
            possible_operand_classes=classes or (),
            operand_class_map_version=OPERAND_CLASS_MAP_VERSION,
            entity=col.entity,
            entity_authority=_authority(pins, logical_ref, "entity", col.entity),
            additivity=col.additivity,
            additivity_authority=_authority(pins, logical_ref, "additivity", col.additivity),
            currency=col.currency,
            currency_authority=_authority(pins, logical_ref, "currency", col.currency),
            missing_context=tuple(missing),
            retrieval_text=" ".join(filter(None, (
                col.definition, col.ai_summary, col.semantic_terms))))
    return capabilities


__all__ = ["ColumnCapabilityV1", "compile_capabilities"]

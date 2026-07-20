"""Explicit v1/v2 FeatureIdea response serializers (spec §8).

The assist routes must NOT return the shared FeatureIdea dataclass directly — any new field would
silently leak into the flag-OFF response (and, via the same shape, break the pre-Slice-3 contract).
v1 emits EXACTLY the pre-Slice-3 field set/order so a flag-OFF response is byte-identical; v2 adds the
Slice-3 fields. The flag is captured ONCE at the route and passed in as `feature_context`."""
from __future__ import annotations

from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement


def _pair(p: tuple[str, str] | None) -> list[str] | None:
    return list(p) if p is not None else None


def _req(r: Requirement) -> dict:
    return {"code": r.code, "operand": list(r.operand), "detail": r.detail}


def serialize_feature_idea_v1(idea: FeatureIdea) -> dict:
    """The pre-Slice-3 shape, in the dataclass field order FastAPI's jsonable_encoder produced.
    The Slice-3 fields are NEVER emitted — flag-OFF byte-identity depends on this."""
    return {
        "name": idea.name,
        "description": idea.description,
        "derives_from": list(idea.derives_from),
        "aggregation": idea.aggregation,
        "grain_table": idea.grain_table,
        "derives_pairs": [list(p) for p in idea.derives_pairs],
        "verification": idea.verification,
        "critic_note": idea.critic_note,
        "rationale": idea.rationale,
    }


def serialize_feature_idea_v2(idea: FeatureIdea) -> dict:
    """v1 plus the Slice-3 typed-computation + tri-state fields, plus the H1a carry-through metadata.
    The H1a fields are emitted ONLY when non-default so a plain idea's v2 (and the flag-OFF v1) stay
    byte-identical; a recipe / governed / user-anchor idea surfaces its server-assigned labels."""
    out = serialize_feature_idea_v1(idea)
    out["operation_kind"] = idea.operation_kind
    out["measure_refs"] = [list(m) for m in idea.measure_refs]
    out["grain_ref"] = _pair(idea.grain_ref)
    out["time_ref"] = _pair(idea.time_ref)
    out["window"] = idea.window
    out["grouping_refs"] = [list(g) for g in idea.grouping_refs]
    out["validation_status"] = idea.validation_status
    out["requirements"] = [_req(r) for r in idea.requirements]
    # ── H1a carry-through metadata — only-when-non-default (see docstring) ──
    if idea.generation_source != "llm_freeform":
        out["generation_source"] = idea.generation_source
    if idea.recipe_id is not None:
        out["recipe_id"] = idea.recipe_id
    if idea.candidate_status:
        out["candidate_status"] = idea.candidate_status
    if idea.input_role_bindings:
        out["input_role_bindings"] = [b.to_json() for b in idea.input_role_bindings]
    if idea.external_requirement_previews:
        out["external_requirement_previews"] = [p.to_json() for p in idea.external_requirement_previews]
    if idea.metadata_snapshot_id is not None:
        out["metadata_snapshot_id"] = idea.metadata_snapshot_id
    if idea.metadata_input_fingerprint is not None:
        out["metadata_input_fingerprint"] = idea.metadata_input_fingerprint
    if idea.binding_fact_keys:
        out["binding_fact_keys"] = list(idea.binding_fact_keys)
    if idea.planner_applicability != "not_applicable_nonrecipe":
        out["planner_applicability"] = idea.planner_applicability
    if idea.physical_plan_id is not None:
        out["physical_plan_id"] = idea.physical_plan_id
    if idea.planner_declaration_id is not None:
        out["planner_declaration_id"] = idea.planner_declaration_id
    return out


def serialize_feature_idea(idea: FeatureIdea, *, feature_context: bool) -> dict:
    return serialize_feature_idea_v2(idea) if feature_context else serialize_feature_idea_v1(idea)

"""Immutable, server-private recipe grounding context.

The public feature candidate intentionally remains small. This module preserves the recipe definition,
semantic parameter choice, and exact per-role column bindings needed for deterministic replay and later
formula-authority checks without exposing governance provenance through the API response.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

from featuregen.overlay.upload.templates import (
    GroundedFeature,
    GroundedNeedBinding,
    Need,
    SourceEntityRoleResolution,
    Template,
    resolve_source_entity_need_role,
)

CANONICALIZATION_VERSION = "recipe-grounding-v1"
_TEMPLATE_FIELDS = frozenset(field.name for field in fields(Template))
_NEED_FIELDS = frozenset(field.name for field in fields(Need))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _scalar(value: Any) -> None | bool | int | str:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise ValueError(f"unsupported semantic parameter type: {type(value).__name__}")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise ValueError(f"unsupported canonical recipe value: {type(value).__name__}")


def canonical_need(need: Need) -> dict[str, Any]:
    return {field: _json_value(getattr(need, field)) for field in sorted(_NEED_FIELDS)}


def canonical_template(template: Template) -> dict[str, Any]:
    body = {
        field: _json_value(getattr(template, field))
        for field in sorted(_TEMPLATE_FIELDS)
        if field != "needs"
    }
    body["needs"] = [canonical_need(need) for need in template.needs]
    return {"version": "canonical-recipe-v1", "template": body}


def semantic_parameters(template: Template, bound: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    if set(bound) != set(template.params):
        raise ValueError(f"semantic parameter set differs from recipe {template.id!r}")
    result: list[tuple[str, Any]] = []
    for key in sorted(bound):
        value = _scalar(bound[key])
        if value not in template.params[key]:
            raise ValueError(f"semantic parameter {key!r} is not authored by recipe {template.id!r}")
        result.append((key, value))
    return tuple(result)


def semantic_parameter_hash(recipe_id: str, bindings: tuple[tuple[str, Any], ...]) -> str:
    return content_hash({
        "version": "recipe-params-v1",
        "recipe_id": recipe_id,
        "bindings": [[key, value] for key, value in bindings],
    })


def _binding_json(binding: GroundedNeedBinding) -> dict[str, Any]:
    return {
        "role": binding.role,
        "catalog_source": binding.catalog_source,
        "logical_ref": binding.logical_ref,
        "graph_object_ref": binding.graph_object_ref,
        "expected_concept": binding.expected_concept,
        "optional": binding.optional,
        "join_role": binding.join_role,
        "temporal_role": binding.temporal_role,
        "distinct_binding_group": binding.distinct_binding_group,
        "binding_resolution": binding.binding_resolution.value,
        "tied_candidate_logical_refs": list(binding.tied_candidate_logical_refs),
        "tied_candidate_set_hash": binding.tied_candidate_set_hash,
    }


@dataclass(frozen=True, slots=True)
class RecipeGroundingContextV1:
    recipe_candidate_key: str
    recipe_id: str
    source_entity_need_role: str | None
    source_entity_role_resolution: SourceEntityRoleResolution
    need_bindings: tuple[GroundedNeedBinding, ...]
    semantic_parameters: tuple[tuple[str, Any], ...]
    semantic_parameter_binding_hash: str
    template_definition: dict[str, Any]
    template_content_hash: str
    canonicalization_version: str = CANONICALIZATION_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "recipe_candidate_key": self.recipe_candidate_key,
            "recipe_id": self.recipe_id,
            "source_entity_need_role": self.source_entity_need_role,
            "source_entity_role_resolution": self.source_entity_role_resolution.value,
            "need_bindings": [_binding_json(binding) for binding in self.need_bindings],
            "semantic_parameters": [[key, value] for key, value in self.semantic_parameters],
            "semantic_parameter_binding_hash": self.semantic_parameter_binding_hash,
            "template_definition": self.template_definition,
            "template_content_hash": self.template_content_hash,
            "canonicalization_version": self.canonicalization_version,
        }


def build_recipe_grounding_context(
    template: Template,
    feature: GroundedFeature,
) -> RecipeGroundingContextV1:
    definition = canonical_template(template)
    template_hash = content_hash(definition)
    parameters = semantic_parameters(template, feature.params)
    parameter_hash = semantic_parameter_hash(template.id, parameters)
    source_role = resolve_source_entity_need_role(template)
    resolution_hash = content_hash([
        [
            binding.role,
            binding.binding_resolution.value,
            binding.tied_candidate_set_hash,
        ]
        for binding in feature.role_bindings
    ])
    candidate_key = content_hash({
        "version": "recipe-candidate-v1",
        "generation_source": "recipe",
        "recipe_id": template.id,
        "template_content_hash": template_hash,
        "semantic_parameter_binding_hash": parameter_hash,
        "aggregation": feature.aggregation,
        "ordered_bindings": [
            [binding.role, binding.logical_ref] for binding in feature.role_bindings
        ],
        "binding_resolution_hash": resolution_hash,
    })
    return RecipeGroundingContextV1(
        recipe_candidate_key=candidate_key,
        recipe_id=template.id,
        source_entity_need_role=source_role.role,
        source_entity_role_resolution=source_role.resolution,
        need_bindings=feature.role_bindings,
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=parameter_hash,
        template_definition=definition,
        template_content_hash=template_hash,
    )


# ── BR-2: canonical-recipe-v2, BESIDE v1 — never replacing it ────────────────────────────────────
# canonical-recipe-v1 (everything above) is preserved byte-for-byte for old contexts; V2
# definitions get their own field-exhaustive canonical form and hash. Generic over dataclasses so
# adding a field to any V2 type is AUTOMATICALLY hash-bearing — the exhaustiveness test proves it.
CANONICAL_RECIPE_V2_VERSION = "canonical-recipe-v2"


def _canonical_dataclass(value: Any) -> Any:
    from dataclasses import fields as _fields
    from dataclasses import is_dataclass
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical_dataclass(getattr(value, f.name))
                for f in sorted(_fields(value), key=lambda f: f.name)}
    if isinstance(value, tuple):
        return [_canonical_dataclass(item) for item in value]
    return _json_value(value)


def canonical_recipe_v2(definition: Any) -> dict[str, Any]:
    """The versioned, field-exhaustive serialization of a RecipeDefinitionV2 (or any nested V2
    spec). Every field of every nested dataclass is hash-bearing by construction."""
    return {"version": CANONICAL_RECIPE_V2_VERSION,
            "definition": _canonical_dataclass(definition)}


def canonical_recipe_v2_hash(definition: Any) -> str:
    return content_hash(canonical_recipe_v2(definition))


def assert_canonical_recipe_exhaustive() -> None:
    """Import/test seam: every behavior-bearing dataclass field is serialized."""
    template_keys = set(canonical_template(_EXHAUSTIVENESS_TEMPLATE)["template"])
    if template_keys != _TEMPLATE_FIELDS:
        raise AssertionError(f"canonical Template fields differ: {template_keys ^ _TEMPLATE_FIELDS}")
    need_keys = set(canonical_need(_EXHAUSTIVENESS_TEMPLATE.needs[0]))
    if need_keys != _NEED_FIELDS:
        raise AssertionError(f"canonical Need fields differ: {need_keys ^ _NEED_FIELDS}")


_EXHAUSTIVENESS_TEMPLATE = Template(
    id="_canonical_probe",
    family="_probe",
    intent="_probe",
    needs=(Need(role="_probe", concept="customer_id"),),
    params={},
    aggregation="_probe",
    additivity="n/a",
    explain="L",
    use_cases=(),
    pit="",
)

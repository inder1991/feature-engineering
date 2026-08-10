"""BR-3 — bounded variant selection and the corrected candidate identity (banking-recipe plan).

The PULL half of parameter choice, extending the shipped push half (router Task 4b): the
hypothesis picks parameters through `param_choice.choose_params`; a USER picks them here — one
validated selection at a time, against the authored `ParameterSpecV2` tuples, with the same
no-override default (first allowed value) both halves share. Nothing ever enumerates the
Cartesian product at request time: a page shows ONE resolved default variant plus the bounded
parameter SCHEMA, and an explicit read-only request resolves exactly one selection.

The corrected identity closes the 145-recipe collision class for the V2 era: every bound
parameter, the recipe REVISION (canonical-recipe-v2 hash) and the output_id are identity-bearing,
and every parameter renders into the display through its mandatory authored projection — a
semantic choice structurally cannot hide inside a generic label. Legacy v1/v2 suggestion
identities are untouched; the corrected identity is emitted only through contract v3 (BR-8).

Selection discipline mirrors the platform's everywhere-rule: closed selection, never generation —
an unknown parameter or an off-menu value is REFUSED; a governed-policy parameter selects its
reviewed policy reference, never a free literal from the browser.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.overlay.upload.recipe_contract_v2 import (
    ParameterSpecV2,
    RecipeDefinitionV2,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    canonical_recipe_v2_hash,
    content_hash,
)

VARIANT_IDENTITY_VERSION = "recipe-variant-v1"


class VariantSelectionError(ValueError):
    """A selection this recipe does not offer — refused, never coerced."""


@dataclass(frozen=True, slots=True)
class ResolvedRecipeVariantV1:
    """One recipe at one complete, validated parameter binding. ``variant_identity`` is the
    deterministic identity contract v3 emits; ``display_name`` renders the output meaning plus
    EVERY parameter through its authored projection — 30-day and 90-day instances of one output
    are two identities and two labels, never one card."""

    recipe_id: str
    recipe_revision_hash: str
    output_id: str
    selection: tuple[tuple[str, object], ...]      # (name, value), authored order
    identity_tokens: tuple[str, ...]               # each parameter's identity projection, rendered
    variant_identity: str
    display_name: str
    is_default: bool


def parameter_schema(definition: RecipeDefinitionV2) -> dict:
    """The bounded schema a page returns INSTEAD of grounding every combination: allowed values
    (or the governed policy reference), class and projections per parameter. Work is proportional
    to parameter count, never to the combination count."""
    return {
        "recipe_id": definition.recipe_id,
        "recipe_revision_hash": canonical_recipe_v2_hash(definition),
        "output_id": definition.output.output_id,
        "parameters": [
            {
                "name": spec.name,
                "parameter_class": spec.parameter_class,
                "allowed_values": list(spec.allowed_values),
                "governed_policy_ref": spec.governed_policy_ref,
                "display_projection": spec.display_projection,
            }
            for spec in definition.parameters
        ],
    }


def _validated_value(spec: ParameterSpecV2, value: object) -> object:
    if spec.parameter_class == "governed_policy":
        # A governed-policy parameter selects its reviewed policy reference — a browser cannot
        # smuggle a free threshold through it. Authored alternatives, when present, are policy
        # labels too.
        allowed = spec.allowed_values or (spec.governed_policy_ref,)
        for candidate in allowed:
            if str(candidate) == str(value):
                return candidate
        raise VariantSelectionError(
            f"parameter {spec.name!r} selects a governed policy from {list(allowed)}; "
            f"{value!r} is not one")
    for candidate in spec.allowed_values:
        if str(candidate) == str(value):
            return candidate
    raise VariantSelectionError(
        f"value {value!r} is not in parameter {spec.name!r}'s authored tuple "
        f"{list(spec.allowed_values)}")


def _default_value(spec: ParameterSpecV2) -> object:
    if spec.parameter_class == "governed_policy":
        return spec.allowed_values[0] if spec.allowed_values else spec.governed_policy_ref
    return spec.allowed_values[0]


def resolve_variant(definition: RecipeDefinitionV2,
                    overrides: Mapping[str, object] | None = None) -> ResolvedRecipeVariantV1:
    """Resolve exactly ONE variant: the reviewed default (no overrides — the same first-allowed
    rule the push half degrades to) or an explicit validated selection. Unknown parameter names
    are refused, off-menu values are refused, and the returned value is always the AUTHORED
    object, matched by string form — the model/browser never supplies the value itself."""
    overrides = dict(overrides or {})
    declared = {spec.name for spec in definition.parameters}
    unknown = set(overrides) - declared
    if unknown:
        raise VariantSelectionError(
            f"unknown parameter(s) {sorted(unknown)} for recipe {definition.recipe_id!r}")

    selection: list[tuple[str, object]] = []
    identity_tokens: list[str] = []
    display_tokens: list[str] = []
    for spec in definition.parameters:
        if spec.name in overrides:
            value = _validated_value(spec, overrides[spec.name])
        else:
            value = _default_value(spec)
        selection.append((spec.name, value))
        identity_tokens.append(spec.identity_projection.replace("{value}", str(value)))
        display_tokens.append(spec.display_projection.replace("{value}", str(value)))

    revision_hash = canonical_recipe_v2_hash(definition)
    identity = variant_identity(
        recipe_id=definition.recipe_id, recipe_revision_hash=revision_hash,
        output_id=definition.output.output_id, selection=tuple(selection))
    display = definition.output.display_label
    if display_tokens:
        display = f"{display} ({', '.join(display_tokens)})"
    return ResolvedRecipeVariantV1(
        recipe_id=definition.recipe_id,
        recipe_revision_hash=revision_hash,
        output_id=definition.output.output_id,
        selection=tuple(selection),
        identity_tokens=tuple(identity_tokens),
        variant_identity=identity,
        display_name=display,
        is_default=not overrides)


def variant_identity(*, recipe_id: str, recipe_revision_hash: str, output_id: str,
                     selection: tuple[tuple[str, object], ...]) -> str:
    """The corrected canonical identity: recipe REVISION, output and EVERY bound parameter are
    identity-bearing. Sorted on parameter name — a selection is a set, its authoring order is
    presentation."""
    return content_hash({
        "version": VARIANT_IDENTITY_VERSION,
        "recipe_id": recipe_id,
        "recipe_revision_hash": recipe_revision_hash,
        "output_id": output_id,
        "selection": sorted([name, str(value)] for name, value in selection),
    })


def enumerate_variant_identities(definition: RecipeDefinitionV2,
                                 combination_cap: int = 4096) -> tuple[str, ...]:
    """AUDIT-ONLY enumeration (the BR-1 collision check): every combination's identity, so the
    audit can prove they are all distinct. Request paths never call this — the cap is a runaway
    guard for the offline check, not a request bound."""
    from itertools import product as _product

    axes = []
    for spec in definition.parameters:
        if spec.parameter_class == "governed_policy":
            axes.append(tuple(spec.allowed_values) or (spec.governed_policy_ref,))
        else:
            axes.append(tuple(spec.allowed_values))
    combos = list(_product(*axes)) if axes else [()]
    if len(combos) > combination_cap:
        raise VariantSelectionError(
            f"{definition.recipe_id!r} declares {len(combos)} combinations — over the audit cap "
            f"({combination_cap}); an authored space this size is itself a defect")
    names = [spec.name for spec in definition.parameters]
    revision_hash = canonical_recipe_v2_hash(definition)
    return tuple(
        variant_identity(recipe_id=definition.recipe_id, recipe_revision_hash=revision_hash,
                         output_id=definition.output.output_id,
                         selection=tuple(zip(names, combo, strict=True)))
        for combo in combos)

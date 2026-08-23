"""The governed variant identity model — canonical id and variant identity are SEPARATE fields.

Why this exists (binding context): the persisted ``semantic_option_decision.source_definition_id``
is consumed as the CANONICAL registry id — ``assemble_current_activation_state`` calls
``v2_recipe_by_id(frozen.source_definition_id)`` and
``_formula_schema_supported(frozen.source_definition_id)`` (semantic_option_decision.py:524, :604)
— so variant identity must be SEPARATE fields, never a mangled id. The planner's
``physical_plan_id`` is a truncated 16-hex display id whose material excludes parameters; full
request identity lives in ``planning_request_hash`` (field-exhaustive incl. ``parameter_values``).

Consequences for consumers:

* Read the registry with ``canonical_definition_id`` — verbatim. ``governed_variant_id`` is an
  opaque digest: nothing can be parsed back out of it, by design.
* Two parameter variants of one recipe differ in ``planning_request_hash`` and agree on
  ``canonical_definition_id``; they are two variants of ONE governed definition, never two
  definitions.
* ``physical_plan_content_hash`` is the FULL hash — ``contract_input_hash`` when the plan is
  compiled, else a sha256 over the plan's full id material. The truncated ``physical_plan_id``
  is a display id and must not be substituted for it.

This module is PURE: stdlib only. Importing it must not reach the planner, the DB layer, or the
registries, so identity can be constructed anywhere in the stack.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# The closed pair of origins a governed definition can come from.
DEFINITION_ORIGIN_RECIPE_V2 = "recipe_v2"
DEFINITION_ORIGIN_LLM_INTENT = "llm_intent"
DEFINITION_ORIGINS: tuple[str, ...] = (DEFINITION_ORIGIN_RECIPE_V2, DEFINITION_ORIGIN_LLM_INTENT)


@dataclass(frozen=True, slots=True)
class GovernedVariantIdentityV1:
    canonical_definition_id: str
    definition_origin: str                 # recipe_v2 | llm_intent
    planning_request_hash: str             # full
    physical_plan_content_hash: str        # full: contract_input_hash when compiled,
                                           # else sha256 over the plan's full id material
    parameter_binding_hash: str = ""       # where the engine minted one
    plan_envelope_version: str = "1"

    def __post_init__(self) -> None:
        if self.definition_origin not in DEFINITION_ORIGINS:
            raise ValueError(
                "definition_origin must be one of "
                f"{DEFINITION_ORIGINS!r}, got {self.definition_origin!r}")
        for field in ("canonical_definition_id", "planning_request_hash",
                      "physical_plan_content_hash"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} is mandatory and may not be blank")

    @property
    def governed_variant_id(self) -> str:
        material = "|".join((self.canonical_definition_id, self.definition_origin,
                             self.planning_request_hash, self.physical_plan_content_hash,
                             self.parameter_binding_hash, self.plan_envelope_version))
        return "gvar_" + hashlib.sha256(material.encode()).hexdigest()


__all__ = [
    "DEFINITION_ORIGINS",
    "DEFINITION_ORIGIN_LLM_INTENT",
    "DEFINITION_ORIGIN_RECIPE_V2",
    "GovernedVariantIdentityV1",
]

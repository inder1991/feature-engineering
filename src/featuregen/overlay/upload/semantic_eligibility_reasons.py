"""SE-4 — the closed eligibility reason vocabulary, grouped into the platform's honest families.

Every code maps to exactly one product FAMILY — the four ways the platform already talks about
an unmet condition, so a refusal is never a shrug: ``undecided`` (nobody with authority decided
yet — a confirmation clears it), ``needs_data_check`` (a runtime observation would settle it),
``structurally_unsuitable`` (no decision or observation can fix it — the binding is wrong), and
``needs_setup`` (governance/config work is the remedy: a policy, a relationship, a mapping).

Two codes are REUSED from the binder's vocabulary (same strings, one meaning):
``IDENTIFIER_NOT_A_MEASURE`` and ``TYPE_INCOMPATIBLE`` from ``recipe_operand_policy``.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_operand_policy import (
    IDENTIFIER_NOT_A_MEASURE,
    TYPE_INCOMPATIBLE,
)

# safety (the legacy _safe_to_bind law, folded — never bindable, whatever the authority)
TARGET_LEAKAGE_BLOCKED = "TARGET_LEAKAGE_BLOCKED"
# meaning
CONCEPT_MISMATCH = "CONCEPT_MISMATCH"
OPERAND_CLASS_MISMATCH = "OPERAND_CLASS_MISMATCH"
ECONOMIC_ROLE_UNPROVEN = "ECONOMIC_ROLE_UNPROVEN"          # same string the binder uses
BUSINESS_EVENT_MISMATCH = "BUSINESS_EVENT_MISMATCH"
# authority
SEMANTIC_AUTHORITY_INSUFFICIENT = "SEMANTIC_AUTHORITY_INSUFFICIENT"
SEMANTIC_CONFLICT = "SEMANTIC_CONFLICT"
PROPOSED_METADATA_ONLY = "PROPOSED_METADATA_ONLY"
# shape
SOURCE_GRAIN_MISMATCH = "SOURCE_GRAIN_MISMATCH"
SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW = "SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW"
# type / value
ADDITIVITY_INCOMPATIBLE = "ADDITIVITY_INCOMPATIBLE"
UNIT_INCOMPATIBLE = "UNIT_INCOMPATIBLE"
CURRENCY_POLICY_MISSING = "CURRENCY_POLICY_MISSING"
# time
EVENT_TIME_REQUIRED = "EVENT_TIME_REQUIRED"
AS_OF_TIME_REQUIRED = "AS_OF_TIME_REQUIRED"
KNOWLEDGE_TIME_REQUIRED = "KNOWLEDGE_TIME_REQUIRED"
TEMPORAL_POLICY_UNRESOLVED = "TEMPORAL_POLICY_UNRESOLVED"
# relationship
RELATIONSHIP_REQUIRED = "RELATIONSHIP_REQUIRED"
POPULATION_DATASET_UNDECLARED = "POPULATION_DATASET_UNDECLARED"
DIRECTIONAL_CARDINALITY_UNPROVEN = "DIRECTIONAL_CARDINALITY_UNPROVEN"
JOIN_PATH_DENIED = "JOIN_PATH_DENIED"
# governance
PERSONAL_DATA_POLICY_REQUIRED = "PERSONAL_DATA_POLICY_REQUIRED"
PROTECTED_CHARACTERISTIC_BLOCKED = "PROTECTED_CHARACTERISTIC_BLOCKED"
STATUS_POLICY_UNRESOLVED = "STATUS_POLICY_UNRESOLVED"
# S4: the resolver RAN and nothing is current for the family this reference falls in. Distinct from
# STATUS_POLICY_UNRESOLVED, which is the blanket "no resolver serves this kind at all" — telling an
# operator the blanket answer when a resolver exists and found nothing hides which of the two
# remedies applies (build the resolver vs publish a realization).
POLICY_REFERENCE_UNRESOLVABLE = "POLICY_REFERENCE_UNRESOLVABLE"
# S8: this build's renderer has no branch for an operator the feature's graph contains. Not a
# governance question and not a data question — the platform cannot emit it, and no decision or
# observation changes that for this build. Kept apart from FORMULA_SCHEMA_UNSUPPORTED, which is
# about the WIRE VERSION rather than about what the renderer can emit.
RENDERER_CANNOT_DISPATCH = "RENDERER_CANNOT_DISPATCH"
# ambiguity
REQUIRED_OPERAND_AMBIGUOUS = "REQUIRED_OPERAND_AMBIGUOUS"
SOURCE_SELECTION_AMBIGUOUS = "SOURCE_SELECTION_AMBIGUOUS"
# external runtime checks (SE-9's typed gauntlet emits these as REQUIREMENTS, never refusals:
# a check nobody has run yet is honest outstanding work, not a defect of the candidate)
IDENTIFIER_UNIQUENESS = "IDENTIFIER_UNIQUENESS"
EVENT_HISTORY_VERIFICATION = "EVENT_HISTORY_VERIFICATION"

# ── Activation-policy codes (remediation A1): candidate-level blockers on the action ladder ─────
BINDING_NOT_BOUND = "BINDING_NOT_BOUND"
BINDING_PLAN_DIVERGENCE = "BINDING_PLAN_DIVERGENCE"
UOA_MISMATCH = "UOA_MISMATCH"
RECIPE_REVIEW_NOT_CURRENT = "RECIPE_REVIEW_NOT_CURRENT"
CONCEPTUAL_PATTERN_NOT_AUTHORABLE = "CONCEPTUAL_PATTERN_NOT_AUTHORABLE"
PHYSICAL_PLAN_MISSING = "PHYSICAL_PLAN_MISSING"
FORMULA_NOT_REVIEWED = "FORMULA_NOT_REVIEWED"
FORMULA_SCHEMA_UNSUPPORTED = "FORMULA_SCHEMA_UNSUPPORTED"
READINESS_NOT_MATERIALIZATION_READY = "READINESS_NOT_MATERIALIZATION_READY"
EXTERNAL_VALIDATION_OUTSTANDING = "EXTERNAL_VALIDATION_OUTSTANDING"
EXECUTION_AUTHORITY_UNEVALUATED = "EXECUTION_AUTHORITY_UNEVALUATED"
EXECUTION_AUTHORITY_UNMET = "EXECUTION_AUTHORITY_UNMET"
SNAPSHOT_STALE_REGENERATE = "SNAPSHOT_STALE_REGENERATE"
OUTPUT_POLICY_INCOMPLETE = "OUTPUT_POLICY_INCOMPLETE"      # C3: a load-bearing output policy unauthored
POLICY_FAMILY_UNVERIFIABLE = "POLICY_FAMILY_UNVERIFIABLE"  # C5: a family's facts axis is absent
HISTORY_DEPTH_INSUFFICIENT = "HISTORY_DEPTH_INSUFFICIENT"  # C9: window exceeds DECLARED depth
ACTIVATION_STATE_DRIFTED = "ACTIVATION_STATE_DRIFTED"

#: code -> product family. A code absent from this table cannot ship — the pin test enforces.
REASON_FAMILIES: dict[str, str] = {
    TARGET_LEAKAGE_BLOCKED: "structurally_unsuitable",
    CONCEPT_MISMATCH: "structurally_unsuitable",
    OPERAND_CLASS_MISMATCH: "structurally_unsuitable",
    IDENTIFIER_NOT_A_MEASURE: "structurally_unsuitable",
    TYPE_INCOMPATIBLE: "structurally_unsuitable",
    BUSINESS_EVENT_MISMATCH: "structurally_unsuitable",
    SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW: "structurally_unsuitable",
    SOURCE_GRAIN_MISMATCH: "structurally_unsuitable",
    UNIT_INCOMPATIBLE: "structurally_unsuitable",
    ADDITIVITY_INCOMPATIBLE: "structurally_unsuitable",
    PROTECTED_CHARACTERISTIC_BLOCKED: "structurally_unsuitable",
    ECONOMIC_ROLE_UNPROVEN: "undecided",
    SEMANTIC_AUTHORITY_INSUFFICIENT: "undecided",
    PROPOSED_METADATA_ONLY: "undecided",
    SEMANTIC_CONFLICT: "needs_data_check",
    REQUIRED_OPERAND_AMBIGUOUS: "undecided",
    CURRENCY_POLICY_MISSING: "needs_setup",
    EVENT_TIME_REQUIRED: "needs_setup",
    AS_OF_TIME_REQUIRED: "needs_setup",
    KNOWLEDGE_TIME_REQUIRED: "needs_setup",
    TEMPORAL_POLICY_UNRESOLVED: "needs_setup",
    RELATIONSHIP_REQUIRED: "needs_setup",
    POPULATION_DATASET_UNDECLARED: "needs_setup",
    DIRECTIONAL_CARDINALITY_UNPROVEN: "needs_data_check",
    JOIN_PATH_DENIED: "structurally_unsuitable",
    PERSONAL_DATA_POLICY_REQUIRED: "needs_setup",
    STATUS_POLICY_UNRESOLVED: "needs_setup",
    POLICY_REFERENCE_UNRESOLVABLE: "needs_setup",
    RENDERER_CANNOT_DISPATCH: "structurally_unsuitable",
    SOURCE_SELECTION_AMBIGUOUS: "undecided",
    IDENTIFIER_UNIQUENESS: "needs_data_check",
    EVENT_HISTORY_VERIFICATION: "needs_data_check",
    BINDING_NOT_BOUND: "undecided",
    BINDING_PLAN_DIVERGENCE: "structurally_unsuitable",
    UOA_MISMATCH: "needs_setup",
    RECIPE_REVIEW_NOT_CURRENT: "undecided",
    CONCEPTUAL_PATTERN_NOT_AUTHORABLE: "needs_setup",
    PHYSICAL_PLAN_MISSING: "needs_setup",
    FORMULA_NOT_REVIEWED: "needs_setup",
    FORMULA_SCHEMA_UNSUPPORTED: "needs_setup",
    READINESS_NOT_MATERIALIZATION_READY: "needs_setup",
    EXTERNAL_VALIDATION_OUTSTANDING: "needs_data_check",
    EXECUTION_AUTHORITY_UNEVALUATED: "needs_setup",
    EXECUTION_AUTHORITY_UNMET: "undecided",
    SNAPSHOT_STALE_REGENERATE: "needs_setup",
    ACTIVATION_STATE_DRIFTED: "needs_setup",
    OUTPUT_POLICY_INCOMPLETE: "needs_setup",
    POLICY_FAMILY_UNVERIFIABLE: "needs_setup",
    HISTORY_DEPTH_INSUFFICIENT: "structurally_unsuitable",
}

#: UI/primary precedence: hard structural truths first, then authority, then setup/checks —
#: the FIRST code present under this order is the verdict's primary_reason_code.
REASON_PRECEDENCE: tuple[str, ...] = (
    TARGET_LEAKAGE_BLOCKED, PROTECTED_CHARACTERISTIC_BLOCKED, JOIN_PATH_DENIED,
    IDENTIFIER_NOT_A_MEASURE, OPERAND_CLASS_MISMATCH, TYPE_INCOMPATIBLE,
    BUSINESS_EVENT_MISMATCH, SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW, SOURCE_GRAIN_MISMATCH,
    UNIT_INCOMPATIBLE, ADDITIVITY_INCOMPATIBLE, CONCEPT_MISMATCH,
    SEMANTIC_CONFLICT, ECONOMIC_ROLE_UNPROVEN,
    SEMANTIC_AUTHORITY_INSUFFICIENT, PROPOSED_METADATA_ONLY,
    REQUIRED_OPERAND_AMBIGUOUS, SOURCE_SELECTION_AMBIGUOUS,
    POPULATION_DATASET_UNDECLARED, RELATIONSHIP_REQUIRED, DIRECTIONAL_CARDINALITY_UNPROVEN,
    CURRENCY_POLICY_MISSING,
    # The specific answer before the blanket one: "the resolver found nothing for this family" is
    # what an operator can act on, and it would be buried under "no resolver serves this kind".
    POLICY_REFERENCE_UNRESOLVABLE, STATUS_POLICY_UNRESOLVED,
    EVENT_TIME_REQUIRED, AS_OF_TIME_REQUIRED, KNOWLEDGE_TIME_REQUIRED,
    TEMPORAL_POLICY_UNRESOLVED, PERSONAL_DATA_POLICY_REQUIRED,
    IDENTIFIER_UNIQUENESS, EVENT_HISTORY_VERIFICATION,
)


def reason_family(code: str) -> str:
    return REASON_FAMILIES[code]


__all__ = [name for name in dir() if name.isupper()] + ["reason_family"]

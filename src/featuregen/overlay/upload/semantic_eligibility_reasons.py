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
# S11: the four remaining EXECUTION-CHAIN facts, discovered while running a chain rather than while
# listing candidates — so, like the two above, the activation policy neither emits nor could emit
# them without compiling every candidate to answer a list query.
#
# The sealed artifact exists and its subgraph check REFUSED it. Structural for this build: no
# decision and no observation makes a refused graph runnable, only a different compilation.
ARTIFACT_NOT_SERVABLE = "ARTIFACT_NOT_SERVABLE"
# The artifact was sealed for one environment and is being verified against another's inventory.
# Also structural — an artifact rendered for a cluster it was not rendered against is the wrong
# artifact, not an under-configured one.
ENVIRONMENT_INCOMPATIBLE = "ENVIRONMENT_INCOMPATIBLE"
# Publication needs a CURRENT passing verification. `needs_setup` because the remedy is to run one:
# the candidate is not unsuitable and nobody has decided against it.
VERIFICATION_NOT_CURRENT = "VERIFICATION_NOT_CURRENT"
# Publication requires a capability attestation and verification must not (§0.3). `needs_setup`
# because the remedy is a grant, which is governance work with an owner.
PUBLICATION_CAPABILITY_MISSING = "PUBLICATION_CAPABILITY_MISSING"
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

# ── The four-stage-gating programme (2026-08-22 plan §5) ─────────────────────────────────────────
# Every code below lands under §5's THREE-PART-COMMIT rule: its definition here, a family row
# below, and a row in `materialize/action_dispositions.py` covering ALL SIX actions — the CI
# exhaustiveness tests refuse a code that skips any part.

# §10 — method identity and certification (production gates; §4.1: the SEALED method decides)
METHOD_CERTIFICATE_MISSING = "METHOD_CERTIFICATE_MISSING"
METHOD_CERTIFICATE_STALE = "METHOD_CERTIFICATE_STALE"
METHOD_CERTIFICATE_MISMATCHED = "METHOD_CERTIFICATE_MISMATCHED"
METHOD_IDENTITY_UNRECORDED = "METHOD_IDENTITY_UNRECORDED"

# §11.2 — buying a formula is an authorized act (conditions of PURCHASE, not of what exists)
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
COST_AUTHORIZATION_MISSING = "COST_AUTHORIZATION_MISSING"
COST_AUTHORIZATION_EXHAUSTED = "COST_AUTHORIZATION_EXHAUSTED"

# §11 / §11.0.1 — the build set must PIN its formula, relationally
FORMULA_DRAFT_NOT_PINNED = "FORMULA_DRAFT_NOT_PINNED"
SELECTION_FORMULA_BINDING_MISSING = "SELECTION_FORMULA_BINDING_MISSING"

# §0.1 / §7.1 — the authorization and decision spine
ACTION_AUTHORIZATION_MISSING = "ACTION_AUTHORIZATION_MISSING"
ACTION_AUTHORIZATION_REVOKED = "ACTION_AUTHORIZATION_REVOKED"
ACTION_DECISION_MISSING = "ACTION_DECISION_MISSING"
DECISION_DRIFT = "DECISION_DRIFT"

# §4.1 / §6 — honest downstream gates and honest measurement absence
FORMULA_NOT_AUTHORED = "FORMULA_NOT_AUTHORED"
FORMULA_REVIEW_UNMEASURED = "FORMULA_REVIEW_UNMEASURED"
ENGINE_CAPABILITY_UNMEASURED = "ENGINE_CAPABILITY_UNMEASURED"

# child §3.2 — the authoring-route selector, surfaced (warnings and refusals BY NAME)
LLM_AUTHORING_REQUIRED = "LLM_AUTHORING_REQUIRED"
REVIEWED_LANE_UNAVAILABLE = "REVIEWED_LANE_UNAVAILABLE"
REVIEWED_EXPECTATION_LEGACY_VERSION = "REVIEWED_EXPECTATION_LEGACY_VERSION"
REVIEWED_BLUEPRINT_NOT_EXECUTABLE = "REVIEWED_BLUEPRINT_NOT_EXECUTABLE"
BLUEPRINT_DERIVED_NOT_REVIEWED = "BLUEPRINT_DERIVED_NOT_REVIEWED"
FORMULA_VALIDATION_FAILED = "FORMULA_VALIDATION_FAILED"
CATALOG_SNAPSHOT_NOT_FROZEN = "CATALOG_SNAPSHOT_NOT_FROZEN"

# §11.1 — the money guard and identity V1/V2
LEGACY_CONFIG_UNPROVEN = "LEGACY_CONFIG_UNPROVEN"
FORMULA_DRAFT_RETIRED = "FORMULA_DRAFT_RETIRED"
LEGACY_REGENERATION_NOT_APPROVED = "LEGACY_REGENERATION_NOT_APPROVED"
FORMULA_DRAFT_NOT_AN_ANSWER = "FORMULA_DRAFT_NOT_AN_ANSWER"
RETIREMENT_OVERRIDDEN = "RETIREMENT_OVERRIDDEN"

# ── Cross-catalog serving (2026-08-24 first-served-card plan: the owner's §capability matrix
# and the six-action availability block). Same three-part-commit rule as §5 above: constant here,
# family row below, all-six-actions row in `materialize/action_dispositions.py`. None of these is
# emitted by the activation policy, so no `ACTIVATION_BLOCKER_DISPOSITIONS` row exists for them.

# The link/join facts. The matrix's Formula column is Allow under EVERY link condition — these
# gate from preview (the first act that computes over the join), never authoring.
DIRECTIONAL_REALIZATION_MISSING = "DIRECTIONAL_REALIZATION_MISSING"     # no physical realization of the link yet (pre-A4c; D1)
DIRECTIONAL_MAPPING_INCOMPLETE = "DIRECTIONAL_MAPPING_INCOMPLETE"       # matrix: "Missing directional mapping"
EXECUTION_CONTEXT_MISSING = "EXECUTION_CONTEXT_MISSING"                 # no adopted execution context (R2: never logical identity)
# R11: unknown cardinality is previewable ONLY under a complete pinned guard policy — each
# guard-policy component that is absent refuses preview under its own name.
JOIN_NULL_POLICY_MISSING = "JOIN_NULL_POLICY_MISSING"
JOIN_COVERAGE_POLICY_MISSING = "JOIN_COVERAGE_POLICY_MISSING"
MAX_MATCH_POLICY_MISSING = "MAX_MATCH_POLICY_MISSING"
TEMPORAL_JOIN_POLICY_MISSING = "TEMPORAL_JOIN_POLICY_MISSING"           # R14: a policy nobody assessed is never applied
# R13: COUNT over duplicate governed transaction identity REFUSES — preview renders the guard,
# the run refuses; the (chartered) remedy is a governed deduplication policy, never COUNT_DISTINCT.
TRANSACTION_IDENTITY_NOT_UNIQUE = "TRANSACTION_IDENTITY_NOT_UNIQUE"
# The fan-out law (matrix: "Known M:N, final grain") — ONE spelling; `planner/physical_plan_v1`
# re-exports this constant for its construction-time refusals.
ALLOCATION_POLICY_REQUIRED = "ALLOCATION_POLICY_REQUIRED"
# S2-P6 split: source compatibility blocks EXECUTE_SANDBOX, never GENERATE_PREVIEW.
EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN = "EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN"
# A6 / G2: the platform's TWO governed authorities on what an operand contributes to a join —
# the recipe author's `operand_class` (projected by `planner/requests`) and the concept
# registry's `entity_link`/`pit_role` ladder (`need_metadata._derive_one`) — disagree about this
# operand, or neither yields a role at all. Today that divergence is SILENT: `_derive_one`
# defaults a status/dimension/direction to MEASURE and `compile_aggregation` short-circuits
# before the additivity matrix runs, so an operand nobody meant to aggregate is staged as one
# the moment a cardinality attaches. The harm is AT THE JOIN — the disputed slot decides hop KEY
# versus staged MEASURE — so this follows the owner's matrix like every other link/join fact:
# authoring proceeds with the caller told, and every act that would COMPUTE over the crossing
# refuses, while the card stays visible as setup work. The cure is a RULING per operand — G2's
# chartered work — expressed as a declared `join_role` (see `recipes/transaction_foundation.py`
# for the first two rulings taken).
OPERAND_ROLE_UNRESOLVED = "OPERAND_ROLE_UNRESOLVED"

# Deployment-capability facts — SERVER-OWNED, folded by the same six-action service (never a
# route-local switch). Each gates exactly the action it names; PUBLISH_SANDBOX is TWO DISTINCT
# facts, never one explanation (round-13 P1-10): the capability itself unreleased vs the
# capability present but THIS artifact unverified.
SANDBOX_EXECUTION_NOT_RELEASED = "SANDBOX_EXECUTION_NOT_RELEASED"
SANDBOX_PUBLICATION_NOT_RELEASED = "SANDBOX_PUBLICATION_NOT_RELEASED"
VERIFIED_OUTPUT_REQUIRED = "VERIFIED_OUTPUT_REQUIRED"
PRODUCTION_MATERIALIZATION_NOT_RELEASED = "PRODUCTION_MATERIALIZATION_NOT_RELEASED"
PRODUCTION_PUBLICATION_NOT_RELEASED = "PRODUCTION_PUBLICATION_NOT_RELEASED"

#: The owner's binding CAPABILITY MATRIX, as a set: every link / join / policy fact a served
#: cross-catalog option can carry. The matrix's Formula column reads "Allow" in EVERY row, so the
#: law over this set is structural — **no member may BLOCK at AUTHOR_FORMULA**. The harm each of
#: them names happens at the JOIN, which is why they gate from GENERATE_PREVIEW, the first act
#: that computes over one.
#:
#: It lives HERE, beside the codes it groups, rather than as a copy inside a test: the previous
#: spelling of that law iterated a hardcoded tuple in `test_action_dispositions.py`, so a newly
#: registered code simply escaped it — which is exactly how A6's own row shipped as BLOCK × six
#: without failing anything. Adding a cross-catalog serving code means adding it here, and CI
#: then enforces the matrix law on it, checks it is a real vocabulary code, and fails on a silent
#: removal. (Residual: a brand-new code still has to be added to this set — the improvement is
#: that there is now ONE place to add it, next to the constants, instead of a duplicate list in a
#: test file nobody reads when registering a code.)
SERVING_CAPABILITY_MATRIX_CODES: frozenset[str] = frozenset({
    DIRECTIONAL_REALIZATION_MISSING,
    DIRECTIONAL_MAPPING_INCOMPLETE,
    JOIN_NULL_POLICY_MISSING,
    JOIN_COVERAGE_POLICY_MISSING,
    MAX_MATCH_POLICY_MISSING,
    TEMPORAL_JOIN_POLICY_MISSING,
    ALLOCATION_POLICY_REQUIRED,
    TRANSACTION_IDENTITY_NOT_UNIQUE,
    EXECUTION_CONTEXT_MISSING,
    EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN,
    OPERAND_ROLE_UNRESOLVED,
})

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
    ARTIFACT_NOT_SERVABLE: "structurally_unsuitable",
    ENVIRONMENT_INCOMPATIBLE: "structurally_unsuitable",
    VERIFICATION_NOT_CURRENT: "needs_setup",
    PUBLICATION_CAPABILITY_MISSING: "needs_setup",
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
    # ── four-stage-gating (§5) ──────────────────────────────────────────────────────────────────
    METHOD_CERTIFICATE_MISSING: "needs_setup",
    METHOD_CERTIFICATE_STALE: "needs_setup",
    METHOD_CERTIFICATE_MISMATCHED: "needs_setup",
    METHOD_IDENTITY_UNRECORDED: "needs_setup",
    PROVIDER_UNAVAILABLE: "needs_setup",
    COST_AUTHORIZATION_MISSING: "undecided",
    COST_AUTHORIZATION_EXHAUSTED: "undecided",
    FORMULA_DRAFT_NOT_PINNED: "needs_setup",
    SELECTION_FORMULA_BINDING_MISSING: "needs_setup",
    ACTION_AUTHORIZATION_MISSING: "needs_setup",
    ACTION_AUTHORIZATION_REVOKED: "needs_setup",
    ACTION_DECISION_MISSING: "needs_setup",
    DECISION_DRIFT: "needs_setup",
    FORMULA_NOT_AUTHORED: "needs_setup",
    FORMULA_REVIEW_UNMEASURED: "needs_setup",
    ENGINE_CAPABILITY_UNMEASURED: "needs_data_check",
    LLM_AUTHORING_REQUIRED: "needs_setup",
    REVIEWED_LANE_UNAVAILABLE: "needs_setup",
    REVIEWED_EXPECTATION_LEGACY_VERSION: "needs_setup",
    REVIEWED_BLUEPRINT_NOT_EXECUTABLE: "needs_setup",
    BLUEPRINT_DERIVED_NOT_REVIEWED: "undecided",
    FORMULA_VALIDATION_FAILED: "needs_setup",
    CATALOG_SNAPSHOT_NOT_FROZEN: "needs_setup",
    LEGACY_CONFIG_UNPROVEN: "structurally_unsuitable",
    FORMULA_DRAFT_RETIRED: "undecided",
    LEGACY_REGENERATION_NOT_APPROVED: "undecided",
    FORMULA_DRAFT_NOT_AN_ANSWER: "undecided",
    RETIREMENT_OVERRIDDEN: "needs_setup",
    # ── cross-catalog serving (2026-08-24 plan) ─────────────────────────────────────────────────
    # The remedies are governance/setup work with an owner: produce a realization (A4c), complete
    # a mapping, adopt an execution context, declare a policy. None is "unsuitable" — the plan's
    # whole point is that each has a named, buildable cure.
    DIRECTIONAL_REALIZATION_MISSING: "needs_setup",
    DIRECTIONAL_MAPPING_INCOMPLETE: "needs_setup",
    EXECUTION_CONTEXT_MISSING: "needs_setup",
    JOIN_NULL_POLICY_MISSING: "needs_setup",
    JOIN_COVERAGE_POLICY_MISSING: "needs_setup",
    MAX_MATCH_POLICY_MISSING: "needs_setup",
    TEMPORAL_JOIN_POLICY_MISSING: "needs_setup",
    # A PROVEN duplicate: the check already ran, so not needs_data_check. The remedy is a governed
    # deduplication policy (chartered, outside this increment) or upstream data ownership — policy
    # work with an owner, honestly named setup.
    TRANSACTION_IDENTITY_NOT_UNIQUE: "needs_setup",
    ALLOCATION_POLICY_REQUIRED: "needs_setup",
    # Unproven, and a measurement would settle it — the definition of needs_data_check.
    EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN: "needs_data_check",
    # No measurement settles a disagreement between two DECLARATIONS, and the binding is not
    # wrong — it is unruled. The remedy is governance work with an owner (declare the operand's
    # join role, or reconcile the concept registry), which is `needs_setup` — the family every
    # other `*_UNRESOLVED` code in this vocabulary already carries.
    OPERAND_ROLE_UNRESOLVED: "needs_setup",
    # Platform capability releases: governance work with an owner (the PUBLICATION_CAPABILITY_
    # MISSING precedent), never a defect of the candidate.
    SANDBOX_EXECUTION_NOT_RELEASED: "needs_setup",
    SANDBOX_PUBLICATION_NOT_RELEASED: "needs_setup",
    VERIFIED_OUTPUT_REQUIRED: "needs_setup",
    PRODUCTION_MATERIALIZATION_NOT_RELEASED: "needs_setup",
    PRODUCTION_PUBLICATION_NOT_RELEASED: "needs_setup",
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

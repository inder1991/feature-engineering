"""Remediation A1 — the ONE activation policy: nothing durable happens without this fold.

Every route that turns a candidate into something permanent — saving an idea, creating a
governed contract, authoring a formula, requesting or executing materialization — asks this
policy first. It is a PURE fold (no connection in its signature, by test) over two layers,
both load-bearing:

* ``FrozenOptionFactsV1`` — exactly what the user saw, from the immutable option decision.
  Never recomputed, never rebound: the frozen layer answers "was THIS candidate ready?".
* ``CurrentActivationStateV1`` — a small re-read the CALLER performs at the moment of the
  durable write: is the review still current at the frozen revision, are the policy pins
  still the active revisions, is the snapshot still fresh, is the formula schema executable?
  The current layer answers "is it STILL true?" — divergence is a typed regenerate blocker,
  never a silent substitution (the same law ``govern.py``'s plan-freshness recheck already
  enforces for governed cross-catalog plans).

Actions form a closed five-step ladder: ``save_idea`` → ``create_contract`` →
``author_formula`` → ``request_materialization`` → ``execute_materialization``. Authoring is
deliberately NOT gated on formula readiness — authoring is how readiness improves; gating it
on the thing it produces would deadlock the workflow. Materialization requires current
effective readiness EXACTLY ``MATERIALIZATION_READY`` plus an engine that advertises the
formula's schema — "a reviewed expectation exists" is never a substitute for executable
readiness.

Every refusal is a ``BlockerV1`` with a closed reason code and the named next step a human can
actually take. A blocked action never hides a candidate — the caller serves the blockers.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload import semantic_eligibility_reasons as R

ACTIVATION_POLICY_VERSION = "activation-policy@1"

#: The closed action ladder, weakest to strongest.
ACTIVATION_ACTIONS = ("save_idea", "create_contract", "author_formula",
                      "request_materialization", "execute_materialization")

MATERIALIZATION_READY = "MATERIALIZATION_READY"

# ── §6 — the formula-schema-support TRI-STATE. `_formula_schema_supported` used to return a bool
# whose False conflated "the engine said no" with "nobody measured", so an unreviewed recipe was
# told "engine support not implemented" — a FALSE STATEMENT that sent operators to the engine team
# for a governance problem. Four values, and each failure surfaces under its own name.
SCHEMA_SUPPORT_SUPPORTED = "supported"                        # classify ran and said yes
SCHEMA_SUPPORT_UNSUPPORTED = "unsupported"                    # classify ran and said NO
SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED = "unmeasured_unreviewed"  # nothing reviewed to measure
SCHEMA_SUPPORT_UNMEASURED_ENGINE = "unmeasured_engine"        # measurement absent or it raised


class ActivationPolicyError(ValueError):
    """An unknown action or malformed facts — a PROGRAMMER error, never a user outcome."""


@dataclass(frozen=True, slots=True)
class FrozenOptionFactsV1:
    """What the user saw — assembled from the immutable option decision (A1b), never live."""

    binding_state: str                          # bound | ambiguous | missing | blocked
    generation_source: str                      # recipe | llm_intent | user_defined
    computation_kind: str                       # deterministic_formula | governed_model_output | conceptual_pattern
    readiness: str                              # authored RECIPE_READINESS at generation
    review_current: bool                        # BR-23 fold at generation, recipe-origin only
    source_definition_id: str = ""
    recipe_revision_hash: str = ""
    confirmation_required_roles: tuple[str, ...] = ()
    has_reviewed_formula_expectation: bool = False
    plan_envelope_present: bool = False
    validation_status: str = "NEEDS_EXTERNAL_VALIDATION"
    outstanding_requirement_codes: tuple[str, ...] = ()
    policy_revision_pins: tuple[str, ...] = ()
    formula_expectation_revision: str = ""
    snapshot_id: str = ""
    plan_refusal_codes: tuple[str, ...] = ()   # WHY no plan folded (B7/B10 refusals)
    readiness_blockers: tuple[str, ...] = ()   # the serving fold's measured blockers (C3):
    #                                            temporal/binding/policy facts the durable write
    #                                            cannot re-measure — carried into the re-fold
    confirmed_uoa_entity: str = ""              # the UOA the human had confirmed at serving
    read_set: tuple[str, ...] = ()              # the frozen plan's bound refs (C2 floor input)
    plan_catalog_source: str = ""
    operand_authorities: tuple[tuple[str, str], ...] = ()  # (ref, measured authority) at serving


@dataclass(frozen=True, slots=True)
class CurrentActivationStateV1:
    """The caller's re-read at the durable write. EVERY default is the failing side — an
    unassembled current state blocks, exactly like an unmeasured release gate."""

    review_current: bool = False                # re-read NOW, at the frozen revision hash
    policy_revisions_current: bool = False      # frozen pins are still the active revisions
    snapshot_freshness: str = "unverifiable"    # current | drifted | unverifiable — only
    #                                             "current" passes; unverifiable FAILS CLOSED
    #                                             (this workflow is new; it has no compat debt)
    effective_readiness: str = ""               # current effective readiness, re-folded
    formula_expectation_revision: str = ""
    formula_schema_support: str = SCHEMA_SUPPORT_UNMEASURED_ENGINE  # §6 tri-state; the field
    #                                             was a bool, renamed so no caller can hand the
    #                                             new str to the old truthiness check unnoticed
    requirements_closed: bool = False           # every typed requirement has a recorded result
    execution_authority_evaluated: bool = False  # the C2 execution floor was actually folded
    execution_floor_met: bool = False           # every bound operand clears execution_at_governed
    authoring_floor_met: bool = False           # every bound operand STILL clears authoring
    uoa_current: bool = False                   # the confirmed UOA has not moved since serving


@dataclass(frozen=True, slots=True)
class BlockerV1:
    code: str
    next_step: str


@dataclass(frozen=True, slots=True)
class ActivationDecisionV1:
    action: str
    allowed: bool
    blockers: tuple[BlockerV1, ...]
    policy_version: str = ACTIVATION_POLICY_VERSION


_FUNNEL_STEP = ("confirm the AI-proposed concept(s) in the Governance screen's "
                "concept-confirmation queue, then regenerate")
_REVIEW_STEP = "record a recipe review at the current revision on the recipe-review screen"
_REGENERATE_STEP = "regenerate from the current considered set"


def _contract_blockers(frozen: FrozenOptionFactsV1,
                       current: CurrentActivationStateV1) -> list[BlockerV1]:
    """The create_contract rules — shared by every rung above it on the ladder."""
    blockers: list[BlockerV1] = []
    if frozen.binding_state != "bound":
        blockers.append(BlockerV1(
            R.BINDING_NOT_BOUND,
            "resolve the binding first — this candidate never bound every required operand"))
    if frozen.confirmation_required_roles:
        roles = ", ".join(frozen.confirmation_required_roles)
        blockers.append(BlockerV1(R.PROPOSED_METADATA_ONLY, f"{_FUNNEL_STEP} (roles: {roles})"))
    if frozen.generation_source == "recipe" and not (frozen.review_current
                                                     and current.review_current):
        blockers.append(BlockerV1(R.RECIPE_REVIEW_NOT_CURRENT, _REVIEW_STEP))
    if frozen.computation_kind == "conceptual_pattern":
        blockers.append(BlockerV1(
            R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE,
            "a conceptual idea has no exact computation — save it as an idea; the formula "
            "seam is what promotes it"))
    if not frozen.plan_envelope_present:
        if R.UOA_MISMATCH in frozen.plan_refusal_codes:
            blockers.append(BlockerV1(
                R.UOA_MISMATCH,
                "this computes at a different grain than your confirmed unit of analysis — "
                "roll it up via a matching-grain recipe, or change the confirmed UOA at scope"))
        else:
            blockers.append(BlockerV1(
                R.PHYSICAL_PLAN_MISSING,
                "no frozen source/join/point-in-time plan exists for this candidate — "
                "regenerate once planning resolves, or resolve the named plan refusal"))
    if current.snapshot_freshness != "current":
        blockers.append(BlockerV1(R.SNAPSHOT_STALE_REGENERATE, _REGENERATE_STEP))
    if not current.policy_revisions_current:
        blockers.append(BlockerV1(R.ACTIVATION_STATE_DRIFTED, _REGENERATE_STEP))
    if not current.uoa_current:
        # B10 item 4: the human re-confirmed a DIFFERENT unit of analysis after this card was
        # served — the card answers a question nobody is asking anymore. Regenerate.
        blockers.append(BlockerV1(R.ACTIVATION_STATE_DRIFTED, _REGENERATE_STEP))
    if R.PERSONAL_DATA_POLICY_REQUIRED in frozen.outstanding_requirement_codes:
        # C4 (D14): read-allowed is not use-allowed — unlicensed personal data never
        # underwrites a governed contract; the fix is a purpose with an owner.
        blockers.append(BlockerV1(
            R.PERSONAL_DATA_POLICY_REQUIRED,
            "a bound column is personal data with no active use policy — a governance "
            "owner declares the purpose under Governance -> Data-use policies, then "
            "regenerate"))
    if not frozen.confirmation_required_roles and not current.authoring_floor_met:
        # C2: the AUTHORING floor, re-read at the durable write. The serve-time answer rides
        # the frozen confirmation_required_roles (funnel-named, role-specific); THIS rule
        # catches drift only — a value confirmed at serving whose authority weakened since.
        blockers.append(BlockerV1(
            R.SEMANTIC_AUTHORITY_INSUFFICIENT,
            "a bound operand's metadata authority dropped below the authoring floor since "
            "this card was served — re-confirm the value, then regenerate"))
    return blockers


def _materialization_blockers(frozen: FrozenOptionFactsV1,
                              current: CurrentActivationStateV1) -> list[BlockerV1]:
    blockers = _contract_blockers(frozen, current)
    if current.effective_readiness != MATERIALIZATION_READY:
        blockers.append(BlockerV1(
            R.READINESS_NOT_MATERIALIZATION_READY,
            f"current effective readiness is "
            f"{current.effective_readiness or 'unmeasured'!s} — materialization requires "
            f"exactly {MATERIALIZATION_READY}"))
    if not frozen.has_reviewed_formula_expectation:
        blockers.append(BlockerV1(
            R.FORMULA_NOT_REVIEWED,
            "author and review a formula expectation for this recipe — execution is "
            "unavailable until then (contract-authoring is not affected)"))
    support = current.formula_schema_support
    if support == SCHEMA_SUPPORT_UNSUPPORTED:
        # §6: reserved for the case it names — classify_demands_for_engine RAN and said no.
        blockers.append(BlockerV1(
            R.FORMULA_SCHEMA_UNSUPPORTED,
            "the execution engine was asked and does not advertise this formula schema — "
            "never downgrade to an older schema"))
    elif support == SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED:
        # §6: missing review must not wear an engine-capability code. FORMULA_NOT_REVIEWED
        # (above) already carries the governance fact; this names why support is unmeasured.
        blockers.append(BlockerV1(
            R.FORMULA_REVIEW_UNMEASURED,
            "engine support was never measured — no reviewed formula expectation exists to "
            "measure; a governance gap, not an engine one"))
    elif support != SCHEMA_SUPPORT_SUPPORTED:
        # Unknown engine, the classification raised, or an unrecognized value: an unmeasured
        # engine is not a supported one, and an unrecognized measurement is an unmeasured one.
        blockers.append(BlockerV1(
            R.ENGINE_CAPABILITY_UNMEASURED,
            "the engine-capability measurement did not run or did not resolve — re-measure "
            "before relying on an engine claim"))
    if (frozen.formula_expectation_revision
            and frozen.formula_expectation_revision != current.formula_expectation_revision):
        blockers.append(BlockerV1(R.ACTIVATION_STATE_DRIFTED, _REGENERATE_STEP))
    if frozen.validation_status != "DESIGN_CHECKED" and not current.requirements_closed:
        blockers.append(BlockerV1(
            R.EXTERNAL_VALIDATION_OUTSTANDING,
            "run and record the named data checks "
            f"({', '.join(frozen.outstanding_requirement_codes) or 'see the option detail'})"))
    if not current.execution_authority_evaluated:
        blockers.append(BlockerV1(
            R.EXECUTION_AUTHORITY_UNEVALUATED,
            "the execution-authority floor was not evaluated for this option (no frozen "
            "plan read set) — regenerate so the floor can be measured"))
    elif not current.execution_floor_met:
        blockers.append(BlockerV1(
            R.EXECUTION_AUTHORITY_UNMET,
            "raise the bound operands' metadata authority to the execution floor "
            "(human-confirmed or source-attested), then regenerate"))
    return blockers


def activation_decision(frozen: FrozenOptionFactsV1, current: CurrentActivationStateV1,
                        action: str, actor: object = None) -> ActivationDecisionV1:
    """Decide one action for one frozen option under the current state. Pure and total over
    the closed action ladder; an unknown action raises (programmer error, never a 4xx)."""
    if action not in ACTIVATION_ACTIONS:
        raise ActivationPolicyError(f"unknown activation action {action!r}; "
                                    f"the ladder is {ACTIVATION_ACTIONS}")
    if action == "save_idea":
        blockers: list[BlockerV1] = []                       # an idea is an idea — always
    elif action in ("create_contract", "author_formula"):
        blockers = _contract_blockers(frozen, current)
    else:
        blockers = _materialization_blockers(frozen, current)
    # Deterministic, deduplicated (state-drift can be flagged by two rules), order-stable.
    seen: set[str] = set()
    unique = tuple(b for b in blockers if not (b.code in seen or seen.add(b.code)))
    return ActivationDecisionV1(action=action, allowed=not unique, blockers=unique)


def decide_all_actions(frozen: FrozenOptionFactsV1,
                       current: CurrentActivationStateV1) -> dict[str, ActivationDecisionV1]:
    """Every rung of the ladder at once — the wire shape's source (`allowed_actions` +
    `blocked_actions`), computed by the SAME fold the durable writes consult."""
    return {action: activation_decision(frozen, current, action)
            for action in ACTIVATION_ACTIONS}


__all__ = ["ACTIVATION_ACTIONS", "ACTIVATION_POLICY_VERSION", "ActivationDecisionV1",
           "ActivationPolicyError", "BlockerV1", "CurrentActivationStateV1",
           "FrozenOptionFactsV1", "MATERIALIZATION_READY", "SCHEMA_SUPPORT_SUPPORTED",
           "SCHEMA_SUPPORT_UNSUPPORTED", "SCHEMA_SUPPORT_UNMEASURED_UNREVIEWED",
           "SCHEMA_SUPPORT_UNMEASURED_ENGINE", "activation_decision",
           "decide_all_actions"]

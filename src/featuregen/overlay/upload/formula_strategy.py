"""HOW a selected feature obtains its formula — decided from evidence, server-side, before authoring.

**Two axes, and conflating them is the defect this exists to prevent.** Candidate ORIGIN is where
the idea came from; formula METHOD is how the formula was produced. A recipe-origin feature may be
LLM-authored — today every one is — and migration 1088's CHECK tied ``origin = 'recipe'`` to a
reviewed blueprint revision, which made exactly that combination unrepresentable in the database.

▲ **"Reviewed" means REGISTRY MEMBERSHIP, not derivability.** ``derive_blueprint_v2`` is pure and
cheerful and derives a blueprint for **90** of 317 recipes; the reviewed registry has **one** entry.
A resolver written from the derivation reads as correct, passes its own tests, and seals ninety
unreviewed method claims into an append-only provenance table — which is the one failure this
programme cannot audit its way out of afterwards.

**The selector below is PURE.** It reads no database and chooses no columns, so every combination is
testable without a world. A facts assembler reads the frozen option, the current review/expectation
revision and the bound blueprint; this decides.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from featuregen.canonical import jcs_sha256

__all__ = [
    "STRATEGY_IDENTITY_VERSION",
    "STRATEGY_RESOLVER_VERSION",
    "FormulaStrategy",
    "FormulaStrategyDecisionV1",
    "FormulaStrategyFactsV1",
    "resolve_formula_strategy",
]

#: The RESOLVER's rules. Bump when the routing changes, so an identity minted under different rules
#: is visibly different rather than silently comparable.
STRATEGY_RESOLVER_VERSION = 1
#: The PAYLOAD's composition. Bump when a field joins or leaves — `expectation_generation` will.
STRATEGY_IDENTITY_VERSION = 1


class FormulaStrategy(StrEnum):
    REVIEWED_RECIPE_BLUEPRINT = "REVIEWED_RECIPE_BLUEPRINT"
    LLM_AUTHORED = "LLM_AUTHORED"
    #: Not a formula at all — a conceptual pattern. "Save idea / specify computation", never a
    #: deterministic Generate Code affordance.
    NON_FORMULA = "NON_FORMULA"
    #: A governed model output. A different set of acts with its own certification programme.
    MODEL_WORKFLOW = "MODEL_WORKFLOW"


@dataclass(frozen=True, slots=True)
class FormulaStrategyFactsV1:
    """What the assembler read. ▲ Every field is a FACT, never a verdict."""

    candidate_origin: str
    computation_kind: str

    recipe_id: str | None = None
    recipe_revision_hash: str | None = None

    #: ▲ NEVER THE RECIPE ID — the registry's own rule: 295 of 317 recipes declare a ref that is not
    #: their own name.
    expectation_ref: str | None = None
    #: ``v1`` | ``v2`` | ``None``. ▲ An UNKNOWN generation fails closed; it never becomes v2 by
    #: default, because two of the three reviewed recipes are Formula V1.
    expectation_generation: str | None = None
    #: REGISTRY MEMBERSHIP, current. The gate.
    reviewed_expectation_current: bool = False

    #: ▲ INFORMATIONAL ONLY, and it grants nothing. Carried so the UI can say "a blueprint could be
    #: derived — send it to review". A selector that branches on this claims a review that no
    #: derivation can supply.
    blueprint_derivable: bool = False
    blueprint_bindable: bool = False

    reviewed_blueprint_revision: str | None = None
    reviewed_blueprint_hash: str | None = None
    review_validity_evidence: str | None = None


    provider_contract_hash: str | None = None
    catalog_snapshot_hash: str | None = None
    binding_plan_hash: str | None = None
    considered_revision_id: str | None = None
    option_id: str | None = None

    #: A server-authored override (parent §11.3), consumed as an INPUT FACT. The client never sends
    #: a method; it asks for an override, and the server verifies the refusal it names.
    method_override_revision_id: str | None = None

    #: ▲ The deterministic lane's EXECUTION POSTURE — a declared deployment capability, and a
    #: MEASURED fact that stays OUT of the identity hash. It changes which STRATEGY is chosen, and
    #: the strategy is what the identity folds — so flipping the posture changes future drafts'
    #: identities through the front door, never by re-minting an existing one.
    deterministic_lane_available: bool = False


@dataclass(frozen=True, slots=True)
class FormulaStrategyDecisionV1:
    strategy: FormulaStrategy
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    strategy_identity_hash: str


def _identity(facts: FormulaStrategyFactsV1, strategy: FormulaStrategy) -> str:
    """The evidence that chose this method, hashed.

    ▲ **MEASURED FACTS STAY OUT** — `blueprint_derivable` and `blueprint_bindable` are measurements
    of the world, and a measurement that flaps re-mints the draft identity and buys the same answer
    twice, which is the expense the money guard exists to prevent.
    """
    return jcs_sha256({
        "strategy_identity_version": STRATEGY_IDENTITY_VERSION,
        "resolver_policy_version": STRATEGY_RESOLVER_VERSION,
        "strategy": str(strategy),
        "considered_revision_id": facts.considered_revision_id,
        "option_id": facts.option_id,
        "expectation_ref": facts.expectation_ref,
        "expectation_generation": facts.expectation_generation,
        "reviewed_expectation_current": facts.reviewed_expectation_current,
        "reviewed_blueprint_revision": facts.reviewed_blueprint_revision,
        "reviewed_blueprint_hash": facts.reviewed_blueprint_hash,
        "review_validity_evidence": facts.review_validity_evidence,
        "method_override_revision_id": facts.method_override_revision_id,
        "provider_contract_hash": facts.provider_contract_hash,
        "catalog_snapshot_hash": facts.catalog_snapshot_hash,
        "binding_plan_hash": facts.binding_plan_hash,
    })


def _decide(facts: FormulaStrategyFactsV1) -> tuple[FormulaStrategy, tuple[str, ...], tuple[str, ...]]:
    if facts.computation_kind == "conceptual_pattern":
        return FormulaStrategy.NON_FORMULA, ("CONCEPTUAL_PATTERN_NOT_AUTHORABLE",), ()
    if facts.computation_kind == "governed_model_output":
        return FormulaStrategy.MODEL_WORKFLOW, ("GOVERNED_MODEL_OUTPUT",), ()

    # ▲ AN OVERRIDE IS EVIDENCE, NOT A LABEL. It reaches here only because the server verified the
    # deterministic refusal it names; the resolver still decides, which is what keeps method
    # selection server-owned while "Try AI formula" remains possible.
    if facts.method_override_revision_id:
        return FormulaStrategy.LLM_AUTHORED, (), ("METHOD_OVERRIDDEN_TO_LLM",)

    if facts.reviewed_expectation_current and facts.expectation_generation == "v2":
        if not facts.deterministic_lane_available:
            # ▲ A PLATFORM capability gap is not a CANDIDATE defect, and the two must not share a
            # code. The deterministic lane cannot execute in this deployment (its grounding-context
            # plumbing is not yet persisted to draft time), so the candidate authors by LLM — WITH
            # THE REASON RECORDED, which is what makes this routing honest rather than silent.
            # `REVIEWED_BLUEPRINT_NOT_EXECUTABLE` below stays reserved for a blueprint that
            # genuinely failed against this candidate.
            return FormulaStrategy.LLM_AUTHORED, (), ("REVIEWED_LANE_UNAVAILABLE",)
        if not facts.blueprint_bindable:
            # Named, and it does NOT fall back. A silent fallback would hide a broken reviewed
            # blueprint, change the cost and change which certificate production needs.
            return (FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT,
                    ("REVIEWED_BLUEPRINT_NOT_EXECUTABLE",), ())
        return FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT, (), ()

    if facts.reviewed_expectation_current and facts.expectation_generation == "v1":
        return FormulaStrategy.LLM_AUTHORED, (), ("REVIEWED_EXPECTATION_LEGACY_VERSION",)

    # ▲ DERIVABLE IS NOT REVIEWED, and this is the branch the whole module guards. Ninety
    # derivations exist and one review does.
    if facts.blueprint_derivable:
        return FormulaStrategy.LLM_AUTHORED, (), ("BLUEPRINT_DERIVED_NOT_REVIEWED",)

    return FormulaStrategy.LLM_AUTHORED, (), ("LLM_AUTHORING_REQUIRED",)


def resolve_formula_strategy(facts: FormulaStrategyFactsV1) -> FormulaStrategyDecisionV1:
    """Choose the authoring method from evidence. Pure — no database, no clock, no client input.

    ▲ **An unknown expectation generation FAILS CLOSED.** ``None`` and anything that is not exactly
    ``"v2"`` route to the LLM, because a generation the platform cannot name is not one it can
    certify a deterministic producer against.
    """
    strategy, blockers, warnings = _decide(facts)
    return FormulaStrategyDecisionV1(
        strategy=strategy, blockers=blockers, warnings=warnings,
        strategy_identity_hash=_identity(facts, strategy))


def facts_without_measurements(facts: FormulaStrategyFactsV1) -> FormulaStrategyFactsV1:
    """The same facts with the MEASURED axes cleared — the test helper for the invariant that
    matters most: flipping `blueprint_derivable` must not move a decision that registry membership
    already settled."""
    return replace(facts, blueprint_derivable=False, blueprint_bindable=facts.blueprint_bindable)

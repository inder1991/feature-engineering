"""BR-7 — the execution-readiness fold: UNASSESSED is replaced by a closed, audited vocabulary.

One PURE function answers "how real is this recipe right now?" from five inputs, every one of
them something another part of the platform already produced — the fold ASSERTS nothing itself:

* the definition's ``computation_kind`` and construction-time validity (BR-2);
* the temporal compiler's verdict and blockers (BR-4, ``CompiledTemporalV1``);
* the operand binding verdicts (BR-5, ``OperandBindingVerdictV1`` — ambiguous/blocked/missing);
* whether a REVIEWED formula expectation exists and its gold/provider evaluation passed (the
  recipe_formula_* gates);
* the execution engine's capability verdict (BR-6, ``classify_formula_capability_v2``).

The ladder, and what each rung REQUIRES (never implies):

    CONCEPTUAL_ONLY          the definition says so — a useful pattern, no exact computation
    FORMULA_BLOCKED          exact formula intended, but a NAMED authority is unresolved
    FORMULA_AUTHORABLE       reviewed expectation exists; grammar accepts it; bindings clean
    FORMULA_VALIDATED        + gold and provider evaluation gates passed
    MATERIALIZATION_BLOCKED  + the selected engine cannot run it (or a governed policy is absent)
    MATERIALIZATION_READY    + engine capability proven
    RETIRED                  kept for legacy resolution only

Every non-terminal state carries MACHINE-READABLE blocker codes — the BR-4/BR-5 vocabularies pass
through verbatim, so "why isn't this ready?" is a list of named facts, not a shrug. An LLM
assertion, template prose, or a hopeful default can not move a recipe up this ladder: every rung
is a typed input some deterministic gate produced. ``UNASSESSED`` does not exist here — it lives
only on the legacy adapter's projection, and contract v3 (BR-8) renders it as the idea it is.
"""
from __future__ import annotations

from dataclasses import dataclass, field

READINESS_LADDER = ("CONCEPTUAL_ONLY", "FORMULA_BLOCKED", "FORMULA_AUTHORABLE",
                    "FORMULA_VALIDATED", "MATERIALIZATION_BLOCKED", "MATERIALIZATION_READY",
                    "RETIRED")

BLOCKER_NO_REVIEWED_EXPECTATION = "no_reviewed_formula_expectation"
BLOCKER_GRAMMAR_UNSUPPORTED = "formula_outside_grammar_capability"
BLOCKER_GOLD_UNPROVEN = "gold_evaluation_unproven"
BLOCKER_ENGINE_UNSUPPORTED = "engine_capability_unproven"


@dataclass(frozen=True, slots=True)
class ReadinessInputsV1:
    """Everything the fold reads — each field the OUTPUT of a deterministic gate elsewhere.
    Defaults are the HONEST ABSENT state, so a caller that cannot yet answer a question leaves
    the recipe blocked rather than promoted."""

    computation_kind: str                       # BR-2 definition
    retired: bool = False
    temporal_blockers: tuple[str, ...] = ()     # BR-4 CompiledTemporalV1.blockers
    binding_blockers: tuple[str, ...] = ()      # BR-5 verdict reason codes (non-bound operands)
    reviewed_expectation: bool = False          # the expectation registry holds this recipe
    grammar_verdict: str = "unsupported_capability"   # BR-6 classify (authoring-time)
    gold_validated: bool = False                # gold + provider evaluation gates passed
    engine_verdict: str | None = None           # BR-6 classify with the SELECTED engine;
    #                                             None = no engine selected yet — the recipe
    #                                             rests at FORMULA_VALIDATED, honestly
    governed_policy_blockers: tuple[str, ...] = ()   # unresolved policy refs (BR-8/BR-23 era)


@dataclass(frozen=True, slots=True)
class RecipeReadinessV1:
    state: str                                  # READINESS_LADDER
    blockers: tuple[str, ...] = field(default=())


def fold_readiness(inputs: ReadinessInputsV1) -> RecipeReadinessV1:
    """The fold. Total, pure, and monotone in its inputs: clearing a blocker can only move a
    recipe UP the ladder, and nothing here can move one up without the corresponding input."""
    if inputs.retired:
        return RecipeReadinessV1("RETIRED")
    if inputs.computation_kind == "conceptual_pattern":
        return RecipeReadinessV1("CONCEPTUAL_ONLY")
    # governed_model_output recipes never enter the FORMULA ladder — BR-7A owns their states;
    # a caller folding one here gets the honest floor:
    if inputs.computation_kind == "governed_model_output":
        return RecipeReadinessV1("CONCEPTUAL_ONLY",
                                 blockers=("model_feature_spec_owns_readiness",))

    blockers = [*inputs.temporal_blockers, *inputs.binding_blockers,
                *inputs.governed_policy_blockers]
    if not inputs.reviewed_expectation:
        blockers.append(BLOCKER_NO_REVIEWED_EXPECTATION)
    if inputs.grammar_verdict != "ok":
        blockers.append(BLOCKER_GRAMMAR_UNSUPPORTED)
    if blockers:
        return RecipeReadinessV1("FORMULA_BLOCKED", blockers=tuple(blockers))
    if not inputs.gold_validated:
        return RecipeReadinessV1("FORMULA_AUTHORABLE", blockers=(BLOCKER_GOLD_UNPROVEN,))
    if inputs.engine_verdict is None:
        return RecipeReadinessV1("FORMULA_VALIDATED")
    if inputs.engine_verdict != "ok":
        return RecipeReadinessV1("MATERIALIZATION_BLOCKED",
                                 blockers=(BLOCKER_ENGINE_UNSUPPORTED,))
    return RecipeReadinessV1("MATERIALIZATION_READY")

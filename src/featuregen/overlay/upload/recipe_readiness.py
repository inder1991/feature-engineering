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


def fold_definition_readiness(definition, *, temporal_blockers: tuple[str, ...] = (),
                              binding_blockers: tuple[str, ...] = (),
                              governed_policy_blockers: tuple[str, ...] = (),
                              ) -> RecipeReadinessV1:
    """Task A6 — the fold over ONE ``RecipeDefinitionV2``'s own declarations, plus whatever the
    caller has actually MEASURED about this candidate.

    Three of the fold's inputs cannot be measured from a definition, and before A6 every caller
    that needed them assembled its own ``ReadinessInputsV1`` and answered the question its own
    way. They are stated ONCE here, so the planning lens, contract v3's execution block and the
    coverage report cannot disagree about how ready a recipe is:

    * ``reviewed_expectation`` — the definition's own ``formula.expectation_ref`` asked of the
      reviewed registry. A pack may not claim review by assertion.
    * ``grammar_verdict="ok"`` — BR-6's ``classify_formula_capability_v2`` classifies a
      PROPOSAL, and no proposal exists until authoring runs. Reading A2's derivation as a
      grammar verdict was measured and is indistinguishable on the shipped registry (all three
      registry anchors derive), so it would buy a third opinion and no new truth; C1/C2 is where
      a real verdict arrives.
    * ``gold_validated=False`` / ``engine_verdict=None`` — no gold or engine gate has run.
      ``None`` rests the ladder at ``FORMULA_VALIDATED``, which is this ladder's DOCUMENTED
      honest ceiling until C1, not a fudge.

    Whatever the caller passes in ``temporal_blockers`` / ``binding_blockers`` /
    ``governed_policy_blockers`` is a measurement it made — passed through verbatim, per BR-7.
    """
    from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
        has_reviewed_expectation,
    )

    formula = definition.formula
    return fold_readiness(ReadinessInputsV1(
        computation_kind=definition.computation_kind,
        retired=definition.readiness == "RETIRED",
        temporal_blockers=tuple(temporal_blockers),
        binding_blockers=tuple(binding_blockers),
        reviewed_expectation=(formula is not None
                              and has_reviewed_expectation(formula.expectation_ref)),
        grammar_verdict="ok",
        gold_validated=False,
        engine_verdict=None,
        governed_policy_blockers=tuple(governed_policy_blockers)))


def exceeds_fold(authored: str, folded: str) -> bool:
    """Is an AUTHORED readiness literal a stronger claim than what the fold actually produced?

    A6's registry law: the ``readiness=`` field on a definition is an assertion the fold must
    not contradict, never the answer. ``RETIRED`` is a terminal, not a rung — it compares only
    to itself. An unknown literal cannot be ranked and is therefore treated as exceeding, which
    is the fail-closed direction.
    """
    if authored == folded:
        return False
    if "RETIRED" in (authored, folded):
        return True
    if authored not in READINESS_LADDER or folded not in READINESS_LADDER:
        return True
    return READINESS_LADDER.index(authored) > READINESS_LADDER.index(folded)

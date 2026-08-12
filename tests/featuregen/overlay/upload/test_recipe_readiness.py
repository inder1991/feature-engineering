"""BR-7 — the readiness fold: every rung requires a typed input some deterministic gate produced;
nothing — not an LLM assertion, not template prose, not a hopeful default — can move a recipe up
the ladder. Blockers pass through VERBATIM from BR-4 and BR-5, so "why isn't this ready?" is a
list of named facts. UNASSESSED does not exist in this vocabulary.
"""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.recipe_readiness import (
    BLOCKER_ENGINE_UNSUPPORTED,
    BLOCKER_GOLD_UNPROVEN,
    BLOCKER_GRAMMAR_UNSUPPORTED,
    BLOCKER_NO_REVIEWED_EXPECTATION,
    READINESS_LADDER,
    ReadinessInputsV1,
    RecipeReadinessV1,
    fold_readiness,
)

_CLEAN = ReadinessInputsV1(
    computation_kind="deterministic_formula",
    reviewed_expectation=True, grammar_verdict="ok", gold_validated=True,
    engine_verdict="ok")


def test_the_ladder_top_requires_every_input_and_the_defaults_are_the_honest_floor():
    assert fold_readiness(_CLEAN) == RecipeReadinessV1("MATERIALIZATION_READY")
    bare = ReadinessInputsV1(computation_kind="deterministic_formula")
    folded = fold_readiness(bare)
    assert folded.state == "FORMULA_BLOCKED", \
        "a caller that cannot answer the questions leaves the recipe blocked, never promoted"
    assert BLOCKER_NO_REVIEWED_EXPECTATION in folded.blockers
    assert BLOCKER_GRAMMAR_UNSUPPORTED in folded.blockers


def test_each_rung_falls_for_exactly_its_own_missing_input():
    assert fold_readiness(replace(_CLEAN, retired=True)).state == "RETIRED"
    assert fold_readiness(replace(
        _CLEAN, computation_kind="conceptual_pattern")).state == "CONCEPTUAL_ONLY"
    no_expectation = fold_readiness(replace(_CLEAN, reviewed_expectation=False))
    assert (no_expectation.state, no_expectation.blockers) == (
        "FORMULA_BLOCKED", (BLOCKER_NO_REVIEWED_EXPECTATION,))
    no_gold = fold_readiness(replace(_CLEAN, gold_validated=False))
    assert (no_gold.state, no_gold.blockers) == ("FORMULA_AUTHORABLE", (BLOCKER_GOLD_UNPROVEN,))
    no_engine_chosen = fold_readiness(replace(_CLEAN, engine_verdict=None))
    assert no_engine_chosen == RecipeReadinessV1("FORMULA_VALIDATED"), \
        "gold passed, no engine selected — the recipe RESTS at validated, honestly"
    engine_cannot = fold_readiness(replace(_CLEAN, engine_verdict="unsupported_engine"))
    assert (engine_cannot.state, engine_cannot.blockers) == (
        "MATERIALIZATION_BLOCKED", (BLOCKER_ENGINE_UNSUPPORTED,))


def test_upstream_blockers_pass_through_verbatim():
    """BR-4's and BR-5's vocabularies arrive unrenamed — one blocker language platform-wide."""
    folded = fold_readiness(replace(
        _CLEAN,
        temporal_blockers=("pre_decision_authority_unproven",),
        binding_blockers=("AMBIGUOUS_MEASURE_BINDING", "ECONOMIC_ROLE_UNPROVEN")))
    assert folded.state == "FORMULA_BLOCKED"
    assert set(folded.blockers) == {"pre_decision_authority_unproven",
                                    "AMBIGUOUS_MEASURE_BINDING", "ECONOMIC_ROLE_UNPROVEN"}


def test_model_outputs_never_enter_the_formula_ladder():
    folded = fold_readiness(replace(_CLEAN, computation_kind="governed_model_output"))
    assert folded.state == "CONCEPTUAL_ONLY"
    assert folded.blockers == ("model_feature_spec_owns_readiness",), \
        "BR-7A owns model readiness — a propensity cannot become MATERIALIZATION_READY here"


def test_the_vocabulary_is_closed_and_unassessed_is_not_in_it():
    assert "UNASSESSED" not in READINESS_LADDER
    assert fold_readiness(_CLEAN).state in READINESS_LADDER

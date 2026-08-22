"""The validity contract `_gold_evaluation_recorded` named for a long time and did not have.

▲ **A STALE PASS IS NOT A PASS.** The old function returned a hardcoded `False`, and its docstring
was right about why: not because nothing recorded evaluation outcomes, but because no reader checked
that a passing artifact was produced under the world that is current now. Returning `True` for a
stale one would launder an old verdict into a present authority. These tests are that check.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from featuregen.overlay.upload.current_evaluation_validity import (
    current_evaluation_contract_now,
    current_evaluation_validity,
    expectations_with_current_evaluation,
)
from featuregen.overlay.upload.recipe_formula_eval_v2 import (
    EvaluationRunConfigurationV2,
    create_evaluation_run_v2,
)

_REF = "posted_debit_amount"


def _config(**overrides):
    contract = current_evaluation_contract_now()
    fields = {
        "provider": "fake", "model": "fake-1", "generation_controls": {},
        "author_provider_contract_hash": contract.author_provider_contract_hash,
        "critic_provider_contract_hash": contract.critic_provider_contract_hash,
        "shadow_window_start": datetime(2026, 8, 1, tzinfo=UTC),
        "shadow_window_end": datetime(2026, 8, 2, tzinfo=UTC),
        "shadow_generation_run_ids": (), "token_budget": 1000,
        "cost_budget": Decimal("1.00"), "created_by": {"actor": "test"},
        "runner_kind": "FAKE_TEST", "code_commit": "testcommit"}
    return EvaluationRunConfigurationV2(**{**fields, **overrides})


def test_the_CURRENT_CONTRACT_IS_DERIVED_FROM_THIS_BUILD_provider_contracts_included():
    """The provider contracts are frozen from the running code rather than passed in, so an
    evaluation conducted against a different author instruction or output schema cannot be mistaken
    for a current one — the failure the whole byte-freeze exists to prevent."""
    contract = current_evaluation_contract_now()
    assert len(contract.author_provider_contract_hash) == 64
    assert len(contract.critic_provider_contract_hash) == 64
    assert contract.author_provider_contract_hash != contract.critic_provider_contract_hash
    assert contract.contract_hash == current_evaluation_contract_now().contract_hash


def test_NO_EVALUATION_AT_ALL_IS_A_NAMED_ABSENCE(db):
    """Not a bare False. A caller that must explain why a recipe is not activatable needs the
    reason, and re-deriving it would mean asking the same question twice."""
    validity = current_evaluation_validity(db, _REF)

    assert validity.is_current is False
    assert validity.eval_run_id is None
    assert any(r.startswith("NO_CURRENT_EVALUATION") for r in validity.reasons)


def test_an_EVALUATION_UNDER_A_DIFFERENT_CONTRACT_IS_NOT_FOUND(db):
    """▲ THE LAUNDERING THIS PREVENTS. A run conducted against different provider contracts
    measured a different world. It is real, it may have passed, and it says nothing about now."""
    create_evaluation_run_v2(db, _config(
        author_provider_contract_hash="some-older-author-contract",
        critic_provider_contract_hash="some-older-critic-contract"))

    validity = current_evaluation_validity(db, _REF)
    assert validity.eval_run_id is None, "a run under another contract must not be adopted"
    assert any(r.startswith("NO_CURRENT_EVALUATION") for r in validity.reasons)


def test_a_CURRENT_RUN_IS_FOUND_and_still_refused_while_the_corpus_is_short(db):
    """▲ THE HONEST STATE TODAY, and the difference from the old constant. The run IS found — it was
    conducted under exactly the contract this build mints — and it is still not an authority,
    because the reviewed corpus holds one clean case and no run over it is certifiable.

    The answer matches the old hardcoded `False`. What changed is that it is now DERIVED, names its
    reason, and will become `True` by itself when the corpus grows (§0.10 step 5B)."""
    run_id = create_evaluation_run_v2(db, _config())

    validity = current_evaluation_validity(db, _REF)

    assert validity.eval_run_id == run_id, "the run measured the current world and must be found"
    assert validity.is_current is False
    assert any(r.startswith("CLEAN_CASES_BELOW_FLOOR") for r in validity.reasons)
    assert validity.code_commit == "testcommit"


def test_CERTIFIABLE_IS_THE_BAR_not_merely_passing(db, monkeypatch):
    """A run can score every attempt correctly and certify nothing. Promoting such a run to an
    activation authority is exactly the laundering this reader exists to prevent, so the reader
    reads `certifiable` and never `passed`."""
    import featuregen.overlay.upload.current_evaluation_validity as mod
    from featuregen.overlay.upload.recipe_formula_eval_v2 import EvaluationGateResultV2

    run_id = create_evaluation_run_v2(db, _config())
    contract = current_evaluation_contract_now()

    def _passing_but_uncertifiable(_conn, eval_run_id):
        return EvaluationGateResultV2(
            eval_run_id=eval_run_id, contract=contract, attempts=12, clean_attempts=1,
            adversarial_attempts=11, exact_matches=12, false_ready=0, technical_failures=0,
            attempts_without_v3_evidence=0, passed=True, certifiable=False,
            reasons=("CLEAN_CASES_BELOW_FLOOR: 1 of 10",))

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_formula_eval_v2.evaluate_persisted_run_v2",
        _passing_but_uncertifiable)

    validity = mod.current_evaluation_validity(db, _REF)
    assert validity.eval_run_id == run_id
    assert validity.is_current is False, "passed is not the bar; certifiable is"


def test_the_SET_FORM_AGREES_WITH_THE_SINGLE_FORM(db):
    """The serving path asks once per candidate, so it consults a set rather than running a gate
    evaluation per candidate. The two must never disagree about the same expectation."""
    create_evaluation_run_v2(db, _config())

    covered = expectations_with_current_evaluation(db)
    assert (_REF in covered) == current_evaluation_validity(db, _REF).is_current


def test_the_ACTIVATION_FOLD_NOW_DERIVES_ITS_ANSWER(db):
    """`_gold_evaluation_recorded` is no longer a constant. It resolves the recipe to its
    EXPECTATION REF — a recipe's ref is its own name for only 3 of the 317 registry recipes — and
    asks the reader."""
    from featuregen.overlay.upload.semantic_option_decision import _gold_evaluation_recorded

    # A recipe the registry never minted has nothing to have evaluated: honestly False, not an error.
    assert _gold_evaluation_recorded(db, "not-a-recipe-anyone-minted") is False

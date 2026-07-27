from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from featuregen.formula.critic import CriticFindingCode
from featuregen.overlay.upload.recipe_formula_eval import (
    EvaluationRunConfiguration,
    FormulaEvaluationIntegrityError,
    create_evaluation_run,
    evaluate_persisted_run,
    expectation_registry_hash,
)
from featuregen.overlay.upload.recipe_formula_gold import (
    CORPUS_CONTENT_HASH,
    FORMULA_GOLD_CASES,
    validate_formula_gold_corpus,
)


def test_formula_gold_corpus_is_non_vacuous_and_covers_blocking_codes() -> None:
    validate_formula_gold_corpus()
    assert len(CORPUS_CONTENT_HASH) == 64
    assert len({case.case_id for case in FORMULA_GOLD_CASES}) == len(FORMULA_GOLD_CASES)
    for recipe_id in ("merchant_mcc_diversity", "obligor_facility_count"):
        assert sum(
            case.case_kind == "clean" and case.recipe_id == recipe_id
            for case in FORMULA_GOLD_CASES
        ) == 10
    expected_blocking = {
        CriticFindingCode.MISSING_REQUIRED_OPERAND.value,
        CriticFindingCode.WRONG_SLOT_DIRECTION.value,
        CriticFindingCode.FILTER_INTENT_MISMATCH.value,
        CriticFindingCode.WINDOW_INTENT_MISMATCH.value,
    }
    assert {
        case.expected.get("blocking_code")
        for case in FORMULA_GOLD_CASES
        if case.expected.get("blocking_code")
    } == expected_blocking


def test_formula_eval_tables_are_write_once(db) -> None:
    tables = {
        "recipe_formula_eval_run",
        "recipe_formula_eval_case",
        "recipe_formula_eval_attempt",
        "recipe_formula_eval_artifact",
    }
    rows = db.execute(
        "SELECT event_object_table FROM information_schema.triggers "
        "WHERE trigger_name LIKE 'recipe_formula_eval_%_no_mutation'"
    ).fetchall()
    assert {row[0] for row in rows} == tables


def _configuration(*, runner_kind: str) -> EvaluationRunConfiguration:
    now = datetime.now(UTC)
    return EvaluationRunConfiguration(
        provider="test-provider",
        model="test-model",
        generation_controls={"temperature": 0},
        author_provider_contract_hash="a" * 64,
        critic_provider_contract_hash="b" * 64,
        shadow_window_start=now - timedelta(days=1),
        shadow_window_end=now,
        shadow_generation_run_ids=("generation-test",),
        token_budget=10_000,
        cost_budget=Decimal("100"),
        created_by={"subject": "test:evaluator"},
        runner_kind=runner_kind,
        code_commit="c" * 40,
    )


def test_create_eval_run_freezes_exact_corpus_and_registry(db) -> None:
    run_id = create_evaluation_run(
        db, _configuration(runner_kind="FAKE_TEST"), eval_run_id="eval-freeze")
    run = db.execute(
        "SELECT corpus_content_hash,expectation_registry_hash,runner_kind "
        "FROM recipe_formula_eval_run WHERE eval_run_id=%s",
        (run_id,),
    ).fetchone()
    assert run == (CORPUS_CONTENT_HASH, expectation_registry_hash(), "FAKE_TEST")
    cases = db.execute(
        "SELECT case_id,case_input_hash,expected_hash "
        "FROM recipe_formula_eval_case WHERE eval_run_id=%s",
        (run_id,),
    ).fetchall()
    assert len(cases) == len(FORMULA_GOLD_CASES)
    assert all(len(row[1]) == 64 and len(row[2]) == 64 for row in cases)


def test_fake_or_incomplete_eval_cannot_produce_provider_artifact(db) -> None:
    fake_id = create_evaluation_run(
        db, _configuration(runner_kind="FAKE_TEST"), eval_run_id="eval-fake")
    with pytest.raises(FormulaEvaluationIntegrityError, match="FAKE_TEST"):
        evaluate_persisted_run(db, fake_id)

    real_id = create_evaluation_run(
        db, _configuration(runner_kind="REAL_PROVIDER"), eval_run_id="eval-real-empty")
    with pytest.raises(FormulaEvaluationIntegrityError, match="missing or undeclared"):
        evaluate_persisted_run(db, real_id)
    assert db.execute(
        "SELECT 1 FROM recipe_formula_eval_artifact WHERE eval_run_id=%s",
        (real_id,),
    ).fetchone() is None


def test_eval_case_hash_tampering_is_detected_before_scoring(db) -> None:
    run_id = create_evaluation_run(
        db, _configuration(runner_kind="REAL_PROVIDER"), eval_run_id="eval-tamper")
    db.execute("DROP TRIGGER recipe_formula_eval_case_no_mutation "
               "ON recipe_formula_eval_case")
    db.execute(
        "UPDATE recipe_formula_eval_case SET expected_hash=%s "
        "WHERE eval_run_id=%s AND case_id=%s",
        ("0" * 64, run_id, FORMULA_GOLD_CASES[0].case_id),
    )
    with pytest.raises(FormulaEvaluationIntegrityError, match="does not verify"):
        evaluate_persisted_run(db, run_id)

from __future__ import annotations

from decimal import Decimal

import pytest

from featuregen.intake.llm import (
    PROVIDER_OK,
    LLMRequest,
    LLMResult,
)
from featuregen.intake.redaction import INPUT_KEY_INTENT
from featuregen.overlay.upload.taxonomy.gold_recognition import TARGET_GOLD
from featuregen.overlay.upload.taxonomy.recognition_release_eval import (
    RecognitionEvaluationConfiguration,
    RecognitionEvaluationIntegrityError,
    _wilson_upper,
    create_evaluation_run,
    evaluate_persisted_run,
    execute_evaluation_run,
)


class _OracleClient:
    def call(self, request: LLMRequest) -> LLMResult:
        instruction = str(request.inputs.get(INPUT_KEY_INTENT, ""))
        case = next(case for case in TARGET_GOLD if case.hypothesis in instruction)
        return LLMResult(
            output={
                "status": "classified",
                "candidates": [{
                    "use_case_id": case.expected_primary,
                    "relationship": "primary",
                    "confidence": "high",
                    "evidence_spans": [case.hypothesis[:40]],
                    "rationale": "oracle fixture",
                }],
                "ambiguity_note": None,
            },
            self_reported_scores={},
            call_ref="",
            status=PROVIDER_OK,
            cost_metadata={
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_amount": "0.001",
            },
        )


def _configuration(runner_kind: str) -> RecognitionEvaluationConfiguration:
    return RecognitionEvaluationConfiguration(
        runner_kind=runner_kind,
        stability_case_count=2,
        repeat_count=1,
        token_budget=10_000,
        cost_budget=Decimal("10"),
        created_by={"subject": "test:evaluator"},
        code_commit="c" * 40,
    )


def test_fake_runner_executes_exact_denominator_but_cannot_qualify(db) -> None:
    run_id = create_evaluation_run(
        db, _configuration("FAKE_TEST"), eval_run_id="recognition-eval-fake")
    assert execute_evaluation_run(db, run_id, _OracleClient()) == 102
    assert db.execute(
        "SELECT count(*) FROM recognition_eval_case WHERE eval_run_id=%s",
        (run_id,),
    ).fetchone()[0] == 100
    assert db.execute(
        "SELECT count(DISTINCT llm_call_ref) FROM recognition_eval_attempt "
        "WHERE eval_run_id=%s",
        (run_id,),
    ).fetchone()[0] == 102
    with pytest.raises(RecognitionEvaluationIntegrityError, match="FAKE_TEST"):
        evaluate_persisted_run(db, run_id)


def test_fake_configuration_cannot_claim_a_real_provider_run(db) -> None:
    with pytest.raises(
        RecognitionEvaluationIntegrityError, match="configured Anthropic"
    ):
        create_evaluation_run(
            db,
            _configuration("REAL_PROVIDER"),
            eval_run_id="recognition-eval-real",
        )


def test_recognition_evaluation_tables_are_write_once(db) -> None:
    tables = {
        "recognition_eval_run",
        "recognition_eval_case",
        "recognition_eval_attempt",
        "recognition_eval_artifact",
    }
    rows = db.execute(
        "SELECT event_object_table FROM information_schema.triggers "
        "WHERE trigger_name LIKE 'recognition_eval_%_no_mutation'"
    ).fetchall()
    assert {row[0] for row in rows} == tables


def test_zero_failures_over_100_meets_one_sided_wilson_gate() -> None:
    assert _wilson_upper(0, 100) <= 0.03
    assert _wilson_upper(1, 100) > 0.03

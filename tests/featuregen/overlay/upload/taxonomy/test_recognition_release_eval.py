from __future__ import annotations

from decimal import Decimal

import pytest

from featuregen.intake.llm import (
    PROVIDER_OK,
    LLMRequest,
    LLMResult,
)
from featuregen.intake.redaction import INPUT_KEY_INTENT
from featuregen.overlay.upload.taxonomy import recognition_release_eval as release_eval
from featuregen.overlay.upload.taxonomy import recognizer as recognizer_module
from featuregen.overlay.upload.taxonomy.gold_recognition import TARGET_GOLD
from featuregen.overlay.upload.taxonomy.recognition import RECOGNITION_VALIDATOR_VERSION
from featuregen.overlay.upload.taxonomy.recognition_release_eval import (
    SCHEMA_VERSION,
    SCORED_VALIDATOR_VERSION,
    RecognitionEvaluationConfiguration,
    RecognitionEvaluationIntegrityError,
    _project_served,
    _repair_turns,
    _served_json,
    _wilson_upper,
    create_evaluation_run,
    evaluate_persisted_run,
    execute_evaluation_run,
)
from featuregen.overlay.upload.taxonomy.recognizer import recognize_with_audit
from featuregen.overlay.upload.taxonomy.recognizer_prompt import PROMPT_VERSION


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


# ── Task 6 (2026-08-15): the gate measures the platform that exists ─────────────────────────────
#
# B3: the evaluator was pinned to schema v1 and scored by re-running the all-or-nothing validator
# over `llm_call.raw_output`, so a recognition the platform SERVED as a partial recovery — a real
# scope, on screen, confirmable — was counted as a technical failure.

_JUNK_SPAN = "   "          # the ONE candidate-local defect the frozen v2 schema accepts
_SECOND_LEAF = "credit.monitoring.limit_management"   # a real selectable leaf


def _candidate(use_case_id: str, *, relationship: str = "primary",
               evidence_spans: tuple[str, ...] = ("scripted evidence",)) -> dict:
    return {"use_case_id": use_case_id, "relationship": relationship, "confidence": "high",
            "evidence_spans": list(evidence_spans), "rationale": "scripted"}


def _body(*candidates: dict, status: str = "classified") -> dict:
    return {"status": status, "candidates": list(candidates), "ambiguity_note": None}


#: The 2026-08-15 incident, verbatim from the plan's §0 — a correct primary beside a candidate that
#: says "placeholder" in as many words, and two `"primary"` relationships where the rules allow one.
_LIVE_INCIDENT_BODY = {
    "status": "classified",
    "candidates": [
        {"use_case_id": "customer.relationship_attrition.churn", "relationship": "primary",
         "confidence": "high",
         "evidence_spans": ["predict churn in the next 90 days",
                            "customers whose transaction activity suddenly accelerates are about "
                            "to leave"],
         "rationale": ""},
        {"use_case_id": "x", "relationship": "primary", "confidence": "high",
         "evidence_spans": ["x"], "rationale": "placeholder"}],
    "modelling_contexts": [],
}


class _CaseScriptedClient:
    """The oracle, except for ONE case, which gets a scripted body on every turn (so a repair never
    succeeds and the platform's fail-open behaviour is what is measured)."""

    def __init__(self, case_id: str, body: dict) -> None:
        self._case_id = case_id
        self._body = body
        self._oracle = _OracleClient()

    def call(self, request: LLMRequest) -> LLMResult:
        instruction = str(request.inputs.get(INPUT_KEY_INTENT, ""))
        case = next(case for case in TARGET_GOLD if case.hypothesis in instruction)
        if case.id != self._case_id:
            return self._oracle.call(request)
        return LLMResult(
            output=dict(self._body), self_reported_scores={}, call_ref="", status=PROVIDER_OK,
            cost_metadata={"input_tokens": 10, "output_tokens": 5, "cost_amount": "0.001"})


def _attempt_for(db, run_id: str, case_id: str) -> dict:
    row = db.execute(
        "SELECT recognition_json, status, technical_failure, abstained, false_narrowing, "
        "recognized_primary, llm_call_ref FROM recognition_eval_attempt "
        "WHERE eval_run_id=%s AND case_id=%s AND repeat_index=0", (run_id, case_id)).fetchone()
    assert row is not None, f"no attempt recorded for {case_id}"
    return {"recognition_json": row[0], "status": row[1], "technical_failure": row[2],
            "abstained": row[3], "false_narrowing": row[4], "recognized_primary": row[5],
            "llm_call_ref": row[6]}


def test_the_evaluator_pins_the_contract_the_platform_dispatches() -> None:
    """A pinned LITERAL, drift-tested — not a read of the recognizer's constant. A derived value
    would follow a contract change silently and keep reporting; this makes the change fail here, so
    a human decides whether the evaluator still measures what it claims to."""
    assert SCHEMA_VERSION == recognizer_module._OUTPUT_SCHEMA_VERSION, (
        "the platform dispatches a recognition schema this evaluator was not written for; "
        "re-review the scoring, then move the pin")
    assert SCORED_VALIDATOR_VERSION == RECOGNITION_VALIDATOR_VERSION, (
        "what the recognizer ACCEPTS has changed, so what 'a correct recognition' means has "
        "changed; re-review the scoring, then move the pin")


def test_a_run_recorded_under_another_contract_is_not_scored(db, monkeypatch) -> None:
    """The versioned refusal. A run stamped with a different contract is not scored at all — a
    number carrying the wrong contract is worse than no number, because it looks like evidence.
    Asserted BEFORE the fake-run refusal, so an off-contract run cannot be diagnosed as anything
    else first."""
    monkeypatch.setattr(release_eval, "SCHEMA_VERSION", 1)
    run_id = create_evaluation_run(
        db, _configuration("FAKE_TEST"), eval_run_id="recognition-eval-old-contract")
    monkeypatch.undo()
    with pytest.raises(RecognitionEvaluationIntegrityError) as excinfo:
        evaluate_persisted_run(db, run_id)
    message = str(excinfo.value)
    assert "this evaluator scores recognition contract" in message
    assert f"v{SCHEMA_VERSION}" in message and "v1" in message
    assert "FAKE_TEST" not in message      # the contract is decided first, and says so


def test_a_partial_recovery_is_scored_as_a_partial_recovery_not_a_technical_failure(db) -> None:
    """B3, closed. The model never fixed its sloppy sibling, the partition kept the valid candidate,
    and the user got a confirmable scope — so the attempt is a SUCCESS with a loss recorded, not the
    technical failure the old evaluator counted by re-running the all-or-nothing validator over the
    raw body (which rejects that body by construction — that is what made it a partial recovery)."""
    case = TARGET_GOLD[0]
    body = _body(_candidate(case.expected_primary),
                 _candidate(_SECOND_LEAF, relationship="secondary",
                            evidence_spans=(_JUNK_SPAN,)))
    run_id = create_evaluation_run(
        db, _configuration("FAKE_TEST"), eval_run_id="recognition-eval-partial")
    execute_evaluation_run(db, run_id, _CaseScriptedClient(case.id, body))

    attempt = _attempt_for(db, run_id, case.id)
    assert attempt["technical_failure"] is False
    assert attempt["status"] == "classified"
    assert attempt["recognized_primary"] == case.expected_primary
    assert attempt["abstained"] is False
    assert attempt["false_narrowing"] is False       # the surviving scope still retains the recipes
    served = attempt["recognition_json"]
    assert served["quality"]["disposition"] == "partially_recovered"
    assert served["quality"]["dropped_candidate_count"] == 1
    assert served["quality"]["drop_reason_codes"] == ["MALFORMED_EVIDENCE_SPANS"]
    assert served["quality"]["repair_attempts"] == 2  # the budget WAS spent before anything was cut
    assert [c["use_case_id"] for c in served["candidates"]] == [case.expected_primary]
    assert served["dropped_candidates"] == [{"index": 1, "reason_code": "MALFORMED_EVIDENCE_SPANS"}]


def test_the_live_incident_replays_as_an_explained_failure(db) -> None:
    """The 2026-08-15 body itself, as a fixture case. On today's platform it is still a failure —
    two `primary` candidates is an aggregate defect the partition refuses on purpose, and the model
    never fixed it — but it is no longer a SILENT one: the model was asked twice, the audit row says
    so, and the evaluation records a technical failure with its repair turns rather than an
    unexplained discard. (Task 3's repair is what rescues this body when the model complies;
    `test_the_padded_body_from_the_live_incident_now_recognises` pins that half.)"""
    case = TARGET_GOLD[1]
    run_id = create_evaluation_run(
        db, _configuration("FAKE_TEST"), eval_run_id="recognition-eval-incident")
    execute_evaluation_run(db, run_id, _CaseScriptedClient(case.id, _LIVE_INCIDENT_BODY))

    attempt = _attempt_for(db, run_id, case.id)
    assert attempt["technical_failure"] is True
    assert attempt["status"] == "technical_failure"
    assert attempt["recognized_primary"] is None
    # Fail-open: nothing narrowed, so nothing was lost to a narrowing either.
    assert attempt["false_narrowing"] is False
    served = attempt["recognition_json"]
    assert served["candidates"] == []                       # no scope is invented from that body
    assert served["quality"]["disposition"] == "technical_failure"
    assert served["quality"]["repair_attempts"] == 2        # the model WAS asked, twice
    ledger = db.execute("SELECT repair_attempts FROM llm_call WHERE llm_call_ref=%s",
                        (attempt["llm_call_ref"],)).fetchone()[0]
    assert [entry["class"] for entry in ledger] == ["repair", "repair"]
    # The complaint names a RULE, never the model's own text — the incident's `"x"` / "placeholder"
    # never re-enter a prompt or a stored row.
    assert all("placeholder" not in entry["reason"] and "'x'" not in entry["reason"]
               for entry in ledger)


@pytest.mark.parametrize("body", [
    _body(_candidate(TARGET_GOLD[0].expected_primary)),                       # clean
    _body(_candidate(TARGET_GOLD[0].expected_primary),
          _candidate(_SECOND_LEAF, relationship="secondary",
                     evidence_spans=(_JUNK_SPAN,))),                          # partial recovery
    _body(_candidate(TARGET_GOLD[0].expected_primary), _candidate(_SECOND_LEAF)),  # two primaries
    _LIVE_INCIDENT_BODY,                                                      # schema-invalid id
])
def test_the_served_result_is_re_derivable_from_the_audit_row(db, body) -> None:
    """The integrity half of scoring the served result, proved the only way that means anything:
    the re-derivation is compared to what the PLATFORM actually served for the same body, not to a
    second opinion about it. If these two could disagree, the evaluator would be back to measuring
    a platform of its own imagination — which is exactly what B3 was."""
    audited = recognize_with_audit(
        db, _CaseScriptedClient(TARGET_GOLD[0].id, body),
        redacted_hypothesis=TARGET_GOLD[0].hypothesis,
        redacted_goal=TARGET_GOLD[0].prediction_goal)
    raw = db.execute("SELECT raw_output, repair_attempts FROM llm_call WHERE llm_call_ref=%s",
                     (audited.llm_call_ref,)).fetchone()

    result, quality = _project_served(
        db, body=raw[0].get("output"), repair_turns=_repair_turns(raw[1]),
        model=audited.result.recognizer_model_id)
    assert _served_json(result, quality) == _served_json(audited.result, audited.quality)


def test_a_retry_is_not_counted_as_a_repair() -> None:
    """The repair RATE is a claim about how often the MODEL got it wrong. A truncated response
    re-requested is not a correction, and counting it as one would inflate a number an operator is
    about to budget from."""
    assert _repair_turns([{"class": "repair", "reason": "X"},
                          {"class": "retry", "reason": "max_tokens"},
                          {"class": "repair", "reason": "Y"}]) == 2
    assert _repair_turns([{"class": "retry", "reason": "transient"}]) == 0
    assert _repair_turns(None) == 0 and _repair_turns(["junk"]) == 0


# ── the report an operator reads ────────────────────────────────────────────────────────────────
#
# `evaluate_persisted_run` only ever scores a REAL_PROVIDER run, and a real provider run is the
# operator action this task deliberately does not take — so the ARITHMETIC lives in `_score_report`,
# a pure function over already-verified rows, and is exercised here against hand-built ones. The
# evidence checks stay where they are: a fake run still cannot produce a provider-qualified artifact.

def _quality(disposition: str, *, repairs: int = 0, dropped: int = 0) -> dict:
    return {"disposition": disposition, "repair_attempts": repairs,
            "dropped_candidate_count": dropped,
            "drop_reason_codes": ["MALFORMED_EVIDENCE_SPANS"] if dropped else []}


def _row(case, *, repeat_index: int = 0, quality: dict | None = None,
         technical: bool = False, abstained: bool = False) -> dict:
    return {
        "case_id": case.id,
        "repeat_index": repeat_index,
        "llm_call_ref": f"llmc_{case.id}_{repeat_index}",
        "recognition_json": {
            "status": "classified",
            "candidates": [{"use_case_id": case.expected_primary}],
            "quality": quality or _quality("clean"),
        },
        "status": "classified",
        "recognized_primary": case.expected_primary,
        "technical_failure": technical,
        "abstained": abstained,
        "false_narrowing": False,
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_amount": Decimal("0.001"),
    }


def _perfect_run(partial_case_index: int | None = None) -> dict:
    """100 primary observations, all correct — optionally with ONE of them a partial recovery."""
    attempts = []
    repair_turns: dict[str, int] = {}
    for index, case in enumerate(TARGET_GOLD):
        quality = (_quality("partially_recovered", repairs=2, dropped=1)
                   if index == partial_case_index else _quality("clean"))
        row = _row(case, quality=quality)
        repair_turns[row["llm_call_ref"]] = quality["repair_attempts"]
        attempts.append(row)
    by_case: dict[str, list[dict]] = {}
    for row in attempts:
        by_case.setdefault(row["case_id"], []).append(row)
    return {
        "eval_run_id": "recognition-eval-scored",
        "run": {"stability_case_count": 0, "repeat_count": 0, "token_budget": 10_000,
                "cost_budget": Decimal("10")},
        "attempts": attempts,
        "primary": attempts,
        "by_case": by_case,
        "case_by_id": {case.id: case for case in TARGET_GOLD},
        "repair_turns_by_ref": repair_turns,
        "retained_relevant": 40,
        "total_relevant": 40,
    }


def test_a_partially_recovered_run_passes_and_is_not_counted_as_a_failure() -> None:
    """THE acceptance. A partial recovery is a recognition the platform SERVED — the user had a
    scope to confirm — so it counts as one, and the run passes. The old evaluator scored the same
    evidence as a technical failure and would have failed this release."""
    report = release_eval._score_report(**_perfect_run(partial_case_index=3))

    assert report["technical_failures"] == 0
    assert report["partially_recovered"] == 1
    assert report["dropped_candidates_total"] == 1
    assert report["passed"] is True
    assert report["applicability_recall"] == 1.0
    assert report["false_narrowings"] == 0


def test_the_report_measures_the_repair_rate_and_does_not_gate_on_it() -> None:
    """Repairs are the new cost: since the frozen enum and the semantic validator inside the loop, a
    body the platform used to discard whole is re-prompted instead. The rate is REPORTED so an
    operator can budget from it; what rate is acceptable is their judgement about cost and model
    choice, and this plan has not made it — so a run with repairs still passes on its own merits."""
    report = release_eval._score_report(**_perfect_run(partial_case_index=7))

    assert report["attempts"] == 100
    assert report["attempts_with_repair"] == 1
    assert report["repair_turns_total"] == 2
    assert report["repair_rate"] == 0.01
    assert report["passed"] is True                      # measured, not gated

    clean = release_eval._score_report(**_perfect_run())
    assert clean["repair_rate"] == 0.0 and clean["attempts_with_repair"] == 0


def test_the_report_names_the_contract_it_measured() -> None:
    """B3's lesson as a field. An evaluation is a claim about a PLATFORM; a report that does not say
    which one is how a gate comes to certify a contract production no longer runs."""
    report = release_eval._score_report(**_perfect_run())

    assert report["evaluator_version"] == "recognition-release-evaluator-v2"
    assert report["schema_id"] == "use_case_recognition"
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["semantic_validator_version"] == SCORED_VALIDATOR_VERSION
    assert report["prompt_version"] == int(PROMPT_VERSION)


def test_a_technical_failure_still_fails_the_run() -> None:
    """The partition rescues a valid answer from a sloppy sibling; it does not rescue a run from a
    recognition that produced nothing. Scoring the served result loosens what counts as a failure —
    it must not loosen what counts as a pass."""
    scored = _perfect_run()
    scored["attempts"][0] = {**scored["attempts"][0], "technical_failure": True,
                             "status": "technical_failure", "recognized_primary": None}
    scored["primary"] = scored["attempts"]
    scored["by_case"][TARGET_GOLD[0].id] = [scored["attempts"][0]]
    report = release_eval._score_report(**scored)

    assert report["technical_failures"] == 1
    assert report["passed"] is False


def test_the_recorded_evidence_matches_the_frozen_provider_contract(db) -> None:
    """**The "is the gate runnable?" proof, without running it.** `evaluate_persisted_run` refuses
    any attempt whose `llm_call` disagrees with the run's frozen contract, and BOTH sides of that
    comparison were wrong: the run stamped `int(PROMPT_VERSION)` (3) and schema v1, while the
    recognizer's dispatch threaded neither prompt version (the seam defaults to 1) nor — until Task
    1 — the schema version. So the 100-case provider gate would have raised "recognition attempt
    differs from the frozen provider contract" on its FIRST attempt, after paying for it, against
    every model. This runs the evaluator's own comparison over evidence a fake client produced."""
    run_id = create_evaluation_run(
        db, _configuration("FAKE_TEST"), eval_run_id="recognition-eval-contract")
    execute_evaluation_run(db, run_id, _OracleClient())

    run = db.execute(
        "SELECT provider,model,prompt_id,prompt_version,schema_id,schema_version,"
        "generation_controls FROM recognition_eval_run WHERE eval_run_id=%s", (run_id,)).fetchone()
    calls = db.execute(
        "SELECT c.task,c.provider,c.model,c.prompt_id,c.prompt_version,c.output_schema_id,"
        "c.output_schema_version,c.generation_settings FROM recognition_eval_attempt a "
        "JOIN llm_call c ON c.llm_call_ref = a.llm_call_ref WHERE a.eval_run_id=%s",
        (run_id,)).fetchall()

    assert len(calls) == 102
    expected = ("use_case_recognition", *run)
    mismatched = [call for call in calls if tuple(call) != expected]
    assert not mismatched, (
        f"the gate would refuse its own evidence: run={expected}, first attempt={mismatched[0]}")
    assert run[3] == int(PROMPT_VERSION) == 3      # the prompt version actually dispatched
    assert run[5] == SCHEMA_VERSION == 2           # …and the frozen schema actually dispatched


def test_the_gate_command_builds_its_own_provider_client(monkeypatch) -> None:
    """The THIRD thing that made the documented gate command unrunnable: `main` asked for
    `current_llm_client()`, and nothing in that process ever registers one — only `featuregen
    worker` does. `python -m …recognition_release_eval run` therefore died with "no LLMClient
    registered" before it reached the provider. It now builds the adapter explicitly and FAILS
    CLOSED when it is not configured: an evaluation that quietly ran against something else would be
    worse than one that did not run."""
    assert not hasattr(release_eval, "current_llm_client"), (
        "the gate is back on a process-wide client nothing registers")
    monkeypatch.delenv("FEATUREGEN_LLM_PROVIDER", raising=False)
    with pytest.raises(RecognitionEvaluationIntegrityError,
                       match="FEATUREGEN_LLM_PROVIDER=anthropic"):
        release_eval._provider_client()


def test_the_gate_command_documents_what_it_spends() -> None:
    """`run` spends real money against a real provider, and the number of calls is not 100 — it is
    100 plus the stability repeats, each of which may cost repair turns. The arithmetic lives in
    `--help`, where the operator about to spend it is looking, rather than only in a plan file."""
    epilog = release_eval._SPEND_EPILOG
    assert "CALLS A REAL PROVIDER" in epilog
    assert "110" in epilog and "330" in epilog          # floor and ceiling, both stated
    assert "repair turns (budget 2)" in epilog
    assert release_eval._parser().epilog == epilog

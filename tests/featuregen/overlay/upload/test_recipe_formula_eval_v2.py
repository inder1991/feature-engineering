"""The V2/V3 evaluation lane — what it scores, what it refuses, and what it will not claim.

The scoring tests are pure and read like a specification, which is the point of `derive_outcome_v2`
being pure: every interesting question ("does a V3 run ending RESOLVED count as a pass?") is
answerable without a database, an authoring run or a provider.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from featuregen.overlay.upload.recipe_formula_eval_v2 import (
    CLEAN_TERMINAL_DISPOSITION,
    OUTCOME_KEYS_V2,
    EvaluationRunConfigurationV2,
    FormulaEvaluationIntegrityErrorV2,
    create_evaluation_run_v2,
    derive_outcome_v2,
    evaluate_persisted_run_v2,
    record_evaluation_attempt_v2,
)
from featuregen.overlay.upload.recipe_formula_gold_v2 import formula_gold_v2_cases

_PIN = "093de7a0e954122f2e0e5706eea9af65ec18b42ddb7b29f4dbd9860911930bbc"


def _clean_case():
    return next(c for c in formula_gold_v2_cases() if c.case_kind == "clean")


def _adversarial_case(refusal_class="schema_error"):
    return next(c for c in formula_gold_v2_cases()
                if c.case_kind == "adversarial"
                and c.expected["refusal_class"] == refusal_class)


def _good_v3_result(**overrides):
    return {
        "authoring_disposition": CLEAN_TERMINAL_DISPOSITION,
        "output_status": "deferred_to_compiler",
        "structural_status": "ok",
        "capability_status": "ok",
        "expectation_status": "match",
        "technical_status": "ok",
        **overrides}


def _config(**overrides):
    return EvaluationRunConfigurationV2(
        provider="fake", model="fake-1", generation_controls={"temperature": 0},
        author_provider_contract_hash="author-1", critic_provider_contract_hash="critic-1",
        shadow_window_start=datetime(2026, 8, 1, tzinfo=UTC),
        shadow_window_end=datetime(2026, 8, 2, tzinfo=UTC),
        shadow_generation_run_ids=(), token_budget=1000, cost_budget=Decimal("1.00"),
        created_by={"actor": "test"}, runner_kind="FAKE_TEST", code_commit="testcommit",
        **overrides)


# ══ THE INVERSION THIS LANE EXISTS TO PREVENT ══════════════════════════════════════════════════
def test_a_CORRECT_V3_RUN_ENDS_READY_FOR_OUTPUT_BINDING_and_that_is_a_pass():
    """▲ NOT `RESOLVED`. A V3 run captures the author's intent and stops; the compiler resolves
    output authority (C-A7). This is the positive half of the inversion."""
    outcome = derive_outcome_v2(
        case=_clean_case(), result=_good_v3_result(),
        candidate_proposal_hash=_PIN, conducted_under_v3=True, artifact_is_v3=True)

    assert outcome["preservation_ok"] is True
    assert outcome["reproduced_reviewed_formula"] is True
    assert outcome["exact_match"] is True
    assert outcome["false_ready"] is False


def test_a_V3_RUN_THAT_RESOLVED_ITS_OWN_OUTPUT_IS_A_FAILURE_not_a_pass():
    """▲ THE NEGATIVE HALF, and the one an evaluator written against V1 would get backwards. A run
    ending `RESOLVED` took a decision the compiler owns. It is ACCEPTED — so it counts as
    `false_ready` — and it is not a correct capture."""
    outcome = derive_outcome_v2(
        case=_clean_case(),
        result=_good_v3_result(authoring_disposition="RESOLVED", output_status="resolved"),
        candidate_proposal_hash=_PIN, conducted_under_v3=True, artifact_is_v3=True)

    assert outcome["accepted"] is True
    assert outcome["preservation_ok"] is False
    assert outcome["false_ready"] is True
    assert outcome["exact_match"] is False


# ══ V3 EVIDENCE IS PART OF THE SCORE ═══════════════════════════════════════════════════════════
def test_a_PERFECT_ATTEMPT_THAT_IS_NOT_V3_EVIDENCE_DOES_NOT_MATCH():
    """An attempt whose artifact does not genuinely parse as V3 cannot demonstrate v3 authoring
    quality however good it looks. Folded into `exact_match` rather than left as a note beside it,
    so no caller can read past it."""
    outcome = derive_outcome_v2(
        case=_clean_case(), result=_good_v3_result(),
        candidate_proposal_hash=_PIN, conducted_under_v3=True, artifact_is_v3=False)

    assert outcome["preservation_ok"] is True
    assert outcome["reproduced_reviewed_formula"] is True
    assert outcome["exact_match"] is False, "v3 evidence is not optional for a v3 pass"


def test_REPRODUCING_A_DIFFERENT_FORMULA_IS_NOT_A_MATCH():
    """`preservation_ok` says the run behaved correctly; reproduction says it produced the REVIEWED
    formula. A run can do the first without the second, and only the pair is a match."""
    outcome = derive_outcome_v2(
        case=_clean_case(), result=_good_v3_result(),
        candidate_proposal_hash="f" * 64, conducted_under_v3=True, artifact_is_v3=True)

    assert outcome["preservation_ok"] is True
    assert outcome["reproduced_reviewed_formula"] is False
    assert outcome["exact_match"] is False


# ══ THE ADVERSARIAL HALF, BY CLASS ═════════════════════════════════════════════════════════════
def test_a_SCHEMA_ERROR_CASE_IS_SATISFIED_ONLY_BY_A_STRUCTURAL_REFUSAL():
    case = _adversarial_case("schema_error")
    caught = derive_outcome_v2(
        case=case,
        result=_good_v3_result(authoring_disposition="INVALID_FORMULA", structural_status="invalid"),
        # artifact_is_v3=False is not a defect here — a malformed proposal CANNOT parse as V3, and
        # that is exactly what the case asked the platform to catch.
        candidate_proposal_hash=None, conducted_under_v3=True, artifact_is_v3=False)
    assert caught["refusal_detected"] is True
    assert caught["exact_match"] is True
    assert caught["false_ready"] is False


def test_a_MALFORMED_PROPOSAL_CAUGHT_BY_THE_WRONG_GATE_IS_NOT_A_PASS():
    """▲ BY CLASS, never by "something went wrong". A malformed proposal stopped at the CAPABILITY
    gate means the structural check missed it — a different defect, with a different owner, from the
    one the case was written to detect. Scoring it as a pass would hide exactly that."""
    outcome = derive_outcome_v2(
        case=_adversarial_case("schema_error"),
        result=_good_v3_result(authoring_disposition="CAPABILITY_UNPROVEN",
                               capability_status="unproven"),
        candidate_proposal_hash=None, conducted_under_v3=True, artifact_is_v3=False)

    assert outcome["refusal_detected"] is False
    assert outcome["exact_match"] is False


def test_an_ADVERSARIAL_CASE_THAT_WAS_ACCEPTED_IS_FALSE_READY():
    outcome = derive_outcome_v2(
        case=_adversarial_case("schema_error"), result=_good_v3_result(),
        candidate_proposal_hash=_PIN, conducted_under_v3=True, artifact_is_v3=True)
    assert outcome["accepted"] is True
    assert outcome["false_ready"] is True
    assert outcome["exact_match"] is False


def test_the_OUTCOME_VOCABULARY_IS_FIXED():
    """A dictionary whose keys varied by case would make "how often did this pass" unanswerable
    across a run."""
    for case in (_clean_case(), _adversarial_case()):
        outcome = derive_outcome_v2(
            case=case, result=_good_v3_result(), candidate_proposal_hash=_PIN,
            conducted_under_v3=True, artifact_is_v3=True)
        assert set(outcome) == OUTCOME_KEYS_V2


# ══ THE RUN, PERSISTED ═════════════════════════════════════════════════════════════════════════
def test_CREATING_A_RUN_FREEZES_THE_CONTRACT_AND_EVERY_CASE(db):
    run_id = create_evaluation_run_v2(db, _config())

    contract_hash, corpus = db.execute(
        "SELECT evaluation_contract_hash, corpus_version FROM recipe_formula_eval_run "
        "WHERE eval_run_id=%s", (run_id,)).fetchone()
    assert contract_hash is not None, "a V2 run must cite the identity it was conducted under"
    assert corpus == "recipe-formula-gold-v2"

    cases = db.execute(
        "SELECT case_kind, subject_kind FROM recipe_formula_eval_case_v2 WHERE eval_run_id=%s",
        (run_id,)).fetchall()
    assert len(cases) == len(formula_gold_v2_cases())
    # The pairing migration 1098 enforces, observed on real rows rather than assumed.
    assert {(k, s) for k, s in cases} == {("clean", "expectation_ref"),
                                          ("adversarial", "gold_fixture")}


def test_a_RUN_CREATED_OVER_A_SHORT_CORPUS_STILL_RUNS(db):
    """▲ Deliberate. A run over one reviewed case still produces real per-case evidence, and
    refusing to gather it would leave the lane unexercised until governance caught up — while the
    §0.5 transition needs it RUN before it can be made the only lane. What must not happen is that
    such a run reads as certification, which is the next test."""
    assert create_evaluation_run_v2(db, _config())


def _passing_rows(clean: int = 1, adversarial: int = 11):
    """Attempts as they look when everything went right — the shape `summarise_attempts_v2` reads.

    Built rather than authored, because producing twelve genuinely-qualifying V3 runs would need
    twelve real provider-authored runs against a durable database. That proof exists and lives
    where it belongs (`test_durable_v3_evidence.py`); what is under test HERE is the verdict logic,
    which is pure precisely so it can be examined without any of that.
    """
    good = {"exact_match": True, "false_ready": False, "technical_failure": False,
            "conducted_under_v3": True}
    return (*(("clean", good, True) for _ in range(clean)),
            # ▲ `v3_evidence` False on the adversarial rows is CORRECT, not a shortcut: their
            # artifacts are malformed on purpose, so the whole-run flag cannot be true. The conduct
            # half is what they are held to.
            *(("adversarial", good, False) for _ in range(adversarial)))


def test_a_PERFECT_RUN_OVER_A_SHORT_CORPUS_PASSES_AND_CERTIFIES_NOTHING(db):
    """▲ THE POINT OF SEPARATING `passed` FROM `certifiable`. Every attempt came out right. One
    reviewed clean case does not demonstrate reliability, so the lane is still not certified — and
    the reason says so by name rather than leaving a caller to infer it."""
    from featuregen.overlay.upload.recipe_formula_eval_v2 import summarise_attempts_v2

    run_id = create_evaluation_run_v2(db, _config())
    contract = evaluate_persisted_run_v2(db, run_id).contract

    gate = summarise_attempts_v2(
        eval_run_id=run_id, contract=contract, rows=_passing_rows(),
        shortfalls=("CLEAN_CASES_BELOW_FLOOR: 1 of 10",))

    assert gate.attempts == 12
    assert gate.exact_matches == 12
    assert gate.false_ready == 0
    assert gate.attempts_without_v3_evidence == 0, (
        "an adversarial attempt is held to the conduct half, not to whole-run evidence")
    assert gate.passed is True
    assert gate.certifiable is False
    assert any(r.startswith("CLEAN_CASES_BELOW_FLOOR") for r in gate.reasons)


def test_a_PERFECT_RUN_OVER_AN_ADEQUATE_CORPUS_CERTIFIES(db):
    """The other side of the same separation, so `certifiable` is not merely always False. This is
    what the day after step 5B looks like: nothing about the code changes, the corpus does."""
    from featuregen.overlay.upload.recipe_formula_eval_v2 import summarise_attempts_v2

    run_id = create_evaluation_run_v2(db, _config())
    contract = evaluate_persisted_run_v2(db, run_id).contract

    gate = summarise_attempts_v2(
        eval_run_id=run_id, contract=contract, rows=_passing_rows(clean=10), shortfalls=())

    assert gate.passed is True
    assert gate.certifiable is True
    assert gate.reasons == ()


def test_ONE_FALSE_READY_ATTEMPT_SINKS_A_RUN(db):
    """A single accepted proposal that should have been refused is not a statistic to average
    away — it is the failure mode the adversarial half exists to detect."""
    from featuregen.overlay.upload.recipe_formula_eval_v2 import summarise_attempts_v2

    run_id = create_evaluation_run_v2(db, _config())
    contract = evaluate_persisted_run_v2(db, run_id).contract
    rows = (*_passing_rows(clean=10),
            ("adversarial", {"exact_match": False, "false_ready": True,
                             "technical_failure": False, "conducted_under_v3": True}, False))

    gate = summarise_attempts_v2(
        eval_run_id=run_id, contract=contract, rows=rows, shortfalls=())

    assert gate.passed is False
    assert gate.certifiable is False
    assert any(r.startswith("FALSE_READY") for r in gate.reasons)


def test_a_RUN_WITH_NO_ATTEMPTS_ESTABLISHES_NOTHING(db):
    gate = evaluate_persisted_run_v2(db, create_evaluation_run_v2(db, _config()))
    assert gate.passed is False
    assert gate.certifiable is False
    assert any(r.startswith("NO_ATTEMPTS") for r in gate.reasons)


def test_the_GATE_REFUSES_A_RUN_THAT_CITES_NO_CONTRACT(db):
    """▲ §0.5 step 5 in the making: a run that cannot say what it was conducted under is not V2/V3
    evidence. Reachable today because the column is nullable while the V1 lane still exists — which
    is exactly why the refusal lives in code now rather than waiting for the NOT NULL."""
    from featuregen.overlay.upload.recipe_formula_eval import (
        EvaluationRunConfiguration,
        create_evaluation_run,
    )

    v1_run = create_evaluation_run(db, EvaluationRunConfiguration(
        provider="fake", model="fake-1", generation_controls={},
        author_provider_contract_hash="a", critic_provider_contract_hash="c",
        shadow_window_start=datetime(2026, 8, 1, tzinfo=UTC),
        shadow_window_end=datetime(2026, 8, 2, tzinfo=UTC),
        shadow_generation_run_ids=(), token_budget=1000, cost_budget=Decimal("1.00"),
        created_by={"actor": "test"}, runner_kind="FAKE_TEST", code_commit="testcommit"))

    with pytest.raises(FormulaEvaluationIntegrityErrorV2, match="cites no evaluation contract"):
        evaluate_persisted_run_v2(db, v1_run)


def test_an_ATTEMPT_WITH_NO_AUTHORING_RUN_IS_NOT_V3_EVIDENCE(db):
    """There is nothing to qualify, so it does not qualify — recorded with the reason rather than
    defaulting to true and being noticed later."""
    run_id = create_evaluation_run_v2(db, _config())
    case = _clean_case()
    _record(db, run_id, case, 0, _good_v3_result(), authoring_run_id=None)

    evidence, problems = db.execute(
        "SELECT v3_evidence, v3_evidence_problems FROM recipe_formula_eval_attempt_v2 "
        "WHERE eval_run_id=%s", (run_id,)).fetchone()
    assert evidence is False
    assert problems == ["the attempt has no authoring run to qualify",
                        "the attempt produced no authoring run, so there is no artifact"]

    gate = evaluate_persisted_run_v2(db, run_id)
    assert gate.attempts_without_v3_evidence == 1
    assert any(r.startswith("NOT_V3_EVIDENCE") for r in gate.reasons)


def test_ATTEMPTS_ARE_WRITE_ONCE(db):
    """Migration 1098's guard. An evaluation whose attempts could be edited afterwards is a claim,
    not evidence."""
    run_id = create_evaluation_run_v2(db, _config())
    _record(db, run_id, _clean_case(), 0, _good_v3_result())

    with db.transaction(force_rollback=True), pytest.raises(Exception, match="write-once"):
        db.execute("UPDATE recipe_formula_eval_attempt_v2 SET disposition='RESOLVED' "
                   "WHERE eval_run_id=%s", (run_id,))
    with db.transaction(force_rollback=True), pytest.raises(Exception, match="write-once"):
        db.execute("DELETE FROM recipe_formula_eval_attempt_v2 WHERE eval_run_id=%s", (run_id,))


def _record(db, run_id, case, index, result, *, authoring_run_id=None):
    return record_evaluation_attempt_v2(
        db, eval_run_id=run_id, case=case, repeat_index=index,
        authoring_run_id=authoring_run_id, result=result,
        candidate_proposal_hash=case.expected.get("proposal_hash"))

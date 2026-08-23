"""§12 — the compiler programme: both comparisons, the honesty rules, and the closed loop.

The case worth reading last is the LOOP: a passing non-empty corpus mints a certificate that the
step-7 reader then finds — certification to production gate, end to end, with no human in the
code path (the humans are in the corpus, where §12.2 put them).
"""
from __future__ import annotations

import pytest

from featuregen.materialize.compiler_certification import (
    compare_expected_rows,
    compare_ir_payloads,
    current_compiler_contract,
    issue_compiler_certificate,
    record_compiler_case,
    run_compiler_case,
)

_IR = {"formula_content_hash": "sha256:f", "expressions": [{"op": "sum", "operand": "amt"}],
       "spine": {"table": "accounts"}}
_ROWS = [{"customer_id": "c1", "total": "10.00"}, {"customer_id": "c2", "total": "0"}]


def _case(conn, tag: str, **overrides) -> str:
    return record_compiler_case(
        conn, expectation_ref=overrides.get("expectation_ref", f"exp-{tag}"),
        blueprint_revision="bp-1", blueprint_hash="sha256:bp",
        approved_ir=overrides.get("approved_ir", _IR),
        dataset_pin=f"sha256:data-{tag}",
        expected_rows=overrides.get("expected_rows", _ROWS),
        runtime_profile={"renderer": "kedro-pyspark", "timezone": "UTC", "ansi": True},
        approved_by=["user:sme-a", "user:sme-b"])


def _contract(conn) -> str:
    return current_compiler_contract(conn).contract_hash


# ══ Comparison A — the IR, structurally ═════════════════════════════════════════════════════════
def test_a_matching_ir_matches_and_reports_no_path():
    assert compare_ir_payloads(_IR, dict(_IR)) == ("MATCHED", None)


def test_a_differing_ir_reports_the_FIRST_DIFFERING_PATH_not_a_hash():
    produced = {**_IR, "expressions": [{"op": "avg", "operand": "amt"}]}
    verdict, path = compare_ir_payloads(_IR, produced)
    assert verdict == "DIFFERED"
    assert path == "expressions[0].op", "'where' is the entire value of this comparison"


def test_only_the_input_identity_moving_is_ITS_OWN_VERDICT_with_its_own_remedy():
    """§12.1's subtlety: the producer's serialization moved, semantics did not. The case still
    fails — the ruling admits no third verdict — but the remedy is re-approve the fixture, a
    governance act, not a compiler debug."""
    produced = {**_IR, "formula_content_hash": "sha256:f-reserialized"}
    verdict, path = compare_ir_payloads(_IR, produced)
    assert verdict == "INPUT_IDENTITY_MOVED"
    assert "formula_content_hash" in path


# ══ Comparison B — banking values, exactly ══════════════════════════════════════════════════════
def test_matching_rows_match():
    assert compare_expected_rows(_ROWS, list(_ROWS), grain_keys=("customer_id",)) \
        == ("MATCHED", None)


def test_row_count_is_exact_in_BOTH_directions():
    verdict, why = compare_expected_rows(_ROWS, _ROWS[:1], grain_keys=("customer_id",))
    assert verdict == "DIFFERED" and "row_count" in why


def test_a_duplicated_grain_row_is_a_FAILURE_not_an_extra():
    doubled = [_ROWS[0], dict(_ROWS[0])]
    verdict, why = compare_expected_rows(_ROWS, doubled, grain_keys=("customer_id",))
    assert verdict == "DIFFERED"


def test_NULL_zero_and_row_absent_are_THREE_different_answers():
    # NULL vs 0
    verdict, why = compare_expected_rows(
        [{"customer_id": "c1", "total": None}], [{"customer_id": "c1", "total": "0"}],
        grain_keys=("customer_id",))
    assert verdict == "DIFFERED" and "null-vs-value" in why
    # value vs column-absent
    verdict, why = compare_expected_rows(
        [{"customer_id": "c1", "total": "0"}], [{"customer_id": "c1"}],
        grain_keys=("customer_id",))
    assert verdict == "DIFFERED" and "presence" in why


def test_decimals_compare_EXACTLY_after_the_DECLARED_policy():
    from featuregen.formula.schema_leaves import DecimalPolicy, OverflowBehavior, RoundingMode

    policy = DecimalPolicy(precision=10, scale=2, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.ERROR)
    verdict, _ = compare_expected_rows(
        [{"customer_id": "c1", "total": "10.005"}], [{"customer_id": "c1", "total": "10.01"}],
        grain_keys=("customer_id",), decimal_policies={"total": policy})
    assert verdict == "MATCHED", "10.005 quantized HALF_UP at scale 2 is 10.01 — exact after"

    verdict, _ = compare_expected_rows(
        [{"customer_id": "c1", "total": "10.004"}], [{"customer_id": "c1", "total": "10.01"}],
        grain_keys=("customer_id",), decimal_policies={"total": policy})
    assert verdict == "DIFFERED", "and exact means exact — no tolerance exists to absorb it"


# ══ the runner's honesty rules ══════════════════════════════════════════════════════════════════
def test_A_PASS_REQUIRES_BOTH_comparisons(db):
    case = _case(db, "both")
    contract = _contract(db)
    assert run_compiler_case(
        db, attempt_id="cca-both-1", case_revision_hash=case, contract_hash=contract,
        produced_ir=dict(_IR), executed_rows=list(_ROWS),
        grain_keys=("customer_id",)) == "PASSED"
    assert run_compiler_case(
        db, attempt_id="cca-both-2", case_revision_hash=case, contract_hash=contract,
        produced_ir={**_IR, "spine": {"table": "other"}}, executed_rows=list(_ROWS),
        grain_keys=("customer_id",)) == "FAILED_IR"
    assert run_compiler_case(
        db, attempt_id="cca-both-3", case_revision_hash=case, contract_hash=contract,
        produced_ir=dict(_IR), executed_rows=[_ROWS[0], {"customer_id": "c2", "total": "1"}],
        grain_keys=("customer_id",)) == "FAILED_VALUES"


def test_NO_EXECUTION_IS_UNMEASURED_never_a_pass(db):
    case = _case(db, "unm")
    outcome = run_compiler_case(
        db, attempt_id="cca-unm", case_revision_hash=case, contract_hash=_contract(db),
        produced_ir=dict(_IR), executed_rows=None, grain_keys=("customer_id",))
    assert outcome == "UNMEASURED"
    reason = db.execute("SELECT unmeasured_reason FROM recipe_compiler_eval_attempt "
                        "WHERE attempt_id = 'cca-unm'").fetchone()[0]
    assert "cannot certify" in reason


def test_A_PROVIDER_DISPATCH_FAILS_THE_ATTEMPT_whatever_the_comparisons_said(db):
    case = _case(db, "disp")
    outcome = run_compiler_case(
        db, attempt_id="cca-disp", case_revision_hash=case, contract_hash=_contract(db),
        produced_ir=dict(_IR), executed_rows=list(_ROWS), grain_keys=("customer_id",),
        provider_dispatch_count=1)
    assert outcome == "FAILED_DISPATCH_PRESENT", \
        "the determinism claim is false; nothing else about the attempt matters"


# ══ the issuer — vacuous truth mints nothing ════════════════════════════════════════════════════
def test_AN_EMPTY_CORPUS_CERTIFIES_NOTHING(db):
    contract = _contract(db)
    assert issue_compiler_certificate(
        db, contract_hash=contract, subject_identity_hash="mih-x") is None


def test_a_failing_case_blocks_the_certificate(db):
    case = _case(db, "fail")
    contract = _contract(db)
    run_compiler_case(db, attempt_id="cca-fail", case_revision_hash=case,
                      contract_hash=contract, produced_ir={**_IR, "spine": {}},
                      executed_rows=list(_ROWS), grain_keys=("customer_id",))
    assert issue_compiler_certificate(
        db, contract_hash=contract, subject_identity_hash="mih-y") is None


def test_THE_LOOP_CLOSES_a_passing_corpus_certifies_and_the_production_reader_FINDS_it(db):
    """Certification → production gate, end to end: the certificate this programme mints is the
    row the step-7 reader answers with — METHOD_CERTIFICATE_MISSING becomes a cleared member."""
    from featuregen.materialize.method_certificates import current_method_certificate

    case = _case(db, "loop")
    contract = _contract(db)
    run_compiler_case(db, attempt_id="cca-loop", case_revision_hash=case,
                      contract_hash=contract, produced_ir=dict(_IR),
                      executed_rows=list(_ROWS), grain_keys=("customer_id",))

    certificate_id = issue_compiler_certificate(
        db, contract_hash=contract, subject_identity_hash="mih-loop")
    assert certificate_id is not None

    found = current_method_certificate(
        db, certificate_kind="AUTHORING_METHOD", subject_identity_hash="mih-loop")
    assert found is not None
    assert found.certificate_revision_id == certificate_id
    assert found.contract_hash == contract


def test_tolerances_are_CONSTRAINED_EMPTY_in_the_schema(db):
    """R16: the grammar cannot express an approximate operation, so a tolerance names a property
    nothing can have — refused by the database, not by review vigilance."""
    with pytest.raises(Exception, match="compiler_case_tolerances_empty"):
        db.execute(
            "INSERT INTO recipe_compiler_eval_case (case_revision_hash, expectation_ref, "
            "blueprint_revision, blueprint_hash, approved_ir_json, approved_ir_hash, "
            "dataset_pin, expected_rows_json, expected_rows_hash, declared_tolerances_json, "
            "runtime_profile_json, approved_by) VALUES ('cse-tol', 'e', 'b', 'h', '{}'::jsonb, "
            "'h', 'd', '[]'::jsonb, 'h', '[{\"rel\": 0.01}]'::jsonb, '{}'::jsonb, '[]'::jsonb)")

"""Trusted, persisted evaluation for the recipe-formula shadow readiness gate.

The public evaluator accepts only an ``eval_run_id``. It reconstructs every metric from
write-once evaluation rows, verified authoring traces, strict dispatch audit chains, and
the durable shadow population. The pure gate helpers remain useful for deterministic unit
tests, but they are not an activation authority.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.config import get_settings
from featuregen.formula.schema import OPERATION_GRAMMAR_VERSION, OUTPUT_POLICY_VERSION
from featuregen.formula.replay_trace import load_verified_checkpoint
from featuregen.idgen import mint_id
from featuregen.overlay.upload.dispatch_audit import formula_dispatches_reconciled
from featuregen.overlay.upload.recipe_formula_authority import (
    verify_formula_authority_envelope,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
    validate_expectation_registry,
)
from featuregen.overlay.upload.recipe_formula_gate import (
    FormulaShadowGateResult,
    ProviderCaseOutcome,
    build_population_report,
    build_provider_evidence,
    evaluate_gate,
)
from featuregen.overlay.upload.recipe_formula_gold import (
    CORPUS_CONTENT_HASH,
    CORPUS_VERSION,
    FORMULA_GOLD_CASES,
    validate_formula_gold_corpus,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    content_hash,
    verify_observation_payload,
)

EVALUATOR_VERSION = "recipe-formula-evaluator-v1"
_OUTCOME_KEYS = frozenset({
    "technical_failure",
    "exact_match",
    "false_resolve",
    "accepted",
    "preservation_ok",
    "blocking_detected",
})


class FormulaEvaluationIntegrityError(RuntimeError):
    """Persisted evaluation evidence is incomplete, inconsistent, or tampered."""


@dataclass(frozen=True, slots=True)
class EvaluationRunConfiguration:
    provider: str
    model: str
    generation_controls: dict[str, Any]
    author_provider_contract_hash: str
    critic_provider_contract_hash: str
    shadow_window_start: datetime
    shadow_window_end: datetime
    shadow_generation_run_ids: tuple[str, ...]
    token_budget: int
    cost_budget: Decimal
    created_by: dict[str, Any]
    runner_kind: str = "REAL_PROVIDER"
    code_commit: str | None = None


def expectation_registry_hash() -> str:
    """Hash the complete validated authorability registry, not a duplicate ID list."""
    validate_expectation_registry()
    material = {
        recipe_id: asdict(expectation)
        for recipe_id, expectation in sorted(RECIPE_FORMULA_EXPECTATIONS.items())
    }
    return content_hash(material)


def _code_commit() -> str:
    configured = get_settings().producer_commit
    if configured != "unset":
        return configured
    try:
        return subprocess.check_output(
            ("git", "rev-parse", "HEAD"),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unverifiable"


def create_evaluation_run(
    conn,
    configuration: EvaluationRunConfiguration,
    *,
    eval_run_id: str | None = None,
) -> str:
    """Freeze the reviewed corpus and provider contract into a write-once run."""
    validate_formula_gold_corpus()
    if configuration.runner_kind not in {"REAL_PROVIDER", "FAKE_TEST"}:
        raise ValueError("runner_kind must be REAL_PROVIDER or FAKE_TEST")
    if configuration.shadow_window_end <= configuration.shadow_window_start:
        raise ValueError("shadow evaluation window must be non-empty")
    if configuration.token_budget <= 0 or configuration.cost_budget < 0:
        raise ValueError("evaluation budgets must be non-negative and non-vacuous")
    if len(configuration.shadow_generation_run_ids) != len(
        set(configuration.shadow_generation_run_ids)
    ):
        raise ValueError("shadow generation run ids must be unique")
    run_id = eval_run_id or mint_id("rfe")
    with conn.transaction():
        conn.execute(
            "INSERT INTO recipe_formula_eval_run "
            "(eval_run_id,corpus_version,corpus_content_hash,expectation_registry_hash,"
            "operation_grammar_version,output_policy_version,author_provider_contract_hash,"
            "critic_provider_contract_hash,provider,model,generation_controls,code_commit,"
            "shadow_window_start,shadow_window_end,shadow_generation_run_ids,token_budget,"
            "cost_budget,runner_kind,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                CORPUS_VERSION,
                CORPUS_CONTENT_HASH,
                expectation_registry_hash(),
                OPERATION_GRAMMAR_VERSION,
                OUTPUT_POLICY_VERSION,
                configuration.author_provider_contract_hash,
                configuration.critic_provider_contract_hash,
                configuration.provider,
                configuration.model,
                Jsonb(configuration.generation_controls),
                configuration.code_commit or _code_commit(),
                configuration.shadow_window_start,
                configuration.shadow_window_end,
                Jsonb(list(configuration.shadow_generation_run_ids)),
                configuration.token_budget,
                configuration.cost_budget,
                configuration.runner_kind,
                Jsonb(configuration.created_by),
            ),
        )
        for case in FORMULA_GOLD_CASES:
            case_input = dict(case.case_input)
            expected = dict(case.expected)
            conn.execute(
                "INSERT INTO recipe_formula_eval_case "
                "(eval_run_id,case_id,recipe_id,case_kind,case_input_json,case_input_hash,"
                "expected_json,expected_hash,repeat_group) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    case.case_id,
                    case.recipe_id,
                    case.case_kind,
                    Jsonb(case_input),
                    content_hash(case_input),
                    Jsonb(expected),
                    content_hash(expected),
                    case.repeat_group,
                ),
            )
    return run_id


def _dispatch_identity(conn, authoring_run_id: str) -> tuple[list[str], list[str], list[str]]:
    rows = conn.execute(
        "SELECT d.dispatch_ref,d.call_role,l.llm_call_ref "
        "FROM llm_dispatch d "
        "JOIN llm_call_dispatch l ON l.dispatch_ref=d.dispatch_ref "
        "WHERE d.authoring_run_id=%s "
        "ORDER BY d.dispatch_ref,l.llm_call_ref",
        (authoring_run_id,),
    ).fetchall()
    author = sorted({str(row[0]) for row in rows if row[1] == "formula.author"})
    critic = sorted({str(row[0]) for row in rows if row[1] == "formula.critic"})
    calls = sorted({str(row[2]) for row in rows})
    return author, critic, calls


def _audited_usage(conn, llm_refs: list[str]) -> tuple[int, int, Decimal]:
    rows = conn.execute(
        "SELECT cost_metadata FROM llm_call WHERE llm_call_ref = ANY(%s)",
        (llm_refs,),
    ).fetchall()
    if len(rows) != len(llm_refs):
        raise FormulaEvaluationIntegrityError("evaluation LLM usage records are incomplete")
    input_tokens = 0
    output_tokens = 0
    cost_amount = Decimal("0")
    for (metadata,) in rows:
        usage = metadata if isinstance(metadata, dict) else {}
        for key, target in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
        ):
            value = usage.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise FormulaEvaluationIntegrityError(
                    f"provider usage {key} is not a non-negative integer")
            if target == "input":
                input_tokens += value
            else:
                output_tokens += value
        raw_cost = usage.get("cost_amount", 0)
        try:
            parsed_cost = Decimal(str(raw_cost))
        except Exception as exc:
            raise FormulaEvaluationIntegrityError(
                "provider cost_amount is not numeric") from exc
        if parsed_cost < 0:
            raise FormulaEvaluationIntegrityError(
                "provider cost_amount cannot be negative")
        cost_amount += parsed_cost
    return input_tokens, output_tokens, cost_amount


def record_evaluation_attempt(
    conn,
    *,
    eval_run_id: str,
    case_id: str,
    authoring_run_id: str,
    disposition: str,
    outcome: Mapping[str, Any],
    repeat_index: int = 0,
    attempt_id: str | None = None,
) -> str:
    """Persist one attempt while deriving provider identities from the audited run."""
    if set(outcome) != _OUTCOME_KEYS or any(
        not isinstance(outcome[key], bool) for key in _OUTCOME_KEYS
    ):
        raise ValueError("evaluation outcome must contain exactly the six boolean gate axes")
    if repeat_index < 0:
        raise ValueError("evaluation repeat index cannot be negative")
    case = conn.execute(
        "SELECT 1 FROM recipe_formula_eval_case WHERE eval_run_id=%s AND case_id=%s",
        (eval_run_id, case_id),
    ).fetchone()
    if case is None:
        raise ValueError("evaluation attempt does not belong to the frozen corpus")
    if not formula_dispatches_reconciled(conn, authoring_run_id):
        raise FormulaEvaluationIntegrityError(
            "evaluation attempt authoring dispatch is not strictly reconciled")
    author_refs, critic_refs, llm_refs = _dispatch_identity(conn, authoring_run_id)
    if not author_refs or not critic_refs or not llm_refs:
        raise FormulaEvaluationIntegrityError(
            "evaluation attempt requires audited author and critic provider calls")
    input_tokens, output_tokens, cost_amount = _audited_usage(conn, llm_refs)
    attempt = attempt_id or mint_id("rfa")
    material = dict(outcome)
    conn.execute(
        "INSERT INTO recipe_formula_eval_attempt "
        "(attempt_id,eval_run_id,case_id,repeat_index,authoring_run_id,"
        "author_dispatch_refs,critic_dispatch_refs,llm_call_refs,disposition,"
        "outcome_json,outcome_hash,input_tokens,output_tokens,cost_amount) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            attempt,
            eval_run_id,
            case_id,
            repeat_index,
            authoring_run_id,
            Jsonb(author_refs),
            Jsonb(critic_refs),
            Jsonb(llm_refs),
            disposition,
            Jsonb(material),
            content_hash(material),
            input_tokens,
            output_tokens,
            cost_amount,
        ),
    )
    return attempt


def _verify_run_and_cases(run: Mapping[str, Any], cases: list[Mapping[str, Any]]) -> None:
    if (
        run["corpus_version"] != CORPUS_VERSION
        or run["corpus_content_hash"] != CORPUS_CONTENT_HASH
        or run["expectation_registry_hash"] != expectation_registry_hash()
        or run["operation_grammar_version"] != OPERATION_GRAMMAR_VERSION
        or run["output_policy_version"] != OUTPUT_POLICY_VERSION
    ):
        raise FormulaEvaluationIntegrityError(
            "evaluation run versions or corpus identity no longer verify")
    expected = {case.case_id: case for case in FORMULA_GOLD_CASES}
    if len(cases) != len(expected) or {row["case_id"] for row in cases} != set(expected):
        raise FormulaEvaluationIntegrityError(
            "persisted evaluation cases differ from the frozen corpus")
    for row in cases:
        case = expected[row["case_id"]]
        if (
            row["recipe_id"] != case.recipe_id
            or row["case_kind"] != case.case_kind
            or row["case_input_json"] != case.case_input
            or row["expected_json"] != case.expected
            or row["case_input_hash"] != content_hash(row["case_input_json"])
            or row["expected_hash"] != content_hash(row["expected_json"])
        ):
            raise FormulaEvaluationIntegrityError(
                f"evaluation case {row['case_id']!r} does not verify")


def _verify_dispatch_contract(
    conn,
    *,
    authoring_run_id: str,
    run: Mapping[str, Any],
) -> None:
    rows = conn.execute(
        "SELECT d.call_role,d.provider_contract_hash,d.provider,d.model,"
        "c.generation_settings "
        "FROM llm_dispatch d "
        "JOIN llm_call_dispatch l ON l.dispatch_ref=d.dispatch_ref "
        "JOIN llm_call c ON c.llm_call_ref=l.llm_call_ref "
        "WHERE d.authoring_run_id=%s",
        (authoring_run_id,),
    ).fetchall()
    if not rows:
        raise FormulaEvaluationIntegrityError("evaluation dispatch contract is missing")
    expected_contracts = {
        "formula.author": run["author_provider_contract_hash"],
        "formula.critic": run["critic_provider_contract_hash"],
    }
    observed_roles: set[str] = set()
    for role, contract_hash, provider, model, controls in rows:
        observed_roles.add(role)
        if (
            role not in expected_contracts
            or contract_hash != expected_contracts[role]
            or provider != run["provider"]
            or model != run["model"]
            or controls != run["generation_controls"]
        ):
            raise FormulaEvaluationIntegrityError(
                "physical dispatch differs from the frozen provider contract")
    if observed_roles != set(expected_contracts):
        raise FormulaEvaluationIntegrityError(
            "evaluation requires both author and critic provider roles")


def _derive_outcome(
    *,
    case: Mapping[str, Any],
    checkpoint,
) -> dict[str, bool]:
    terminal = checkpoint.terminal_result
    result = terminal.get("result") if isinstance(terminal, dict) else None
    if not isinstance(result, dict):
        raise FormulaEvaluationIntegrityError("evaluation terminal result is missing")
    disposition = str(result.get("authoring_disposition"))
    expected = case["expected_json"]
    fault = case["case_input_json"].get("fault")
    critic_findings = (
        checkpoint.critic_result.get("findings", ())
        if isinstance(checkpoint.critic_result, dict)
        else ()
    )
    critic_codes = {
        finding.get("code")
        for finding in critic_findings
        if isinstance(finding, dict) and finding.get("severity") == "blocking"
    }
    if expected.get("blocking_code"):
        blocking_detected = expected["blocking_code"] in critic_codes
    elif fault == "OUTPUT_POLICY_UNRESOLVED":
        blocking_detected = result.get("output_status") in {
            "needs_authority",
            "external_requirement",
            "invalid_output",
        }
    elif fault == "OPERAND_NOT_PRESERVED":
        blocking_detected = (
            isinstance(terminal, dict)
            and terminal.get("reason") == "recipe_expectation_not_preserved"
            and "OPERAND_NOT_PRESERVED" in terminal.get("violations", ())
        )
    elif fault in {"MALFORMED_PROVIDER_RESPONSE", "DISPATCH_FAILURE"}:
        blocking_detected = result.get("technical_status") == "technical_failure"
    else:
        blocking_detected = False
    preservation_ok = (
        case["case_kind"] == "clean"
        and disposition == "RESOLVED"
        and result.get("structural_status") == "ok"
        and result.get("expectation_status") == "match"
        and result.get("candidate_formula_hash") is not None
    )
    accepted = disposition == "RESOLVED"
    false_resolve = accepted and (
        case["case_kind"] == "adversarial" or not preservation_ok)
    exact_match = (
        disposition == expected.get("disposition")
        and not false_resolve
        and (
            preservation_ok
            if case["case_kind"] == "clean"
            else blocking_detected
        )
    )
    return {
        "technical_failure": result.get("technical_status") == "technical_failure",
        "exact_match": exact_match,
        "false_resolve": false_resolve,
        "accepted": accepted,
        "preservation_ok": preservation_ok,
        "blocking_detected": blocking_detected,
    }


def _verified_attempt_outcomes(
    conn,
    run: Mapping[str, Any],
    cases: list[Mapping[str, Any]],
    attempts: list[Mapping[str, Any]],
) -> tuple[ProviderCaseOutcome, ...]:
    primary = [row for row in attempts if row["repeat_index"] == 0]
    by_case = {row["case_id"]: row for row in primary}
    if len(primary) != len(cases) or len(by_case) != len(primary):
        raise FormulaEvaluationIntegrityError(
            "every frozen case requires exactly one distinct primary attempt")
    run_ids = [row["authoring_run_id"] for row in primary]
    if any(not isinstance(value, str) for value in run_ids) or len(run_ids) != len(set(run_ids)):
        raise FormulaEvaluationIntegrityError(
            "primary cases must use distinct, non-null authoring runs")
    seen_dispatches: set[str] = set()
    seen_calls: set[str] = set()
    outcomes: list[ProviderCaseOutcome] = []
    for case in cases:
        attempt = by_case[case["case_id"]]
        outcome = attempt["outcome_json"]
        if (
            not isinstance(outcome, dict)
            or set(outcome) != _OUTCOME_KEYS
            or any(not isinstance(outcome[key], bool) for key in _OUTCOME_KEYS)
            or content_hash(outcome) != attempt["outcome_hash"]
        ):
            raise FormulaEvaluationIntegrityError(
                f"evaluation attempt for {case['case_id']!r} has a bad outcome hash")
        authoring_run_id = attempt["authoring_run_id"]
        authoring_manifest = conn.execute(
            "SELECT intent_hash,versions FROM formula_authoring_run "
            "WHERE authoring_run_id=%s",
            (authoring_run_id,),
        ).fetchone()
        if authoring_manifest is None:
            raise FormulaEvaluationIntegrityError("evaluation authoring run is missing")
        checkpoint = load_verified_checkpoint(
            conn,
            authoring_run_id,
            intent_hash=authoring_manifest[0],
            versions=authoring_manifest[1],
        )
        terminal = checkpoint.terminal_result
        result = terminal.get("result") if isinstance(terminal, dict) else None
        derived_outcome = _derive_outcome(
            case=case,
            checkpoint=checkpoint,
        )
        if (
            not isinstance(result, dict)
            or result.get("authoring_disposition") != attempt["disposition"]
            or outcome != derived_outcome
        ):
            raise FormulaEvaluationIntegrityError(
                f"evaluation attempt for {case['case_id']!r} conflicts with its trace")
        author_refs, critic_refs, llm_refs = _dispatch_identity(conn, authoring_run_id)
        if (
            author_refs != attempt["author_dispatch_refs"]
            or critic_refs != attempt["critic_dispatch_refs"]
            or llm_refs != attempt["llm_call_refs"]
            or not formula_dispatches_reconciled(conn, authoring_run_id)
        ):
            raise FormulaEvaluationIntegrityError(
                f"evaluation attempt for {case['case_id']!r} has an invalid dispatch chain")
        _verify_dispatch_contract(
            conn,
            authoring_run_id=authoring_run_id,
            run=run,
        )
        all_dispatches = set(author_refs) | set(critic_refs)
        if seen_dispatches & all_dispatches or seen_calls & set(llm_refs):
            raise FormulaEvaluationIntegrityError(
                "provider calls or physical dispatches were reused as distinct cases")
        seen_dispatches.update(all_dispatches)
        seen_calls.update(llm_refs)
        outcomes.append(ProviderCaseOutcome(
            case_id=case["case_id"],
            case_kind=case["case_kind"],
            technical_failure=outcome["technical_failure"],
            exact_match=outcome["exact_match"],
            false_resolve=outcome["false_resolve"],
            accepted=outcome["accepted"],
            preservation_ok=outcome["preservation_ok"],
            blocking_detected=outcome["blocking_detected"],
            dispatch_count=len(all_dispatches),
            reconciled_dispatch_count=len(all_dispatches),
            strict_audited_dispatch_count=len(all_dispatches),
        ))
    return tuple(outcomes)


def _authority_population(
    conn,
    *,
    since: datetime,
    until: datetime,
    generation_run_ids: tuple[str, ...],
) -> dict[str, int]:
    """Count only current, hash-verified authority envelopes in the frozen cohort window."""
    with conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            "SELECT * FROM recipe_formula_shadow_observation "
            "WHERE created_at >= %s AND created_at < %s "
            "AND generation_run_id = ANY(%s) "
            "AND authority_axis='VERIFIED_AT_CAPTURE'",
            (since, until, list(generation_run_ids)),
        ).fetchall()
    counts = {recipe_id: 0 for recipe_id in RECIPE_FORMULA_EXPECTATIONS}
    for row in rows:
        envelope = row.get("binding_envelope_json")
        recipe_id = row.get("recipe_id")
        if (
            recipe_id in counts
            and isinstance(envelope, dict)
            and verify_observation_payload(row) is None
            and verify_formula_authority_envelope(conn, envelope) is None
        ):
            counts[recipe_id] += 1
    return counts


def _artifact_report(
    run: Mapping[str, Any],
    result: FormulaShadowGateResult,
    outcomes: tuple[ProviderCaseOutcome, ...],
) -> dict[str, Any]:
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "eval_run_id": run["eval_run_id"],
        "corpus_version": run["corpus_version"],
        "corpus_content_hash": run["corpus_content_hash"],
        "runner_kind": run["runner_kind"],
        "provider": run["provider"],
        "model": run["model"],
        "code_commit": run["code_commit"],
        "case_ids": [outcome.case_id for outcome in outcomes],
        "gate": {
            "population_reconciled": result.population_reconciled,
            "non_vacuous_positives": result.non_vacuous_positives,
            "deterministic_correctness": result.deterministic_correctness,
            "provider_quality": result.provider_quality,
            "passed": result.passed,
            "activation_status": result.activation_status,
            "reasons": list(result.reasons),
        },
        "population": asdict(result.report),
    }


def evaluate_persisted_run(conn, eval_run_id: str) -> FormulaShadowGateResult:
    """Evaluate one immutable run from durable evidence and append its final artifact."""
    with conn.cursor(row_factory=dict_row) as cursor:
        run = cursor.execute(
            "SELECT * FROM recipe_formula_eval_run WHERE eval_run_id=%s",
            (eval_run_id,),
        ).fetchone()
        cases = cursor.execute(
            "SELECT * FROM recipe_formula_eval_case WHERE eval_run_id=%s ORDER BY case_id",
            (eval_run_id,),
        ).fetchall()
        attempts = cursor.execute(
            "SELECT * FROM recipe_formula_eval_attempt WHERE eval_run_id=%s "
            "ORDER BY case_id,repeat_index",
            (eval_run_id,),
        ).fetchall()
    if run is None:
        raise FormulaEvaluationIntegrityError("evaluation run does not exist")
    if run["runner_kind"] != "REAL_PROVIDER":
        raise FormulaEvaluationIntegrityError(
            "FAKE_TEST runs can test mechanics but cannot produce a qualified artifact")
    _verify_run_and_cases(run, cases)
    frozen_run_ids = run["shadow_generation_run_ids"]
    if (
        not isinstance(frozen_run_ids, list)
        or not frozen_run_ids
        or any(not isinstance(value, str) for value in frozen_run_ids)
        or len(frozen_run_ids) != len(set(frozen_run_ids))
    ):
        raise FormulaEvaluationIntegrityError(
            "real-provider evaluation requires an exact non-empty shadow run cohort")
    generation_run_ids = tuple(frozen_run_ids)
    observed_run_ids = {
        row[0]
        for row in conn.execute(
            "SELECT generation_run_id FROM recipe_formula_shadow_expected_run "
            "WHERE generation_run_id = ANY(%s)",
            (list(generation_run_ids),),
        ).fetchall()
    }
    if observed_run_ids != set(generation_run_ids):
        raise FormulaEvaluationIntegrityError(
            "frozen shadow run cohort contains missing or undeclared runs")
    outcomes = _verified_attempt_outcomes(conn, run, cases, attempts)
    input_tokens = sum(row["input_tokens"] for row in attempts)
    output_tokens = sum(row["output_tokens"] for row in attempts)
    cost = sum((row["cost_amount"] for row in attempts), Decimal("0"))
    if (
        input_tokens + output_tokens > run["token_budget"]
        or cost > run["cost_budget"]
    ):
        raise FormulaEvaluationIntegrityError("evaluation exceeded its frozen budget")
    provider = build_provider_evidence(outcomes)
    adversarial = [
        outcome for outcome in outcomes if outcome.case_kind == "adversarial"]
    report = build_population_report(
        conn,
        since=run["shadow_window_start"],
        until=run["shadow_window_end"],
        generation_run_ids=generation_run_ids,
    )
    result = evaluate_gate(
        report,
        provider,
        deterministic_false_resolves=sum(
            1 for outcome in outcomes if outcome.false_resolve),
        deterministic_adversarial_cases=len(adversarial),
        deterministic_adversarial_detections=sum(
            1 for outcome in adversarial if outcome.blocking_detected),
        real_authority_population=_authority_population(
            conn,
            since=run["shadow_window_start"],
            until=run["shadow_window_end"],
            generation_run_ids=generation_run_ids,
        ),
    )
    artifact = _artifact_report(run, result, outcomes)
    digest = content_hash(artifact)
    artifact_id = f"rfe_artifact_{eval_run_id}"
    inserted = conn.execute(
        "INSERT INTO recipe_formula_eval_artifact "
        "(artifact_id,eval_run_id,result,report_json,content_hash) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (eval_run_id) DO NOTHING "
        "RETURNING artifact_id",
        (
            artifact_id,
            eval_run_id,
            "PASS" if result.passed else "FAIL",
            Jsonb(artifact),
            digest,
        ),
    ).fetchone()
    if inserted is None:
        existing = conn.execute(
            "SELECT result,report_json,content_hash FROM recipe_formula_eval_artifact "
            "WHERE eval_run_id=%s",
            (eval_run_id,),
        ).fetchone()
        expected = ("PASS" if result.passed else "FAIL", artifact, digest)
        if existing != expected:
            raise FormulaEvaluationIntegrityError(
                "existing evaluation artifact conflicts with recomputed evidence")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recipe-formula-eval",
        description="Evaluate persisted recipe-formula provider evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--eval-run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = os.environ.get("FEATUREGEN_DSN")
    if not dsn:
        raise RuntimeError("FEATUREGEN_DSN is required for durable formula evaluation")
    with psycopg.connect(dsn) as conn:
        result = evaluate_persisted_run(conn, args.eval_run_id)
        print(json.dumps({
            "passed": result.passed,
            "activation_status": result.activation_status,
            "reasons": list(result.reasons),
        }, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

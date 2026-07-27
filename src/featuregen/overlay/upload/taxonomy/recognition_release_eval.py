"""Durable 100-case release evaluation for recognition and applicability."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.idgen import mint_id
from featuregen.intake.llm import FakeLLM, current_llm_client
from featuregen.intake.llm_claude import ClaudeLLM
from featuregen.overlay.upload.enrich_llm import (
    current_enrichment_generation_settings,
    register_enrichment_schemas,
)
from featuregen.overlay.upload.recipe_formula_shadow import content_hash
from featuregen.overlay.upload.taxonomy.applicability import (
    ConfirmedScope,
    in_scope_recipes,
)
from featuregen.overlay.upload.taxonomy.gold_recognition import (
    TARGET_GOLD,
    validate_target_gold,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    APPLICABILITY_MAPPING_VERSION,
    RECIPE_REGISTRY_VERSION,
    TAXONOMY_VERSION,
    RecognitionStatus,
    validate_recognition_output,
)
from featuregen.overlay.upload.taxonomy.recognizer import (
    RECOGNIZER_TASK,
    recognize_with_audit,
)
from featuregen.overlay.upload.taxonomy.recognizer_prompt import (
    PROMPT_ID,
    PROMPT_VERSION,
    build_recognition_prompt,
)

CORPUS_VERSION = "recognition-four-objective-v1"
EVALUATOR_VERSION = "recognition-release-evaluator-v1"
SCHEMA_ID = "use_case_recognition"
SCHEMA_VERSION = 1
TARGET_CORPUS_HASH = content_hash([asdict(case) for case in TARGET_GOLD])
_WILSON_Z_ONE_SIDED_95 = 1.6448536269514722


class RecognitionEvaluationIntegrityError(RuntimeError):
    """Persisted recognition evidence is incomplete, inconsistent or synthetic."""


@dataclass(frozen=True, slots=True)
class RecognitionEvaluationConfiguration:
    runner_kind: str
    stability_case_count: int
    repeat_count: int
    token_budget: int
    cost_budget: Decimal
    created_by: dict[str, Any]
    code_commit: str | None = None


def _code_commit() -> str:
    try:
        return subprocess.check_output(
            ("git", "rev-parse", "HEAD"),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unverifiable"


def _schema(conn) -> dict:
    registry = DocumentSchemaRegistry(conn)
    schema = registry.schema_for(SCHEMA_ID, SCHEMA_VERSION)
    if schema is None:
        register_enrichment_schemas(conn)
        schema = registry.schema_for(SCHEMA_ID, SCHEMA_VERSION)
    if not isinstance(schema, dict):
        raise RecognitionEvaluationIntegrityError(
            "recognition output schema is unavailable")
    return schema


def create_evaluation_run(
    conn,
    configuration: RecognitionEvaluationConfiguration,
    *,
    eval_run_id: str | None = None,
) -> str:
    validate_target_gold()
    if configuration.runner_kind not in {"REAL_PROVIDER", "FAKE_TEST"}:
        raise ValueError("runner_kind must be REAL_PROVIDER or FAKE_TEST")
    if not 0 <= configuration.stability_case_count <= len(TARGET_GOLD):
        raise ValueError("stability case count is outside the corpus")
    if configuration.repeat_count < 0:
        raise ValueError("repeat count cannot be negative")
    if configuration.token_budget <= 0 or configuration.cost_budget < 0:
        raise ValueError("evaluation budgets must be non-negative and non-vacuous")
    settings = current_enrichment_generation_settings()
    provider = str(settings["provider"])
    model = str(settings["model"])
    if configuration.runner_kind == "REAL_PROVIDER" and provider != "anthropic":
        raise RecognitionEvaluationIntegrityError(
            "REAL_PROVIDER recognition evaluation requires the configured Anthropic adapter")
    schema = _schema(conn)
    run_id = eval_run_id or mint_id("rre")
    prompt_hash = hashlib.sha256(build_recognition_prompt().encode()).hexdigest()
    schema_hash = content_hash(schema)
    with conn.transaction():
        conn.execute(
            "INSERT INTO recognition_eval_run "
            "(eval_run_id,corpus_version,corpus_content_hash,taxonomy_version,"
            "applicability_version,recipe_registry_version,provider,model,prompt_id,"
            "prompt_version,prompt_content_hash,schema_id,schema_version,schema_content_hash,"
            "generation_controls,runner_kind,stability_case_count,repeat_count,token_budget,"
            "cost_budget,code_commit,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                CORPUS_VERSION,
                TARGET_CORPUS_HASH,
                TAXONOMY_VERSION,
                APPLICABILITY_MAPPING_VERSION,
                RECIPE_REGISTRY_VERSION,
                provider,
                model,
                PROMPT_ID,
                int(PROMPT_VERSION),
                prompt_hash,
                SCHEMA_ID,
                SCHEMA_VERSION,
                schema_hash,
                Jsonb(settings),
                configuration.runner_kind,
                configuration.stability_case_count,
                configuration.repeat_count,
                configuration.token_budget,
                configuration.cost_budget,
                configuration.code_commit or _code_commit(),
                Jsonb(configuration.created_by),
            ),
        )
        for case in TARGET_GOLD:
            material = asdict(case)
            conn.execute(
                "INSERT INTO recognition_eval_case "
                "(eval_run_id,case_id,case_json,case_content_hash) VALUES (%s,%s,%s,%s)",
                (run_id, case.id, Jsonb(material), content_hash(material)),
            )
    return run_id


def _primary(output: dict) -> str | None:
    return next(
        (
            str(candidate["use_case_id"])
            for candidate in output.get("candidates", ())
            if candidate.get("relationship") == "primary"
        ),
        None,
    )


def _scope_from_output(output: dict) -> ConfirmedScope:
    primary = _primary(output)
    secondary = tuple(
        str(candidate["use_case_id"])
        for candidate in output.get("candidates", ())
        if candidate.get("relationship") == "secondary"
    )
    return ConfirmedScope(
        primary=primary,
        secondary=secondary,
        unscoped=output.get("status") == "unscoped",
    )


def _usage(metadata: dict | None) -> tuple[int, int, Decimal]:
    value = metadata if isinstance(metadata, dict) else {}
    input_tokens = value.get("input_tokens", 0)
    output_tokens = value.get("output_tokens", 0)
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        raise RecognitionEvaluationIntegrityError(
            "provider token usage is not a non-negative integer")
    try:
        cost = Decimal(str(value.get("cost_amount", 0)))
    except Exception as exc:
        raise RecognitionEvaluationIntegrityError(
            "provider cost is not numeric") from exc
    if cost < 0:
        raise RecognitionEvaluationIntegrityError("provider cost cannot be negative")
    return input_tokens, output_tokens, cost


def _record_attempt(
    conn,
    *,
    eval_run_id: str,
    case,
    repeat_index: int,
    llm_call_ref: str,
) -> None:
    with conn.cursor(row_factory=dict_row) as cursor:
        call = cursor.execute(
            "SELECT * FROM llm_call WHERE llm_call_ref=%s",
            (llm_call_ref,),
        ).fetchone()
    if call is None:
        raise RecognitionEvaluationIntegrityError("recognition LLM audit row is missing")
    raw = call["raw_output"]
    output = raw.get("output") if isinstance(raw, dict) else None
    technical = True
    false_narrowing = True
    abstained = True
    primary = None
    status = "technical_failure"
    material: dict[str, Any] = {}
    if isinstance(output, dict):
        material = output
        try:
            validate_recognition_output(material)
        except Exception:
            pass
        else:
            status = str(material["status"])
            primary = _primary(material)
            scope = _scope_from_output(material)
            primary_recipes, supporting_recipes = in_scope_recipes(scope)
            retained = primary_recipes | supporting_recipes
            false_narrowing = any(
                recipe_id not in retained
                for recipe_id in case.expected_relevant_recipes
            )
            technical = False
            abstained = status == RecognitionStatus.UNSCOPED.value
    input_tokens, output_tokens, cost = _usage(call["cost_metadata"])
    conn.execute(
        "INSERT INTO recognition_eval_attempt "
        "(attempt_id,eval_run_id,case_id,repeat_index,llm_call_ref,recognition_json,"
        "recognition_hash,recognized_primary,status,false_narrowing,technical_failure,"
        "abstained,input_tokens,output_tokens,cost_amount) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            mint_id("rra"),
            eval_run_id,
            case.id,
            repeat_index,
            llm_call_ref,
            Jsonb(material),
            content_hash(material),
            primary,
            status,
            false_narrowing,
            technical,
            abstained,
            input_tokens,
            output_tokens,
            cost,
        ),
    )


def execute_evaluation_run(conn, eval_run_id: str, client) -> int:
    run = conn.execute(
        "SELECT runner_kind,stability_case_count,repeat_count,provider "
        "FROM recognition_eval_run WHERE eval_run_id=%s",
        (eval_run_id,),
    ).fetchone()
    if run is None:
        raise RecognitionEvaluationIntegrityError("evaluation run does not exist")
    if run[0] == "REAL_PROVIDER" and (
        run[3] != "anthropic"
        or isinstance(client, FakeLLM)
        or not isinstance(client, ClaudeLLM)
    ):
        raise RecognitionEvaluationIntegrityError(
            "a fake client cannot execute a REAL_PROVIDER recognition run")
    executed = 0
    for case_index, case in enumerate(TARGET_GOLD):
        repeats = 1 + (
            run[2] if case_index < run[1] else 0)
        for repeat_index in range(repeats):
            audited = recognize_with_audit(
                conn,
                client,
                redacted_hypothesis=case.hypothesis,
                redacted_goal=case.prediction_goal,
                audit_run_id=f"recognition-eval:{eval_run_id}",
            )
            if audited.llm_call_ref is None:
                raise RecognitionEvaluationIntegrityError(
                    f"case {case.id} produced no durable LLM audit reference")
            _record_attempt(
                conn,
                eval_run_id=eval_run_id,
                case=case,
                repeat_index=repeat_index,
                llm_call_ref=audited.llm_call_ref,
            )
            executed += 1
    return executed


def _wilson_upper(failures: int, observations: int) -> float:
    if observations <= 0:
        return 1.0
    p = failures / observations
    z = _WILSON_Z_ONE_SIDED_95
    denominator = 1 + z * z / observations
    centre = p + z * z / (2 * observations)
    margin = z * math.sqrt(
        p * (1 - p) / observations + z * z / (4 * observations * observations))
    return (centre + margin) / denominator


def _verify_run(run: dict, cases: list[dict]) -> None:
    if (
        run["corpus_version"] != CORPUS_VERSION
        or run["corpus_content_hash"] != TARGET_CORPUS_HASH
        or run["taxonomy_version"] != TAXONOMY_VERSION
        or run["applicability_version"] != APPLICABILITY_MAPPING_VERSION
        or run["recipe_registry_version"] != RECIPE_REGISTRY_VERSION
        or run["prompt_content_hash"]
        != hashlib.sha256(build_recognition_prompt().encode()).hexdigest()
    ):
        raise RecognitionEvaluationIntegrityError(
            "recognition evaluation versions or corpus have drifted")
    expected = {case.id: asdict(case) for case in TARGET_GOLD}
    if {row["case_id"] for row in cases} != set(expected) or len(cases) != 100:
        raise RecognitionEvaluationIntegrityError(
            "persisted recognition corpus is not the exact 100-case denominator")
    for row in cases:
        if (
            row["case_json"] != expected[row["case_id"]]
            or row["case_content_hash"] != content_hash(row["case_json"])
        ):
            raise RecognitionEvaluationIntegrityError(
                f"recognition case {row['case_id']} does not verify")


def evaluate_persisted_run(conn, eval_run_id: str) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cursor:
        run = cursor.execute(
            "SELECT * FROM recognition_eval_run WHERE eval_run_id=%s",
            (eval_run_id,),
        ).fetchone()
        cases = cursor.execute(
            "SELECT * FROM recognition_eval_case WHERE eval_run_id=%s ORDER BY case_id",
            (eval_run_id,),
        ).fetchall()
        attempts = cursor.execute(
            "SELECT * FROM recognition_eval_attempt WHERE eval_run_id=%s "
            "ORDER BY case_id,repeat_index",
            (eval_run_id,),
        ).fetchall()
    if run is None:
        raise RecognitionEvaluationIntegrityError("evaluation run does not exist")
    if run["runner_kind"] != "REAL_PROVIDER":
        raise RecognitionEvaluationIntegrityError(
            "FAKE_TEST runs cannot produce a provider-qualified artifact")
    _verify_run(run, cases)
    primary = [attempt for attempt in attempts if attempt["repeat_index"] == 0]
    if len(primary) != 100 or len({row["case_id"] for row in primary}) != 100:
        raise RecognitionEvaluationIntegrityError(
            "evaluation requires 100 distinct primary observations")
    expected_attempts = 100 + run["stability_case_count"] * run["repeat_count"]
    if len(attempts) != expected_attempts:
        raise RecognitionEvaluationIntegrityError(
            "evaluation stability attempts are incomplete")
    call_refs = [row["llm_call_ref"] for row in attempts]
    if len(call_refs) != len(set(call_refs)):
        raise RecognitionEvaluationIntegrityError(
            "recognition attempts reused a provider audit row")

    case_by_id = {case.id: case for case in TARGET_GOLD}
    by_case: dict[str, list[dict]] = {}
    for attempt in attempts:
        by_case.setdefault(attempt["case_id"], []).append(attempt)
        call = conn.execute(
            "SELECT task,provider,model,prompt_id,prompt_version,output_schema_id,"
            "output_schema_version,generation_settings,raw_output,cost_metadata "
            "FROM llm_call WHERE llm_call_ref=%s",
            (attempt["llm_call_ref"],),
        ).fetchone()
        if call is None:
            raise RecognitionEvaluationIntegrityError("attempt LLM call is missing")
        raw = call[8]
        output = raw.get("output") if isinstance(raw, dict) else None
        tokens_in, tokens_out, cost = _usage(call[9])
        expected_fields = (
            RECOGNIZER_TASK,
            run["provider"],
            run["model"],
            run["prompt_id"],
            run["prompt_version"],
            run["schema_id"],
            run["schema_version"],
            run["generation_controls"],
        )
        if call[:8] != expected_fields:
            raise RecognitionEvaluationIntegrityError(
                "recognition attempt differs from the frozen provider contract")
        if (
            not isinstance(output, dict)
            or output != attempt["recognition_json"]
            or content_hash(output) != attempt["recognition_hash"]
            or (tokens_in, tokens_out, cost)
            != (
                attempt["input_tokens"],
                attempt["output_tokens"],
                attempt["cost_amount"],
            )
        ):
            raise RecognitionEvaluationIntegrityError(
                "recognition attempt conflicts with its immutable LLM audit")
        try:
            validate_recognition_output(output)
        except Exception as exc:
            if not attempt["technical_failure"]:
                raise RecognitionEvaluationIntegrityError(
                    "invalid provider output was scored as non-technical") from exc
        else:
            case = case_by_id[attempt["case_id"]]
            scope = _scope_from_output(output)
            primary_recipes, supporting_recipes = in_scope_recipes(scope)
            false_narrowing = any(
                recipe_id not in primary_recipes | supporting_recipes
                for recipe_id in case.expected_relevant_recipes
            )
            if (
                attempt["technical_failure"]
                or attempt["recognized_primary"] != _primary(output)
                or attempt["status"] != output["status"]
                or attempt["false_narrowing"] != false_narrowing
                or attempt["abstained"] != (output["status"] == "unscoped")
            ):
                raise RecognitionEvaluationIntegrityError(
                    "persisted recognition outcome was not derived from provider output")

    technical = sum(row["technical_failure"] for row in primary)
    abstentions = sum(row["abstained"] for row in primary)
    false_narrowings = sum(row["false_narrowing"] for row in primary)
    wrong_primary = sum(
        row["recognized_primary"] != case_by_id[row["case_id"]].expected_primary
        for row in primary
    )
    stable = 0
    for _case_id, rows in by_case.items():
        if len(rows) == 1:
            continue
        signatures = {
            (
                row["status"],
                row["recognized_primary"],
                tuple(
                    sorted(
                        candidate["use_case_id"]
                        for candidate in row["recognition_json"].get("candidates", ())
                    )
                ),
            )
            for row in rows
        }
        stable += len(signatures) == 1
    stability_required = run["stability_case_count"]
    wilson_upper = _wilson_upper(false_narrowings, 100)
    total_tokens = sum(
        row["input_tokens"] + row["output_tokens"] for row in attempts)
    total_cost = sum((row["cost_amount"] for row in attempts), Decimal("0"))
    budget_ok = (
        total_tokens <= run["token_budget"] and total_cost <= run["cost_budget"])
    passed = (
        technical == 0
        and abstentions == 0
        and false_narrowings == 0
        and wrong_primary == 0
        and wilson_upper <= 0.03
        and stable == stability_required
        and budget_ok
    )
    report = {
        "evaluator_version": EVALUATOR_VERSION,
        "eval_run_id": eval_run_id,
        "primary_observations": 100,
        "false_narrowings": false_narrowings,
        "false_narrowing_wilson_upper_one_sided_95": wilson_upper,
        "technical_failures": technical,
        "abstentions": abstentions,
        "wrong_primary": wrong_primary,
        "stability_cases": stability_required,
        "stable_cases": stable,
        "total_tokens": total_tokens,
        "total_cost": str(total_cost),
        "budget_ok": budget_ok,
        "passed": passed,
    }
    digest = content_hash(report)
    inserted = conn.execute(
        "INSERT INTO recognition_eval_artifact "
        "(artifact_id,eval_run_id,result,report_json,content_hash) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (eval_run_id) DO NOTHING "
        "RETURNING artifact_id",
        (
            f"recognition_artifact_{eval_run_id}",
            eval_run_id,
            "PASS" if passed else "FAIL",
            Jsonb(report),
            digest,
        ),
    ).fetchone()
    if inserted is None:
        existing = conn.execute(
            "SELECT result,report_json,content_hash FROM recognition_eval_artifact "
            "WHERE eval_run_id=%s",
            (eval_run_id,),
        ).fetchone()
        if existing != ("PASS" if passed else "FAIL", report, digest):
            raise RecognitionEvaluationIntegrityError(
                "existing recognition artifact conflicts with recomputed evidence")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recognition-release-eval")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--stability-case-count", type=int, default=10)
    run.add_argument("--repeat-count", type=int, default=1)
    run.add_argument("--token-budget", type=int, default=2_000_000)
    run.add_argument("--cost-budget", type=Decimal, default=Decimal("500"))
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--eval-run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = os.environ.get("FEATUREGEN_DSN") or os.environ.get("FEATUREGEN_DB_DSN")
    if not dsn:
        raise RuntimeError("FEATUREGEN_DSN is required")
    with psycopg.connect(dsn) as conn:
        if args.command == "run":
            run_id = create_evaluation_run(
                conn,
                RecognitionEvaluationConfiguration(
                    runner_kind="REAL_PROVIDER",
                    stability_case_count=args.stability_case_count,
                    repeat_count=args.repeat_count,
                    token_budget=args.token_budget,
                    cost_budget=args.cost_budget,
                    created_by={"subject": "service:recognition-release-eval"},
                ),
            )
            execute_evaluation_run(conn, run_id, current_llm_client())
            report = evaluate_persisted_run(conn, run_id)
        else:
            run_id = args.eval_run_id
            report = evaluate_persisted_run(conn, run_id)
        print(json.dumps({"eval_run_id": run_id, **report}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

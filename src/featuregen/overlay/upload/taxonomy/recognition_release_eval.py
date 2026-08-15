"""Durable 100-case release evaluation for recognition and applicability.

**Versioned with the recognition contract (repair seam, Task 6).** An evaluation is a claim about a
PLATFORM, and this one was rewritten because it had begun measuring a platform that no longer exists
(the plan's blocker B3): pinned to schema v1 while the recognizer dispatched the frozen v2 contract,
and scoring by re-running the all-or-nothing validator over ``llm_call.raw_output`` — so a
recognition the platform SERVED as a partial recovery, with a real scope the user could confirm, was
counted as a technical failure. Conservative, but a gate that under-reports success is still a gate
measuring the wrong thing.

Two rules now hold this straight:

* **The evaluator scores the SERVED, normalized result** — the ``RecognitionResult`` the platform
  actually handed its caller, including the partition's survivors and its drops — and re-derives it
  from the immutable ``llm_call`` row rather than trusting the attempt. ``raw_output`` stays exactly
  where it was, as the audit of what the provider said; the two are different questions.
* **The evaluator refuses a run recorded under a contract it was not written for.** It scores one
  (schema, prompt, semantic-validator) triple, pinned in code; a run stamped with any other is not
  scored at all, because a number carrying the wrong contract is worse than no number.

Running the real 100-case provider gate is an OPERATOR action with real spend — see the module's
``main`` and the plan's Task 6 acceptance for the exact command and its arithmetic.
"""
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
from featuregen.intake.llm import FakeLLM
from featuregen.intake.llm_claude import ClaudeConfig, ClaudeLLM, build_claude_llm
from featuregen.overlay.upload.enrich_llm import (
    FAILURE_KIND_SEMANTIC_INVALID,
    AuditedStructuredResult,
    current_enrichment_generation_settings,
    register_enrichment_schemas,
)
from featuregen.overlay.upload.recipe_formula_shadow import content_hash
from featuregen.overlay.upload.taxonomy.applicability import (
    in_scope_recipes,
    scope_from_recognition,
)
from featuregen.overlay.upload.taxonomy.gold_recognition import (
    TARGET_GOLD,
    validate_target_gold,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    APPLICABILITY_MAPPING_VERSION,
    RECIPE_REGISTRY_VERSION,
    TAXONOMY_VERSION,
    RecognitionQuality,
    RecognitionResult,
    RecognitionStatus,
    recognition_quality,
    unscoped_result,
    validate_recognition_output,
)
from featuregen.overlay.upload.taxonomy.recognizer import (
    RECOGNIZER_TASK,
    AuditedRecognition,
    recognize_with_audit,
)
from featuregen.overlay.upload.taxonomy.recognizer import (
    # Deliberately the recognizer's OWN decision helpers, private names and all. This module
    # re-derives what the platform served from durable evidence, and a second implementation of
    # "what would we have served?" is precisely the drift that made the old evaluator score a
    # partial recovery as a technical failure. One implementation, two callers.
    _partial_recovery as _recognizer_partial_recovery,
)
from featuregen.overlay.upload.taxonomy.recognizer import (
    _result_from_output as _recognizer_result_from_output,
)
from featuregen.overlay.upload.taxonomy.recognizer_prompt import (
    PROMPT_ID,
    PROMPT_VERSION,
    build_recognition_prompt,
)

CORPUS_VERSION = "recognition-four-objective-v1"
# v2 — this evaluator scores the SERVED result under the frozen v2 contract. The version rides on
# every artifact, so a stored report says which platform it measured rather than leaving a reader to
# assume it was this one.
EVALUATOR_VERSION = "recognition-release-evaluator-v2"
SCHEMA_ID = "use_case_recognition"

# ── the recognition contract this evaluator was WRITTEN FOR ──────────────────────────────────────
# Both are pinned LITERALS, not reads of the recognizer's constants, and a test asserts each equals
# what the platform actually dispatches. A derived value would silently follow a contract change and
# keep reporting; a literal makes the change fail CI and land on a human, which is the point — what
# "a correct recognition" means moved, so whether this evaluator still measures it is a judgement.
SCHEMA_VERSION = 2
#: The semantic validator whose accept/reject set this evaluator's scoring assumes
#: (`recognition.RECOGNITION_VALIDATOR_VERSION`). It is not stamped on a run row — it is a property
#: of the CODE that scores, so it is pinned here and drift-tested rather than compared per run.
SCORED_VALIDATOR_VERSION = "2"

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


# `_primary` / `_scope_from_output` — a second, raw-output-shaped reading of "what scope is this?" —
# were DELETED here rather than left to rot. Scoring now goes through
# `applicability.scope_from_recognition` over the SERVED result, which is the function the platform
# itself uses to decide what a recognition scopes to; keeping a parallel one that reads the provider's
# raw body is how the evaluator came to disagree with the platform in the first place.


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


def _served_json(result: RecognitionResult, quality: RecognitionQuality) -> dict[str, Any]:
    """The SERVED recognition, serialized — what the platform handed its caller, not what the
    provider said. The provider's words are already durable in ``llm_call.raw_output``, and the two
    answer different questions: "was the model right?" and "what did the user get?". Scoring the
    second is the whole of B3's fix, and the drops and the quality ride along because a partial
    recovery is not describable without them."""
    return {
        "status": result.status.value,
        "candidates": [
            {
                "use_case_id": candidate.use_case_id,
                "relationship": candidate.relationship,
                "confidence": candidate.confidence,
                "evidence_spans": list(candidate.evidence_spans),
                "rationale": candidate.rationale,
            }
            for candidate in result.candidates
        ],
        "modelling_contexts": list(result.modelling_contexts),
        "target_entity": result.target_entity,
        "warnings": list(result.warnings),
        "dropped_candidates": [
            {"index": drop.index, "reason_code": drop.reason_code}
            for drop in result.dropped_candidates
        ],
        "quality": {
            "disposition": quality.disposition.value,
            "repair_attempts": quality.repair_attempts,
            "dropped_candidate_count": quality.dropped_candidate_count,
            "drop_reason_codes": list(quality.drop_reason_codes),
        },
    }


def _repair_turns(call: Any) -> int:
    """Turns on which the MODEL was asked to fix its own answer, read from the immutable ledger on
    the audit row. ``retry`` entries (truncation, transient faults) are not corrections and are not
    counted — the repair RATE is a claim about how often the model got it wrong, not about how often
    the network did."""
    ledger = call if isinstance(call, list) else []
    return sum(1 for entry in ledger
               if isinstance(entry, dict) and entry.get("class") == "repair")


def _schema_valid(conn, body: Any) -> bool:
    """Does this body satisfy the REGISTERED v2 contract? The seam exposes a body for partition only
    on the semantic arm; a schema-invalid body is discarded whole, so re-deriving what was served
    needs the same fork in the road."""
    if not isinstance(body, dict):
        return False
    try:
        DocumentSchemaRegistry(conn).validate(SCHEMA_ID, SCHEMA_VERSION, body)
    except Exception:
        return False
    return True


def _project_served(conn, *, body: Any, repair_turns: int,
                    model: str) -> tuple[RecognitionResult, RecognitionQuality]:
    """Re-derive what the platform SERVED for this final provider body, from durable evidence only.

    This is the integrity half of scoring the served result: the attempt row says what was served,
    and this says what MUST have been served given the audit row — the final body plus its repair
    ledger. It walks the recognizer's own fork (schema-valid → semantics → partition) using the
    recognizer's own helpers, so there is exactly one implementation of the platform's decision.

    An eval run dispatches with no ``dispatch_audit`` context, so the audit-degraded arm cannot fire
    and every non-served body is a schema/semantic/provider failure. A mismatch is therefore a real
    anomaly — a platform bug during the gate run — and the caller is right to fail on it."""
    result: RecognitionResult | None = None
    if _schema_valid(conn, body):
        try:
            validate_recognition_output(body)
        except Exception:
            # Repair exhausted on the SEMANTIC arm: the seam exposes this final body and the
            # recognizer partitions it. Reconstruct exactly that seam disposition.
            result = _recognizer_partial_recovery(
                AuditedStructuredResult(
                    output=None, llm_call_ref=None, provider_calls=0, usage={},
                    failure_kind=FAILURE_KIND_SEMANTIC_INVALID,
                    last_schema_valid_semantic_invalid_output=dict(body),
                    repair_attempts=repair_turns),
                model=model)
        else:
            result = _recognizer_result_from_output(body, model_id=model)
    if result is None:
        result = unscoped_result("recognition failed or egress-blocked", model_id=model,
                                 prompt_version=PROMPT_VERSION, technical=True)
    return result, recognition_quality(result, repair_attempts=repair_turns)


def _record_attempt(
    conn,
    *,
    eval_run_id: str,
    case,
    repeat_index: int,
    audited: AuditedRecognition,
) -> None:
    """Persist ONE scored attempt from the recognition the platform actually served.

    B3, closed: this used to re-run the all-or-nothing validator over ``llm_call.raw_output`` and
    call anything it rejected a technical failure — so a partial recovery, which by definition
    carries a body that validator rejects, was scored as a failure while the user was looking at a
    confirmable scope. The disposition is now the platform's own."""
    llm_call_ref = audited.llm_call_ref
    with conn.cursor(row_factory=dict_row) as cursor:
        call = cursor.execute(
            "SELECT * FROM llm_call WHERE llm_call_ref=%s",
            (llm_call_ref,),
        ).fetchone()
    if call is None:
        raise RecognitionEvaluationIntegrityError("recognition LLM audit row is missing")
    result = audited.result
    material = _served_json(result, audited.quality)
    status = result.status.value
    technical = result.status is RecognitionStatus.TECHNICAL_FAILURE
    abstained = result.status is RecognitionStatus.UNSCOPED
    primary = next(
        (c.use_case_id for c in result.candidates if c.relationship == "primary"), None)
    # False narrowing is a property of the SCOPE the user was given. A fail-open result narrows
    # nothing (`in_scope_recipes` returns every recipe), so it is not a narrowing — it is a technical
    # failure or an abstention, which the gate counts on their own axes. The old code marked those
    # `true` and double-charged them.
    primary_recipes, supporting_recipes = in_scope_recipes(scope_from_recognition(result))
    retained = primary_recipes | supporting_recipes
    false_narrowing = any(
        recipe_id not in retained for recipe_id in case.expected_relevant_recipes)
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
                audited=audited,
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


def _verify_contract(run: dict) -> None:
    """Refuse a run recorded under a recognition contract this evaluator was not written for.

    The FIRST thing scoring does, before anything else is said about the run — including whether it
    was a fake one. An evaluator that cannot interpret a run has nothing to report about it, and a
    number carrying the wrong contract is worse than no number: it looks like evidence.

    What "the contract" is here is the (schema id, schema version, prompt version) triple stamped on
    the run, against what this build dispatches. The prompt CONTENT and the corpus are checked
    separately in :func:`_verify_run`; the semantic validator is not on the row at all — it is a
    property of the scoring code, pinned as :data:`SCORED_VALIDATOR_VERSION` and drift-tested."""
    if (
        run["schema_id"] != SCHEMA_ID
        or run["schema_version"] != SCHEMA_VERSION
        or run["prompt_version"] != int(PROMPT_VERSION)
    ):
        raise RecognitionEvaluationIntegrityError(
            "this evaluator scores recognition contract "
            f"({SCHEMA_ID} v{SCHEMA_VERSION}, prompt v{PROMPT_VERSION}, validator "
            f"v{SCORED_VALIDATOR_VERSION}); the run was recorded under "
            f"({run['schema_id']} v{run['schema_version']}, prompt v{run['prompt_version']})")


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
    _verify_contract(run)                    # before anything else is claimed about this run
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
    repair_turns_by_ref: dict[str, int] = {}
    retained_relevant = 0
    total_relevant = 0
    for attempt in attempts:
        by_case.setdefault(attempt["case_id"], []).append(attempt)
        call = conn.execute(
            "SELECT task,provider,model,prompt_id,prompt_version,output_schema_id,"
            "output_schema_version,generation_settings,raw_output,cost_metadata,repair_attempts "
            "FROM llm_call WHERE llm_call_ref=%s",
            (attempt["llm_call_ref"],),
        ).fetchone()
        if call is None:
            raise RecognitionEvaluationIntegrityError("attempt LLM call is missing")
        raw = call[8]
        output = raw.get("output") if isinstance(raw, dict) else None
        tokens_in, tokens_out, cost = _usage(call[9])
        repair_turns = _repair_turns(call[10])
        repair_turns_by_ref[attempt["llm_call_ref"]] = repair_turns
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
        # THE RE-DERIVATION. The attempt says what was served; this says what MUST have been served,
        # computed from the immutable audit row alone (the final body + its repair ledger) through
        # the recognizer's own decision helpers. It replaces a check that re-ran the all-or-nothing
        # validator over the raw body and called every rejection a technical failure — which is how
        # a partial recovery, whose body that validator rejects BY CONSTRUCTION, was scored as a
        # failure the platform never had.
        served_result, served_quality = _project_served(
            conn, body=output, repair_turns=repair_turns, model=run["model"])
        served = _served_json(served_result, served_quality)
        if (
            served != attempt["recognition_json"]
            or content_hash(served) != attempt["recognition_hash"]
            or (tokens_in, tokens_out, cost)
            != (
                attempt["input_tokens"],
                attempt["output_tokens"],
                attempt["cost_amount"],
            )
        ):
            raise RecognitionEvaluationIntegrityError(
                "recognition attempt conflicts with its immutable LLM audit")
        case = case_by_id[attempt["case_id"]]
        primary_recipes, supporting_recipes = in_scope_recipes(
            scope_from_recognition(served_result))
        retained = primary_recipes | supporting_recipes
        kept_relevant = [recipe_id for recipe_id in case.expected_relevant_recipes
                         if recipe_id in retained]
        if attempt["repeat_index"] == 0:
            # Applicability RECALL is a ratio over recipes, so it is accumulated on the 100 primary
            # observations only — the stability repeats are a different denominator and would
            # silently reweight the cases that happen to be repeated.
            retained_relevant += len(kept_relevant)
            total_relevant += len(case.expected_relevant_recipes)
        if (
            attempt["technical_failure"] != (
                served_result.status is RecognitionStatus.TECHNICAL_FAILURE)
            or attempt["abstained"] != (served_result.status is RecognitionStatus.UNSCOPED)
            or attempt["status"] != served_result.status.value
            or attempt["recognized_primary"] != next(
                (c.use_case_id for c in served_result.candidates
                 if c.relationship == "primary"), None)
            or attempt["false_narrowing"] != (
                len(kept_relevant) != len(case.expected_relevant_recipes))
        ):
            raise RecognitionEvaluationIntegrityError(
                "persisted recognition outcome was not derived from provider output")

    report = _score_report(
        eval_run_id=eval_run_id, run=run, attempts=attempts, primary=primary,
        by_case=by_case, case_by_id=case_by_id, repair_turns_by_ref=repair_turns_by_ref,
        retained_relevant=retained_relevant, total_relevant=total_relevant)
    _persist_artifact(conn, eval_run_id=eval_run_id, report=report)
    return report


def _score_report(
    *,
    eval_run_id: str,
    run: dict,
    attempts: list[dict],
    primary: list[dict],
    by_case: dict[str, list[dict]],
    case_by_id: dict[str, Any],
    repair_turns_by_ref: dict[str, int],
    retained_relevant: int,
    total_relevant: int,
) -> dict[str, Any]:
    """The report ARITHMETIC, over rows whose integrity :func:`evaluate_persisted_run` has already
    established. Pure and DB-free on purpose.

    It is a separate function because the scoring body of a release evaluation is otherwise
    unreachable in a test: an evaluation only scores a REAL_PROVIDER run, and a real provider run is
    an operator action with real spend that no test may take. Splitting the maths from the evidence
    checks lets the numbers an operator will read be exercised against hand-built rows, without
    weakening the rule that a fake run can never produce a provider-qualified artifact — which is
    the boundary that makes every one of these numbers mean anything."""
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
    # Applicability RECALL: expected-relevant recipes retained / expected-relevant total, over the
    # 100 primary observations. `false_narrowings == 0` and `recall == 1.0` are the same statement
    # counted differently (cases vs recipes), so recall is reported for diagnosis and deliberately
    # NOT added to `passed` — a second gate saying the same thing would only look like more evidence.
    applicability_recall = 1.0 if total_relevant == 0 else retained_relevant / total_relevant
    # REPAIR RATE (new). Since Task 1's frozen enum and Task 3's semantic validator inside the loop,
    # a body the platform used to discard whole is now re-prompted — so the same corpus costs more
    # provider calls than it did, and how much more is a fact somebody has to measure before they
    # can budget for it. It is REPORTED, never gated: what an acceptable repair rate is, is an
    # operator's call about cost and model choice, and this plan has not made it.
    dispositions = [row["recognition_json"].get("quality", {}) for row in attempts]
    repair_turns_total = sum(
        repair_turns_by_ref.get(row["llm_call_ref"], 0) for row in attempts)
    attempts_with_repair = sum(
        1 for row in attempts if repair_turns_by_ref.get(row["llm_call_ref"], 0) > 0)
    repair_rate = attempts_with_repair / len(attempts) if attempts else 0.0
    partially_recovered = sum(
        1 for q in dispositions if q.get("disposition") == "partially_recovered")
    repaired = sum(1 for q in dispositions if q.get("disposition") == "repaired")
    dropped_candidates_total = sum(
        int(q.get("dropped_candidate_count", 0) or 0) for q in dispositions)
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
        # The contract this number is ABOUT. A report that does not say which platform it measured
        # is the defect this evaluator was rewritten for (B3), one artifact later.
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": int(PROMPT_VERSION),
        "semantic_validator_version": SCORED_VALIDATOR_VERSION,
        "eval_run_id": eval_run_id,
        "primary_observations": 100,
        "false_narrowings": false_narrowings,
        "false_narrowing_wilson_upper_one_sided_95": wilson_upper,
        "applicability_recall": applicability_recall,
        "technical_failures": technical,
        "abstentions": abstentions,
        "wrong_primary": wrong_primary,
        "stability_cases": stability_required,
        "stable_cases": stable,
        "attempts": len(attempts),
        "attempts_with_repair": attempts_with_repair,
        "repair_turns_total": repair_turns_total,
        "repair_rate": repair_rate,
        "repaired": repaired,
        "partially_recovered": partially_recovered,
        "dropped_candidates_total": dropped_candidates_total,
        "total_tokens": total_tokens,
        "total_cost": str(total_cost),
        "budget_ok": budget_ok,
        "passed": passed,
    }
    return report


def _persist_artifact(conn, *, eval_run_id: str, report: dict[str, Any]) -> None:
    """Write the immutable artifact for this evaluation, or verify the one already there.

    Re-evaluating a run must reach the same verdict from the same evidence — the append-only tables
    make that checkable rather than assumed, and a disagreement is an integrity failure, not a
    refresh."""
    passed = bool(report["passed"])
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


#: What `run` COSTS, stated where the operator running it will see it. `--help` is the right place
#: for this: the command spends real money against a real provider, and the number of calls is not
#: 100 — it is 100 plus the stability repeats, each of which may cost repair turns.
_SPEND_EPILOG = """\
`run` CALLS A REAL PROVIDER. Budget, in physical provider requests:

  logical recognitions = 100 corpus cases + (--stability-case-count x --repeat-count)
                       = 110 at the defaults (10 x 1)

  physical requests per logical recognition
                       = 1 + repair turns (budget 2) + retry turns (budget 2)

  floor    110  every case answered acceptably on its first turn
  expected 110 + 2 x (repair rate) x 110    repairs are the cost of asking the model to fix
                                            a body the platform used to discard whole
  ceiling  330  every case exhausting its repair budget (550 if retries also fire on every
                case, which needs a provider fault, not a model one)

Token/cost budgets are enforced from the run row (--token-budget, --cost-budget) and reported as
`budget_ok`; the REPAIR RATE is reported and deliberately not gated — what rate is acceptable is an
operator's judgement about cost and model choice.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recognition-release-eval",
        epilog=_SPEND_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", epilog=_SPEND_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    run.add_argument("--stability-case-count", type=int, default=10)
    run.add_argument("--repeat-count", type=int, default=1)
    run.add_argument("--token-budget", type=int, default=2_000_000)
    run.add_argument("--cost-budget", type=Decimal, default=Decimal("500"))
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--eval-run-id", required=True)
    return parser


def _provider_client():
    """The real adapter this command dispatches through, built from the environment.

    ``current_llm_client()`` was called here and NOTHING in this process ever registers one — only
    ``featuregen worker`` does — so the documented gate command failed with "no LLMClient
    registered" before it reached the provider. Built explicitly instead of registered globally: the
    only caller that needs it is two lines below, and a process-wide default is how a fake client
    ends up somewhere it was never meant to be. Fails CLOSED when the adapter is not configured —
    an evaluation that quietly ran against something else would be worse than one that did not run."""
    config = ClaudeConfig.from_env()
    if not config.enabled:
        raise RecognitionEvaluationIntegrityError(
            "the real provider gate needs FEATUREGEN_LLM_PROVIDER=anthropic (with "
            "ANTHROPIC_API_KEY and, optionally, FEATUREGEN_LLM_MODEL) — refusing to run a "
            "REAL_PROVIDER evaluation without the configured Anthropic adapter")
    return build_claude_llm(config)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = os.environ.get("FEATUREGEN_DSN") or os.environ.get("FEATUREGEN_DB_DSN")
    if not dsn:
        raise RuntimeError("FEATUREGEN_DSN is required")
    with psycopg.connect(dsn) as conn:
        if args.command == "run":
            client = _provider_client()      # before the run row: fail closed costs nothing
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
            execute_evaluation_run(conn, run_id, client)
            report = evaluate_persisted_run(conn, run_id)
        else:
            run_id = args.eval_run_id
            report = evaluate_persisted_run(conn, run_id)
        print(json.dumps({"eval_run_id": run_id, **report}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

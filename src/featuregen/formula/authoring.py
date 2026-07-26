"""End-to-end offline TypedFormula authoring orchestration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.audited import current_formula_generation_settings
from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID, author_formula
from featuregen.formula.capability import (
    CAPABILITY_POLICY_VERSION,
    classify_formula_capability,
)
from featuregen.formula.critic import (
    CRITIC_POLICY_VERSION,
    critique,
)
from featuregen.formula.frozen_configuration import (
    FrozenAuthorCriticConfigurationV1,
    verify_frozen_configuration,
)
from featuregen.formula.output_authority import (
    ExprFacts,
    ExternalRequirement,
    InvalidOutput,
    NeedsAuthority,
    resolve_formula_output_policy,
)
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.result import (
    DISPOSITION_POLICY_VERSION,
    AuthoringAxes,
    AuthorityFailure,
    derive_disposition,
)
from featuregen.formula.schema import (
    OUTPUT_POLICY_VERSION,
    DiffBody,
    RatioBody,
    SchemaError,
    TypedFormulaProposalV1,
    TypedFormulaV1,
    UnaryBody,
)
from featuregen.formula.trace import append_event, open_authoring_run
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.operational_facts import read_operational_value

AUTHORING_ORCHESTRATOR_VERSION = 1


def _actor_json(actor: IdentityEnvelope | None) -> dict | None:
    if actor is None:
        return None
    return {
        "subject": actor.subject,
        "actor_kind": actor.actor_kind,
        "authenticated": actor.authenticated,
        "auth_method": actor.auth_method,
        "role_claims": list(actor.role_claims),
    }


def _intent_material(intent) -> dict:
    return {
        "name": intent.name,
        "hypothesis": intent.hypothesis,
        "target_entity": intent.target_entity,
        "target_grain_keys": list(intent.target_grain_keys),
        "recipe_authoring_context": getattr(intent, "recipe_authoring_context", None),
    }


def _expressions(proposal: TypedFormulaProposalV1):
    body = proposal.body
    if isinstance(body, UnaryBody):
        return (("body.expr", body.expr),)
    if isinstance(body, RatioBody):
        return (("body.numerator", body.numerator), ("body.denominator", body.denominator))
    if isinstance(body, DiffBody):
        return (("body.minuend", body.minuend), ("body.subtrahend", body.subtrahend))
    return ()


def _facts(conn, proposal: TypedFormulaProposalV1):
    per_expr: dict[str, ExprFacts] = {}
    for path, expression in _expressions(proposal):
        if expression.operand is None:
            per_expr[path] = ExprFacts()
            continue
        per_expr[path] = ExprFacts(
            output_type=read_operational_value(
                conn, expression.operand, "logical_representation"),
            additivity=read_operational_value(conn, expression.operand, "additivity"),
            unit=read_operational_value(conn, expression.operand, "unit"),
            currency=read_operational_value(conn, expression.operand, "currency"),
        )
    grain = {
        ref: read_operational_value(conn, ref, "is_grain")
        for ref in proposal.grain.keys
    }
    return per_expr, grain


def _formula(proposal: TypedFormulaProposalV1, output) -> TypedFormulaV1:
    return TypedFormulaV1(
        formula_schema_version=proposal.formula_schema_version,
        operation_grammar_version=proposal.operation_grammar_version,
        output_policy_version=OUTPUT_POLICY_VERSION,
        canonicalization_version=proposal.canonicalization_version,
        grain=proposal.grain,
        body=proposal.body,
        parameters=proposal.parameters,
        decimal=proposal.decimal,
        output=output,
    )


def _expectation_status(proposal: TypedFormulaProposalV1, output) -> str:
    expected = proposal.expected_output
    if expected is None:
        return "not_provided"
    values = (
        (expected.output_type, output.output_type),
        (expected.unit, output.unit),
        (expected.currency, output.currency),
    )
    return "match" if all(want is None or want == got for want, got in values) else "mismatch"


def _terminal_payload(result) -> dict:
    return {
        "authoring_disposition": result.authoring_disposition,
        "candidate_formula_hash": result.candidate_formula_hash,
        "structural_status": result.structural_status,
        "capability_status": result.capability_status,
        "output_status": result.output_status,
        "expectation_status": result.expectation_status,
        "critic_status": result.critic_status,
        "technical_status": result.technical_status,
    }


def _technical_failure(conn, run_id: str, seq: int, reason: str):
    result = derive_disposition(
        AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "technical_failure"),
        authoring_run_id=run_id,
    )
    append_event(
        conn,
        run_id,
        "failed",
        seq=seq,
        idempotency_key=f"{run_id}:terminal",
        payload={**_terminal_payload(result), "reason": reason},
    )
    return result


def run_authoring(
    conn,
    intent,
    author_client,
    critic_client,
    *,
    roles=(),
    actor: IdentityEnvelope | None,
    max_turns: int = 8,
    frozen_configuration: FrozenAuthorCriticConfigurationV1 | None = None,
    proposal_validator: Callable[[TypedFormulaProposalV1], tuple[str, ...]] | None = None,
    tool_runner: Callable[..., dict] | None = None,
    authoring_run_id: str | None = None,
    facts_reader: Callable[[TypedFormulaProposalV1], tuple[dict, dict]] | None = None,
    critic_metadata_loader: Callable[[str], dict] | None = None,
    progress_callback: Callable[[], None] | None = None,
):
    """Author, parse, govern output semantics, independently critique, and fold one result."""
    versions = {
        "orchestrator": AUTHORING_ORCHESTRATOR_VERSION,
        "capability": CAPABILITY_POLICY_VERSION,
        "critic": CRITIC_POLICY_VERSION,
        "disposition": DISPOSITION_POLICY_VERSION,
    }
    if frozen_configuration is not None:
        versions["frozen_configuration_policy"] = (
            frozen_configuration.configuration_policy_version)
        versions["frozen_configuration_hash"] = frozen_configuration.configuration_hash
    run_id = open_authoring_run(
        conn,
        intent_hash=canonical_hash(_intent_material(intent)),
        versions=versions,
        actor=_actor_json(actor),
        authoring_run_id=authoring_run_id,
    )
    if frozen_configuration is not None:
        try:
            current_settings = current_formula_generation_settings()
            verify_frozen_configuration(
                frozen_configuration,
                generation_settings=current_settings,
                author_instruction=AUTHOR_INSTRUCTION,
                author_prompt_id=AUTHOR_PROMPT_ID,
            )
        except Exception as exc:
            return _technical_failure(
                conn, run_id, 0, f"configuration:{type(exc).__name__}")
    seq = 0

    def persist_author_turn(turn) -> None:
        nonlocal seq
        output_hash = canonical_hash(turn.output) if turn.output is not None else None
        tool_result_hash = (
            canonical_hash(turn.tool_result) if turn.tool_result is not None else None)
        append_event(
            conn,
            run_id,
            "author_turn",
            seq=seq,
            idempotency_key=f"{run_id}:author:{turn.index}",
            llm_call_ref=turn.llm_call_ref,
            payload={
                "index": turn.index,
                "kind": turn.kind.value,
                "tool_name": turn.tool_name,
                "provider_calls": turn.provider_calls,
                "usage": turn.usage,
                "output": turn.output,
                "output_hash": output_hash,
                "tool_result": turn.tool_result,
                "tool_result_hash": tool_result_hash,
            },
        )
        seq += 1

    try:
        raw, turns = author_formula(
            conn,
            intent,
            author_client,
            roles=tuple(roles),
            max_turns=max_turns,
            actor=actor,
            authoring_run_id=run_id,
            on_turn=persist_author_turn,
            provider_contract=(
                frozen_configuration.author if frozen_configuration is not None else None),
            progress_callback=progress_callback,
            **({} if tool_runner is None else {"tool_runner": tool_runner}),
        )
    except Exception as exc:
        return _technical_failure(conn, run_id, seq, f"author:{type(exc).__name__}")
    if raw is None:
        result = derive_disposition(
            AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "technical_failure"),
            authoring_run_id=run_id,
        )
        append_event(
            conn, run_id, "failed", seq=seq, idempotency_key=f"{run_id}:terminal",
            payload=_terminal_payload(result))
        return result

    try:
        proposal = parse_proposal_v1(raw)
    except SchemaError as exc:
        result = derive_disposition(
            AuthoringAxes(
                "invalid_formula", "ok", "resolved", "not_provided", "clean", "ok"),
            authoring_run_id=run_id,
        )
        append_event(
            conn, run_id, "failed", seq=seq, idempotency_key=f"{run_id}:terminal",
            payload={**_terminal_payload(result), "reason": str(exc)})
        return result

    if proposal_validator is not None:
        violations = proposal_validator(proposal)
        if violations:
            result = derive_disposition(
                AuthoringAxes(
                    "invalid_formula", "ok", "resolved", "not_provided", "clean", "ok"),
                authoring_run_id=run_id,
            )
            append_event(
                conn,
                run_id,
                "failed",
                seq=seq,
                idempotency_key=f"{run_id}:terminal",
                payload={
                    **_terminal_payload(result),
                    "reason": "recipe_expectation_not_preserved",
                    "violations": list(violations),
                },
            )
            return result

    append_event(
        conn,
        run_id,
        "validation_result",
        seq=seq,
        idempotency_key=f"{run_id}:validation",
        payload={"structural_status": "ok"},
    )
    seq += 1
    capability = classify_formula_capability(proposal)
    if capability != "ok":
        result = derive_disposition(
            AuthoringAxes(
                "ok", "unsupported_capability", "resolved", "not_provided", "clean", "ok"),
            authoring_run_id=run_id,
            capability_reason="multi_source_or_out_of_v1",
        )
        append_event(
            conn, run_id, "completed", seq=seq, idempotency_key=f"{run_id}:terminal",
            payload=_terminal_payload(result))
        return result

    try:
        review = critique(
            conn,
            intent,
            proposal,
            critic_client,
            roles=tuple(roles),
            actor=actor,
            authoring_run_id=run_id,
            provider_contract=(
                frozen_configuration.critic if frozen_configuration is not None else None),
            metadata_loader=critic_metadata_loader,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        return _technical_failure(conn, run_id, seq, f"critic:{type(exc).__name__}")
    findings = review.findings
    findings_hash = review.findings_hash
    critic_failed = review.technical_failure
    append_event(
        conn,
        run_id,
        "critic_result",
        seq=seq,
        idempotency_key=f"{run_id}:critic",
        llm_call_ref=review.llm_call_ref,
        payload={
            "technical_failure": critic_failed,
            "findings_hash": findings_hash,
            "provider_calls": review.provider_calls,
            "usage": review.usage,
            "findings": [
                {"code": finding.code.value, "severity": finding.severity}
                for finding in findings
            ],
        },
    )
    seq += 1
    if critic_failed:
        result = derive_disposition(
            AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "technical_failure"),
            authoring_run_id=run_id,
            critic_findings_hash=findings_hash,
        )
        append_event(
            conn, run_id, "failed", seq=seq, idempotency_key=f"{run_id}:terminal",
            payload=_terminal_payload(result))
        return result

    try:
        if progress_callback is not None:
            progress_callback()
        per_expr, grain_facts = (
            facts_reader(proposal)
            if facts_reader is not None
            else _facts(conn, proposal)
        )
        output = resolve_formula_output_policy(
            proposal,
            per_expr_facts=per_expr,
            grain_facts=grain_facts,
            now=datetime.now(UTC),
        )
    except Exception as exc:
        return _technical_failure(conn, run_id, seq, f"output_authority:{type(exc).__name__}")
    critic_status = (
        "blocking"
        if any(finding.severity == "blocking" for finding in findings)
        else "advisory"
        if findings
        else "clean"
    )
    if isinstance(output, NeedsAuthority):
        result = derive_disposition(
            AuthoringAxes("ok", "ok", "needs_authority", "not_provided", critic_status, "ok"),
            authoring_run_id=run_id,
            candidate_proposal=proposal,
            authority_failures=(AuthorityFailure(output.reason),),
            critic_findings_hash=findings_hash,
        )
    elif isinstance(output, ExternalRequirement):
        result = derive_disposition(
            AuthoringAxes(
                "ok", "ok", "external_requirement", "not_provided", critic_status, "ok"),
            authoring_run_id=run_id,
            candidate_proposal=proposal,
            output_requirements=(output,),
            critic_findings_hash=findings_hash,
        )
    elif isinstance(output, InvalidOutput):
        result = derive_disposition(
            AuthoringAxes("ok", "ok", "invalid_output", "not_provided", critic_status, "ok"),
            authoring_run_id=run_id,
            critic_findings_hash=findings_hash,
        )
    else:
        formula = _formula(proposal, output)
        result = derive_disposition(
            AuthoringAxes(
                "ok",
                "ok",
                "resolved",
                _expectation_status(proposal, output),
                critic_status,
                "ok",
            ),
            authoring_run_id=run_id,
            candidate_formula=formula,
            critic_findings_hash=findings_hash,
        )
    append_event(
        conn,
        run_id,
        "completed",
        seq=seq,
        idempotency_key=f"{run_id}:terminal",
        payload=_terminal_payload(result),
    )
    return result

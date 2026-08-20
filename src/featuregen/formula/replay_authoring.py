"""End-to-end offline TypedFormula authoring orchestration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal, cast

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.audited import current_formula_generation_settings
from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID, author_formula
from featuregen.formula.authoring_result_leaves import (
    DISPOSITION_POLICY_VERSION,
    AuthoringAxes,
    AuthorityFailure,
)
from featuregen.formula.capability import (
    CAPABILITY_POLICY_VERSION,
    classify_formula_capability,
)
from featuregen.formula.control import (
    FormulaControlFlow,
    LeaseFence,
    RecoveryRequiresReconciliation,
)
from featuregen.formula.critic import (
    CRITIC_POLICY_VERSION,
    CriticFinding,
    CriticFindingCode,
    CriticReview,
    critique,
)
from featuregen.formula.frozen_configuration import FrozenAuthorCriticConfigurationV1
from featuregen.formula.frozen_configuration_v1 import verify_frozen_configuration
from featuregen.formula.intent_material import _intent_material
from featuregen.formula.output_authority import (
    ExprFacts,
    ExternalRequirement,
    InvalidOutput,
    NeedsAuthority,
    resolve_formula_output_policy,
)
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.replay_trace import (
    append_event,
    load_verified_checkpoint,
    open_authoring_run,
)
from featuregen.formula.result import AuthoringResult, derive_disposition
from featuregen.formula.schema import (
    OUTPUT_POLICY_VERSION,
    DiffBody,
    FormulaOutputPolicyV1,
    RatioBody,
    TypedFormulaProposalV1,
    TypedFormulaV1,
    UnaryBody,
)
from featuregen.formula.schema_leaves import AdditivityClass, SchemaError
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


def _expectation_status(
    proposal: TypedFormulaProposalV1, output
) -> Literal["match", "mismatch", "not_provided"]:
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
        "result": _plain(result),
    }


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _restore_terminal_result(checkpoint, run_id: str) -> AuthoringResult:
    terminal = checkpoint.terminal_result
    if not isinstance(terminal, dict) or not isinstance(terminal.get("result"), dict):
        raise RecoveryRequiresReconciliation(
            "terminal authoring result is not replayable")
    material = terminal["result"]
    try:
        axes = AuthoringAxes(
            cast(Any, material["structural_status"]),
            cast(Any, material["capability_status"]),
            cast(Any, material["output_status"]),
            cast(Any, material["expectation_status"]),
            cast(Any, material["critic_status"]),
            cast(Any, material["technical_status"]),
        )
        proposal_material = material.get("candidate_proposal")
        proposal = (
            parse_proposal_v1(proposal_material)
            if isinstance(proposal_material, dict)
            else None
        )
        formula = None
        if isinstance(material.get("candidate_formula"), dict):
            source_proposal = proposal or (
                parse_proposal_v1(checkpoint.raw_proposal)
                if checkpoint.raw_proposal is not None
                else None
            )
            output_material = checkpoint.output_policy_result
            if source_proposal is None or output_material is None:
                raise RecoveryRequiresReconciliation(
                    "resolved terminal is missing proposal or output policy")
            output = FormulaOutputPolicyV1(
                output_type=str(output_material["output_type"]),
                unit=output_material.get("unit"),
                currency=output_material.get("currency"),
                output_additivity=AdditivityClass(
                    output_material["output_additivity"]),
                external_type_required=bool(
                    output_material["external_type_required"]),
            )
            formula = _formula(source_proposal, output)
            proposal = None
        result = derive_disposition(
            axes,
            authoring_run_id=run_id,
            candidate_formula=formula,
            candidate_proposal=proposal,
            output_requirements=tuple(
                ExternalRequirement(str(item["requirement"]))
                for item in material.get("output_requirements", ())
            ),
            authority_failures=tuple(
                AuthorityFailure(
                    reason=str(item["reason"]),
                    operand=item.get("operand"),
                    field=item.get("field"),
                )
                for item in material.get("authority_failures", ())
            ),
            capability_reason=material.get("capability_reason"),
            critic_findings_hash=material.get("critic_findings_hash"),
        )
    except RecoveryRequiresReconciliation:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryRequiresReconciliation(
            "terminal authoring result has invalid typed material") from exc
    if result.candidate_formula_hash != terminal.get("candidate_formula_hash"):
        raise RecoveryRequiresReconciliation(
            "terminal formula content hash changed during replay")
    return result


def _technical_failure(
    conn,
    run_id: str,
    seq: int,
    reason: str,
    *,
    lease_fence: LeaseFence | None = None,
):
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
        stage="TERMINAL",
        lease_fence=lease_fence,
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
    lease_fence: LeaseFence | None = None,
):
    """Author, parse, govern output semantics, independently critique, and fold one result."""
    versions: dict[str, Any] = {
        "orchestrator": AUTHORING_ORCHESTRATOR_VERSION,
        "capability": CAPABILITY_POLICY_VERSION,
        "critic": CRITIC_POLICY_VERSION,
        "disposition": DISPOSITION_POLICY_VERSION,
    }
    if frozen_configuration is not None:
        versions["frozen_configuration_policy"] = (
            frozen_configuration.configuration_policy_version)
        versions["frozen_configuration_hash"] = frozen_configuration.configuration_hash
    intent_hash = canonical_hash(_intent_material(intent))
    run_id = open_authoring_run(
        conn,
        intent_hash=intent_hash,
        versions=versions,
        actor=_actor_json(actor),
        authoring_run_id=authoring_run_id,
        lease_fence=lease_fence,
    )
    checkpoint = load_verified_checkpoint(
        conn,
        run_id,
        intent_hash=intent_hash,
        versions=versions,
    )
    if checkpoint.terminal_result is not None:
        return _restore_terminal_result(checkpoint, run_id)
    if frozen_configuration is not None:
        try:
            current_settings = current_formula_generation_settings()
            verify_frozen_configuration(
                frozen_configuration,
                generation_settings=current_settings,
                author_instruction=AUTHOR_INSTRUCTION,
                author_prompt_id=AUTHOR_PROMPT_ID,
            )
        except FormulaControlFlow:
            raise
        except Exception as exc:
            return _technical_failure(
                conn,
                run_id,
                0,
                f"configuration:{type(exc).__name__}",
                lease_fence=lease_fence,
            )
    seq = checkpoint.next_seq

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
            stage=f"AUTHOR_TURN_{turn.index}",
            logical_turn_index=turn.index,
            tool_context_hash=turn.tool_context_hash,
            canonical_output_hash=output_hash,
            lease_fence=lease_fence,
            payload={
                "index": turn.index,
                "kind": turn.kind.value,
                "llm_call_ref": turn.llm_call_ref,
                "tool_name": turn.tool_name,
                "provider_calls": turn.provider_calls,
                "usage": turn.usage,
                "output": turn.output,
                "output_hash": output_hash,
                "tool_result": turn.tool_result,
                "tool_result_hash": tool_result_hash,
                "tool_context_hash": turn.tool_context_hash,
            },
        )
        seq += 1

    if checkpoint.raw_proposal is None:
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
                lease_fence=lease_fence,
                resume_turns=checkpoint.author_turns,
                **({} if tool_runner is None else {"tool_runner": tool_runner}),
            )
        except FormulaControlFlow:
            raise
        except Exception as exc:
            return _technical_failure(
                conn,
                run_id,
                seq,
                f"author:{type(exc).__name__}",
                lease_fence=lease_fence,
            )
    else:
        raw = checkpoint.raw_proposal
    if raw is None:
        result = derive_disposition(
            AuthoringAxes("ok", "ok", "resolved", "not_provided", "clean", "technical_failure"),
            authoring_run_id=run_id,
        )
        append_event(
            conn, run_id, "failed", seq=seq, idempotency_key=f"{run_id}:terminal",
            payload=_terminal_payload(result), stage="TERMINAL", lease_fence=lease_fence)
        return result

    try:
        proposal = parse_proposal_v1(checkpoint.raw_proposal or raw)
    except SchemaError as exc:
        result = derive_disposition(
            AuthoringAxes(
                "invalid_formula", "ok", "resolved", "not_provided", "clean", "ok"),
            authoring_run_id=run_id,
        )
        append_event(
            conn, run_id, "failed", seq=seq, idempotency_key=f"{run_id}:terminal",
            payload={**_terminal_payload(result), "reason": str(exc)},
            stage="TERMINAL",
            lease_fence=lease_fence,
        )
        return result
    if checkpoint.raw_proposal is None:
        append_event(
            conn,
            run_id,
            "validation_result",
            seq=seq,
            idempotency_key=f"{run_id}:proposal",
            payload={"proposal": raw, "proposal_hash": canonical_hash(raw)},
            stage="AUTHOR_PROPOSAL_PARSED",
            canonical_output_hash=canonical_hash(raw),
            lease_fence=lease_fence,
        )
        seq += 1

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
                stage="TERMINAL",
                lease_fence=lease_fence,
            )
            return result

    expectation_already_validated = any(
        event["stage"] == "EXPECTATION_VALIDATED" for event in checkpoint.events)
    if not expectation_already_validated:
        append_event(
            conn,
            run_id,
            "validation_result",
            seq=seq,
            idempotency_key=f"{run_id}:validation",
            payload={"structural_status": "ok"},
            stage="EXPECTATION_VALIDATED",
            lease_fence=lease_fence,
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
            payload=_terminal_payload(result), stage="TERMINAL", lease_fence=lease_fence)
        return result

    if checkpoint.critic_result is None:
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
                lease_fence=lease_fence,
            )
        except FormulaControlFlow:
            raise
        except Exception as exc:
            return _technical_failure(
                conn,
                run_id,
                seq,
                f"critic:{type(exc).__name__}",
                lease_fence=lease_fence,
            )
    else:
        restored_findings_list: list[CriticFinding] = []
        for item in checkpoint.critic_result.get("findings", ()):
            severity = item["severity"]
            if severity not in ("blocking", "advisory"):
                raise FormulaControlFlow("replayed critic severity is invalid")
            restored_findings_list.append(CriticFinding(
                code=CriticFindingCode(item["code"]),
                severity=severity,
                operand=item.get("operand"),
                detail=item.get("detail"),
            ))
        restored_findings = tuple(restored_findings_list)
        review = CriticReview(
            findings=restored_findings,
            findings_hash=str(checkpoint.critic_result["findings_hash"]),
            technical_failure=bool(
                checkpoint.critic_result["technical_failure"]),
            llm_call_ref=None,
            provider_calls=0,
            usage={},
        )
    findings = review.findings
    findings_hash = review.findings_hash
    critic_failed = review.technical_failure
    if checkpoint.critic_result is None:
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
                    {
                        "code": finding.code.value,
                        "severity": finding.severity,
                        "operand": finding.operand,
                        "detail": finding.detail,
                    }
                    for finding in findings
                ],
            },
            stage="CRITIC_COMPLETED",
            logical_turn_index=0,
            canonical_output_hash=findings_hash,
            lease_fence=lease_fence,
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
            payload=_terminal_payload(result), stage="TERMINAL", lease_fence=lease_fence)
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
    except FormulaControlFlow:
        raise
    except Exception as exc:
        return _technical_failure(
            conn,
            run_id,
            seq,
            f"output_authority:{type(exc).__name__}",
            lease_fence=lease_fence,
        )
    output_payload = _plain(output)
    if checkpoint.output_policy_result is None:
        append_event(
            conn,
            run_id,
            "validation_result",
            seq=seq,
            idempotency_key=f"{run_id}:output-policy",
            payload={"result": output_payload},
            stage="OUTPUT_POLICY_RESOLVED",
            canonical_output_hash=canonical_hash(output_payload),
            lease_fence=lease_fence,
        )
        seq += 1
    elif checkpoint.output_policy_result != output_payload:
        raise RecoveryRequiresReconciliation("replayed output policy changed")
    critic_status: Literal["clean", "advisory", "blocking"] = (
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
        stage="TERMINAL",
        lease_fence=lease_fence,
    )
    return result

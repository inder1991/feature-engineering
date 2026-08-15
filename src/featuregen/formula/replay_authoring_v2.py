"""The REPLAY-SHAPED Formula-v2 authoring orchestrator — the one the live worker can call.

``replay_authoring.run_authoring`` is the orchestrator production actually runs
(``recipe_formula_worker.py:52``), and A3's plan defect 1 recorded why ``authoring_v2`` could not
be wired into it: the live path is not the 300-line ``authoring.run_authoring`` that
``run_authoring_v2`` siblings, it is a 684-line machine carrying **checkpoint/replay, a frozen
configuration, an injected proposal validator, an injected tool runner, an injected facts reader,
a deterministic ``authoring_run_id``, a critic metadata loader, a progress callback and a lease
fence**. This module is that machine's v2 sibling.

WHAT IS COPIED, DELIBERATELY, AND WHAT IS NOT
---------------------------------------------
The ORCHESTRATION LAWS are ``replay_authoring``'s, restated over the v2 types rather than
reinvented — the stage vocabulary (``AUTHOR_TURN_n`` → ``AUTHOR_PROPOSAL_PARSED`` →
``EXPECTATION_VALIDATED`` → ``CRITIC_COMPLETED`` → ``OUTPUT_POLICY_RESOLVED`` → ``TERMINAL``) is
not a style choice: ``replay_trace._verify_stage_transition`` ENFORCES exactly that order, so a v2
run that invented its own stage names could never be replayed. Every resume point, every
idempotency key, every fence-guarded write and the "a completed stage is never repeated" law are
v1's, unchanged.

The GRAMMAR is v2's throughout: ``AUTHOR_TURN_CONTRACT_V2`` on the wire, ``parse_versioned`` +
``validate_semantics_v2``, ``classify_formula_capability_v2(engine=None)``, ``resolve_output_v2``
over a bundle keyed by operand ``logical_ref``, ``derive_disposition_v2``, and
``freeze_current_configuration_v2`` as the frozen configuration.

**The terminal artifact is the PAIR** (``candidate_proposal`` + ``candidate_output``), per A3's
plan defect 2: BR-6 never minted a ``TypedFormulaV2``, so there is nothing to fuse and
``derive_disposition_v2`` enforces the coherence instead.

TWO PLACES THIS DIFFERS FROM ``authoring_v2``, BOTH FOLLOWING v1's REPLAY LAWS
-----------------------------------------------------------------------------
1. **A REJECTED run's terminal event kind is ``failed``, not ``completed``.**
   ``replay_authoring`` writes ``failed`` for an invalid proposal and ``completed`` for an
   unsupported one; ``authoring_v2`` writes ``completed`` for both. The replay store's
   ``run_status`` is what a recovering worker reads, and this module follows the store it writes
   to. Asserted, not assumed.
2. **The facts reader returns ``(facts_by_ref, authority_failures)``**, not v1's
   ``(per_expr, grain)``. v2's authority attribution is a first-class return of the reader
   (``authoring_v2._read_c1_facts_v2`` and ``recipe_authoring.FrozenRecipeReadContext.
   formula_facts_v2`` both have this shape) because a grain key whose governance failed closed is
   the same missing authority as an operand's, and v2's resolver takes no grain bundle at all.

⟨LLM⟩ **THE LIVE HALF IS DEFERRED, HONESTLY**, exactly as A3's is: Anthropic billing is exhausted,
so every run driving this module in the suite is a recorded fixture. What is proven is the SEAM —
stage order, resume points, artifact coherence, configuration drift, fence discipline. What is NOT
proven is that a real model held to ``AUTHOR_INSTRUCTION_V2`` emits a usable v2 proposal.
``test_a_billing_refusal_is_technical_never_a_capability_or_grammar_verdict`` is what makes that
deferral safe rather than convenient (the ``3219a209`` precedent, restated at the replay layer).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.audited import current_formula_generation_settings
from featuregen.formula.author import AUTHOR_TURN_CONTRACT_V2, author_formula
from featuregen.formula.authoring_v2 import (
    AUTHORING_ORCHESTRATOR_VERSION_V2,
    OutputResolutionV2,
    _capability_reason,
    _critic_status,
    _expectation_status,
    _names_an_unsupported_operation_v2,
    classify_output_policy_v2,
    forbids_authored_artifact_v2,
)
from featuregen.formula.capability_v2 import classify_formula_capability_v2
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
from featuregen.formula.frozen_configuration import (
    FrozenAuthorCriticConfigurationV1,
    verify_frozen_configuration_v2,
)
from featuregen.formula.output_authority_v2 import (
    FormulaOutputPolicyV2,
    OperandFactsV2,
    resolve_output_v2,
)
from featuregen.formula.parse_v2 import parse_versioned
from featuregen.formula.replay_trace import (
    append_event,
    load_verified_checkpoint,
    open_authoring_run,
)
from featuregen.formula.result import (
    AuthoringAxes,
    AuthorityFailure,
)
from featuregen.formula.result_v2 import DISPOSITION_POLICY_VERSION_V2, derive_disposition_v2
from featuregen.formula.schema import AdditivityClass, SchemaError
from featuregen.formula.schema_v2 import (
    OPERATION_GRAMMAR_VERSION_V2,
    TypedFormulaProposalV2,
)
from featuregen.overlay.field_evidence import canonical_hash

if TYPE_CHECKING:
    # TYPE_CHECKING-only for ``authoring_v2``'s reason: a name that exists at runtime is a name
    # that can be CONSTRUCTED, bypassing every coherence guard ``derive_disposition_v2`` owns.
    from featuregen.formula.result_v2 import AuthoringResultV2

__all__ = [
    "AUTHORING_ORCHESTRATOR_VERSION_V2_REPLAY",
    "run_authoring_v2_replay",
]

#: The wiring version of THIS orchestrator (stage order, resume points, axis mapping). Its own
#: number beside ``authoring_v2``'s: the two modules can be re-wired independently, and a frozen
#: run's manifest must name the one that decided it.
AUTHORING_ORCHESTRATOR_VERSION_V2_REPLAY = 1

#: The facts reader's contract: one v2 proposal in, the ref-keyed bundle plus the governed reads
#: that failed CLOSED out. The worker injects ``FrozenRecipeReadContext.formula_facts_v2``.
FactsReaderV2 = Callable[
    [TypedFormulaProposalV2],
    "tuple[Mapping[str, OperandFactsV2], tuple[AuthorityFailure, ...]]",
]


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


def _terminal_payload(result) -> dict:
    return {
        "authoring_disposition": result.authoring_disposition,
        "candidate_proposal_hash": result.candidate_proposal_hash,
        "structural_status": result.structural_status,
        "capability_status": result.capability_status,
        "output_status": result.output_status,
        "expectation_status": result.expectation_status,
        "critic_status": result.critic_status,
        "technical_status": result.technical_status,
        "result": _plain(result),
    }


def _axes(
    *,
    structural_status: str = "ok",
    capability_status: str = "ok",
    output_status: str = "resolved",
    expectation_status: str = "not_provided",
    critic_status: str = "clean",
    technical_status: str = "ok",
) -> AuthoringAxes:
    """The six axes for an EARLY exit, with v1's replay defaults deliberately kept.

    ``output_status="resolved"`` reads oddly beside ``authoring_v2``'s ``"needs_authority"``, and
    it is v1's replay value for every early-exit arm. It changes no disposition — technical,
    invalid and unsupported all outrank the output axis in the §F precedence — and copying the
    orchestration laws means copying this one rather than quietly improving it. The result carries
    no output policy either way, and ``derive_disposition_v2`` enforces that."""
    return AuthoringAxes(
        structural_status=cast(Any, structural_status),
        capability_status=cast(Any, capability_status),
        output_status=cast(Any, output_status),
        expectation_status=cast(Any, expectation_status),
        critic_status=cast(Any, critic_status),
        technical_status=cast(Any, technical_status),
    )


def _restore_terminal_result(checkpoint, run_id: str) -> AuthoringResultV2:
    """Rebuild a terminal v2 result from its own trace, through the SAME fold that wrote it.

    Nothing is trusted verbatim: the axes are re-folded by ``derive_disposition_v2`` (so its
    coherence guards run again on replay), the proposal is re-parsed from the raw wire dict the
    ``AUTHOR_PROPOSAL_PARSED`` stage recorded, the policy is rebuilt from the
    ``OUTPUT_POLICY_RESOLVED`` stage's own material, and the resulting ``candidate_proposal_hash``
    must equal the one the terminal recorded — v1's identity check, over v2's artifact.
    """
    terminal = checkpoint.terminal_result
    if not isinstance(terminal, dict) or not isinstance(terminal.get("result"), dict):
        raise RecoveryRequiresReconciliation(
            "terminal v2 authoring result is not replayable")
    material = terminal["result"]
    try:
        axes = _axes(
            structural_status=material["structural_status"],
            capability_status=material["capability_status"],
            output_status=material["output_status"],
            expectation_status=material["expectation_status"],
            critic_status=material["critic_status"],
            technical_status=material["technical_status"],
        )
        proposal = None
        if isinstance(material.get("candidate_proposal"), dict):
            source = (checkpoint.raw_proposal
                      if isinstance(checkpoint.raw_proposal, dict)
                      else material["candidate_proposal"])
            parsed = parse_versioned(source)
            if not isinstance(parsed, TypedFormulaProposalV2):
                raise RecoveryRequiresReconciliation(
                    "terminal v2 result carries a proposal of another generation")
            proposal = parsed
        output = None
        if isinstance(material.get("candidate_output"), dict):
            output_material = checkpoint.output_policy_result
            if output_material is None:
                raise RecoveryRequiresReconciliation(
                    "resolved terminal is missing its output policy stage")
            output = FormulaOutputPolicyV2(
                output_type=str(output_material["output_type"]),
                unit=str(output_material["unit"]),
                currency=str(output_material["currency"]),
                output_additivity=AdditivityClass(output_material["output_additivity"]),
                external_type_required=bool(output_material["external_type_required"]),
            )
        result = derive_disposition_v2(
            axes,
            authoring_run_id=run_id,
            candidate_proposal=proposal,
            candidate_output=output,
            output_requirements=tuple(
                str(item) for item in material.get("output_requirements", ())),
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
    except (KeyError, TypeError, ValueError, SchemaError) as exc:
        raise RecoveryRequiresReconciliation(
            "terminal v2 authoring result has invalid typed material") from exc
    if result.candidate_proposal_hash != terminal.get("candidate_proposal_hash"):
        raise RecoveryRequiresReconciliation(
            "terminal v2 proposal content hash changed during replay")
    return result


def _terminal(conn, run_id: str, seq: int, kind: str, result, *,
              lease_fence: LeaseFence | None, extra: dict | None = None) -> None:
    append_event(
        conn,
        run_id,
        kind,
        seq=seq,
        idempotency_key=f"{run_id}:terminal",
        payload={**_terminal_payload(result), **(extra or {})},
        stage="TERMINAL",
        lease_fence=lease_fence,
    )


def _technical_failure(conn, run_id: str, seq: int, reason: str, *,
                       lease_fence: LeaseFence | None = None) -> AuthoringResultV2:
    """A TECHNICAL terminal carries NO artifact and NO verdict about the formula.

    Every provider-side failure lands here — including a billing refusal, which the audited seam
    surfaces as ``output=None`` from ``author_formula``. A payment problem must never become a
    durable statement about the v2 grammar (D-10, the ``3219a209`` precedent)."""
    result = derive_disposition_v2(
        _axes(technical_status="technical_failure"), authoring_run_id=run_id)
    _terminal(conn, run_id, seq, "failed", result,
              lease_fence=lease_fence, extra={"reason": reason})
    return result


def run_authoring_v2_replay(
    conn,
    intent,
    author_client,
    critic_client,
    *,
    roles=(),
    actor: IdentityEnvelope | None,
    max_turns: int = 8,
    frozen_configuration: FrozenAuthorCriticConfigurationV1 | None = None,
    proposal_validator: Callable[[TypedFormulaProposalV2], tuple[str, ...]] | None = None,
    tool_runner: Callable[..., dict] | None = None,
    authoring_run_id: str | None = None,
    facts_reader: FactsReaderV2 | None = None,
    critic_metadata_loader: Callable[[str], dict] | None = None,
    progress_callback: Callable[[], None] | None = None,
    lease_fence: LeaseFence | None = None,
) -> AuthoringResultV2:
    """Author, parse, govern output semantics, independently critique, and fold ONE v2 result —
    resumably, under a lease, against a frozen configuration.

    Same parameters as ``replay_authoring.run_authoring``, because the live worker's call site is
    the same call site; the types they carry are v2's.
    """
    versions: dict[str, Any] = {
        "orchestrator": AUTHORING_ORCHESTRATOR_VERSION_V2_REPLAY,
        "formula_schema": 2,
        "operation_grammar": OPERATION_GRAMMAR_VERSION_V2,
        "critic": CRITIC_POLICY_VERSION,
        "disposition": DISPOSITION_POLICY_VERSION_V2,
        # The non-replay v2 orchestrator's own wiring version rides along: a run must name which
        # v2 stage-mapping decided it, and the two modules move independently.
        "authoring_v2": AUTHORING_ORCHESTRATOR_VERSION_V2,
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
        conn, run_id, intent_hash=intent_hash, versions=versions)
    if checkpoint.terminal_result is not None:
        return _restore_terminal_result(checkpoint, run_id)
    if frozen_configuration is not None:
        try:
            verify_frozen_configuration_v2(
                frozen_configuration,
                generation_settings=current_formula_generation_settings(),
            )
        except FormulaControlFlow:
            raise
        except Exception as exc:
            return _technical_failure(
                conn, run_id, 0, f"configuration:{type(exc).__name__}",
                lease_fence=lease_fence)
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
                turn_contract=AUTHOR_TURN_CONTRACT_V2,
                **({} if tool_runner is None else {"tool_runner": tool_runner}),
            )
        except FormulaControlFlow:
            raise
        except Exception as exc:
            return _technical_failure(
                conn, run_id, seq, f"author:{type(exc).__name__}", lease_fence=lease_fence)
    else:
        raw = checkpoint.raw_proposal
    if raw is None:
        # (None, turns): max_turns exhaustion, token budget, an egress block, or a provider
        # failure — a billing refusal is exactly this, and it is TECHNICAL, never a verdict.
        result = derive_disposition_v2(
            _axes(technical_status="technical_failure"), authoring_run_id=run_id)
        _terminal(conn, run_id, seq, "failed", result, lease_fence=lease_fence)
        return result

    material = checkpoint.raw_proposal or raw
    # UNSUPPORTED != INVALID, read from the RAW dict before parsing: the shape gate would report
    # an out-of-vocabulary aggregate as an enum violation, indistinguishable from real
    # malformation, and "invalid" for a request the grammar simply does not cover yet is the
    # conflation the fold forbids. ``turns_v2`` relaxes both fields on the wire so it reaches here.
    if _names_an_unsupported_operation_v2(material):
        result = derive_disposition_v2(
            _axes(structural_status="unsupported_operation"), authoring_run_id=run_id)
        _terminal(conn, run_id, seq, "completed", result, lease_fence=lease_fence)
        return result
    try:
        parsed = parse_versioned(material)
        if not isinstance(parsed, TypedFormulaProposalV2):
            # A run driven under the v2 turn contract that comes back declaring version 1 is the
            # WRONG CONTRACT, not a missing capability — the v1 generation is fully supported
            # elsewhere, so calling it "unsupported" would report a gap that does not exist.
            raise SchemaError("a v2 run received a proposal of another generation")
        proposal = parsed
    except SchemaError as exc:
        result = derive_disposition_v2(
            _axes(structural_status="invalid_formula"), authoring_run_id=run_id)
        _terminal(conn, run_id, seq, "failed", result,
                  lease_fence=lease_fence, extra={"reason": str(exc)})
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
            result = derive_disposition_v2(
                _axes(structural_status="invalid_formula"), authoring_run_id=run_id)
            _terminal(conn, run_id, seq, "failed", result, lease_fence=lease_fence, extra={
                "reason": "recipe_expectation_not_preserved",
                "violations": list(violations),
            })
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

    capability = classify_formula_capability_v2(proposal, engine=None)
    if capability != "ok":
        result = derive_disposition_v2(
            _axes(capability_status="unsupported_capability"),
            authoring_run_id=run_id,
            capability_reason=_capability_reason(proposal),
        )
        _terminal(conn, run_id, seq, "completed", result, lease_fence=lease_fence)
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
                conn, run_id, seq, f"critic:{type(exc).__name__}", lease_fence=lease_fence)
    else:
        restored: list[CriticFinding] = []
        for item in checkpoint.critic_result.get("findings", ()):
            severity = item["severity"]
            if severity not in ("blocking", "advisory"):
                raise FormulaControlFlow("replayed critic severity is invalid")
            restored.append(CriticFinding(
                code=CriticFindingCode(item["code"]),
                severity=severity,
                operand=item.get("operand"),
                detail=item.get("detail"),
            ))
        review = CriticReview(
            findings=tuple(restored),
            findings_hash=str(checkpoint.critic_result["findings_hash"]),
            technical_failure=bool(checkpoint.critic_result["technical_failure"]),
            llm_call_ref=None,
            provider_calls=0,
            usage={},
        )
    findings = review.findings
    findings_hash = review.findings_hash
    if checkpoint.critic_result is None:
        append_event(
            conn,
            run_id,
            "critic_result",
            seq=seq,
            idempotency_key=f"{run_id}:critic",
            llm_call_ref=review.llm_call_ref,
            payload={
                "technical_failure": review.technical_failure,
                "findings_hash": findings_hash,
                "provider_calls": review.provider_calls,
                "usage": review.usage,
                # CODES + targets only: the model-authored ``detail`` is free text and never
                # enters the trace; the hash covers it for tamper-evidence.
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
    if review.technical_failure:
        result = derive_disposition_v2(
            _axes(technical_status="technical_failure"),
            authoring_run_id=run_id,
            critic_findings_hash=findings_hash,
        )
        _terminal(conn, run_id, seq, "failed", result, lease_fence=lease_fence)
        return result

    try:
        if progress_callback is not None:
            progress_callback()
        facts_by_ref, authority_failures = (
            facts_reader(proposal) if facts_reader is not None else ({}, ()))
        resolved: object = resolve_output_v2(proposal, facts_by_ref)
    except FormulaControlFlow:
        raise
    except Exception as exc:
        return _technical_failure(
            conn, run_id, seq, f"output_authority:{type(exc).__name__}", lease_fence=lease_fence)
    output_payload = _plain(resolved)
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
        raise RecoveryRequiresReconciliation("replayed v2 output policy changed")

    if authority_failures:
        # A governed read that failed CLOSED means no authority was established for that operand
        # at all. Resolving an output over the empty facts it produced would answer with a guess,
        # so the axis is needs_authority and the resolver is not consulted for a verdict.
        resolution = OutputResolutionV2(
            "needs_authority", None, reason=authority_failures[0].reason)
    else:
        resolution = classify_output_policy_v2(resolved)
    critic_status = _critic_status(list(findings))
    axes = _axes(
        output_status=resolution.status,
        expectation_status=_expectation_status(proposal, resolution.policy),
        critic_status=critic_status,
    )
    # Artifact coherence is decided HERE and enforced THERE, exactly as ``authoring_v2._finish``
    # does it: the v2 artifact is the PAIR, and both halves are offered only when the output
    # actually resolved AND the folded disposition permits an artifact at all.
    carries_artifact = not forbids_authored_artifact_v2(axes)
    result = derive_disposition_v2(
        axes,
        authoring_run_id=run_id,
        candidate_proposal=proposal if carries_artifact else None,
        candidate_output=(
            resolution.policy if carries_artifact and axes.output_status == "resolved" else None),
        output_requirements=resolution.requirements,
        authority_failures=authority_failures,
        critic_findings_hash=findings_hash,
    )
    _terminal(conn, run_id, seq, "completed", result, lease_fence=lease_fence)
    return result

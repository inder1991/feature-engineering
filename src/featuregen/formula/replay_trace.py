"""Write-once replay trace used for fenced formula recovery and audit."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Literal

import psycopg

from featuregen.config import get_settings
from featuregen.formula.control import (
    FormulaControlFlow,
    LeaseFence,
    LeaseFenceLost,
    RecoveryRequiresReconciliation,
)
from featuregen.idgen import mint_id

logger = logging.getLogger(__name__)

_KINDS = frozenset({
    "author_turn",
    "critic_result",
    "validation_result",
    "completed",
    "failed",
})
_TERMINAL_KINDS = frozenset({"completed", "failed"})


class FormulaTraceUnavailable(RuntimeError):
    """The independently committed formula audit store could not be reached."""


def _durable_write(conn, operation) -> None:
    """Commit one trace mutation independently when a production DSN is configured.

    A configured deployment must never degrade formula audit writes into the caller transaction:
    provider egress could then survive while its manifest/turn is rolled back. DSN-less unit tests
    and local harnesses retain the existing caller-connection behavior.
    """
    dsn = get_settings().dsn
    if not dsn:
        operation(conn)
        return
    try:
        with psycopg.connect(dsn) as trace_conn:
            operation(trace_conn)
    except FormulaControlFlow:
        raise
    except Exception as exc:
        logger.exception("durable formula-authoring trace write failed")
        raise FormulaTraceUnavailable("formula authoring audit is unavailable") from exc


def _hash(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fence_values(fence: LeaseFence | None) -> tuple[int | None, str | None, int | None]:
    if fence is None:
        return None, None, None
    return fence.queue_id, fence.lease_owner, fence.lease_fence


def _insert_run(
    conn,
    run_id: str,
    *,
    intent_hash: str,
    versions: dict,
    actor,
    fence: LeaseFence | None,
) -> None:
    encoded_versions = json.dumps(versions)
    encoded_actor = json.dumps(actor) if actor is not None else None
    queue_id, lease_owner, lease_fence = _fence_values(fence)
    if fence is None:
        inserted = conn.execute(
            "INSERT INTO formula_authoring_run "
            "(authoring_run_id, intent_hash, versions, actor, queue_id, lease_owner, lease_fence) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb, NULL, NULL, NULL) "
            "ON CONFLICT (authoring_run_id) DO NOTHING RETURNING authoring_run_id",
            (run_id, intent_hash, encoded_versions, encoded_actor),
        ).fetchone()
    else:
        inserted = conn.execute(
            "INSERT INTO formula_authoring_run "
            "(authoring_run_id, intent_hash, versions, actor, queue_id, lease_owner, lease_fence) "
            "SELECT %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s "
            "WHERE EXISTS (SELECT 1 FROM queue WHERE id=%s AND status='leased' "
            "AND lease_owner=%s AND lease_fence=%s AND lease_expires_at > now()) "
            "ON CONFLICT (authoring_run_id) DO NOTHING RETURNING authoring_run_id",
            (
                run_id,
                intent_hash,
                encoded_versions,
                encoded_actor,
                queue_id,
                lease_owner,
                lease_fence,
                queue_id,
                lease_owner,
                lease_fence,
            ),
        ).fetchone()
    if inserted is not None:
        return
    if fence is not None and conn.execute(
        "SELECT 1 FROM queue WHERE id=%s AND status='leased' AND lease_owner=%s "
        "AND lease_fence=%s AND lease_expires_at > now()",
        (queue_id, lease_owner, lease_fence),
    ).fetchone() is None:
        raise LeaseFenceLost("formula authoring run lease fence is no longer live")
    existing = conn.execute(
        "SELECT intent_hash,versions,actor,queue_id,lease_owner,lease_fence "
        "FROM formula_authoring_run "
        "WHERE authoring_run_id=%s",
        (run_id,),
    ).fetchone()
    if existing is None or existing[:3] != (intent_hash, versions, actor):
        raise ValueError(f"formula authoring run {run_id!r} conflicts with stored identity")


def open_authoring_run(
    conn,
    *,
    intent_hash: str,
    versions: dict,
    actor,
    authoring_run_id: str | None = None,
    lease_fence: LeaseFence | None = None,
) -> str:
    run_id = authoring_run_id or mint_id("far")
    _durable_write(
        conn,
        lambda write_conn: _insert_run(
            write_conn,
            run_id,
            intent_hash=intent_hash,
            versions=versions,
            actor=actor,
            fence=lease_fence,
        ),
    )
    return run_id


def _insert_event(
    conn,
    run_id: str,
    kind: str,
    *,
    seq: int,
    idempotency_key: str,
    payload: dict,
    llm_call_ref: str | None,
    stage: str,
    logical_turn_index: int | None,
    canonical_input_hash: str | None,
    provider_contract_hash: str | None,
    tool_context_hash: str | None,
    canonical_output_hash: str | None,
    fence: LeaseFence | None,
) -> None:
    if fence is not None and llm_call_ref is not None:
        identities = conn.execute(
            "SELECT DISTINCT d.canonical_turn_input_hash,d.provider_contract_hash "
            "FROM llm_dispatch d JOIN llm_call_dispatch l USING (dispatch_ref) "
            "WHERE l.llm_call_ref=%s AND d.authoring_run_id=%s",
            (llm_call_ref, run_id),
        ).fetchall()
        if (
            len(identities) != 1
            or identities[0][0] is None
            or identities[0][1] is None
        ):
            raise RecoveryRequiresReconciliation(
                "formula provider call identity is missing or ambiguous")
        stored_input_hash, stored_contract_hash = identities[0]
        if (
            canonical_input_hash is not None
            and canonical_input_hash != stored_input_hash
        ):
            raise RecoveryRequiresReconciliation(
                "formula provider canonical input identity changed")
        if (
            provider_contract_hash is not None
            and provider_contract_hash != stored_contract_hash
        ):
            raise RecoveryRequiresReconciliation(
                "formula provider contract identity changed")
        canonical_input_hash = stored_input_hash
        provider_contract_hash = stored_contract_hash
    encoded = json.dumps(payload)
    payload_hash = _hash(payload)
    queue_id, lease_owner, lease_fence = _fence_values(fence)
    values = (
        run_id,
        seq,
        kind,
        idempotency_key,
        llm_call_ref,
        encoded,
        stage,
        logical_turn_index,
        canonical_input_hash,
        provider_contract_hash,
        tool_context_hash,
        canonical_output_hash,
        payload_hash,
        queue_id,
        lease_owner,
        lease_fence,
    )
    columns = (
        "(authoring_run_id, seq, kind, idempotency_key, llm_call_ref, payload, stage, "
        "logical_turn_index, canonical_input_hash, provider_contract_hash, tool_context_hash, "
        "canonical_output_hash, payload_hash, queue_id, lease_owner, lease_fence) "
    )
    if fence is None:
        inserted = conn.execute(
            "INSERT INTO formula_authoring_trace_event "
            + columns
            + "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING authoring_run_id",
            values,
        ).fetchone()
    else:
        inserted = conn.execute(
            "INSERT INTO formula_authoring_trace_event "
            + columns
            + "SELECT %s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
            "WHERE EXISTS (SELECT 1 FROM queue WHERE id=%s AND status='leased' "
            "AND lease_owner=%s AND lease_fence=%s AND lease_expires_at > now()) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING authoring_run_id",
            (*values, queue_id, lease_owner, lease_fence),
        ).fetchone()
    if inserted is not None:
        return
    if fence is not None and conn.execute(
        "SELECT 1 FROM queue WHERE id=%s AND status='leased' AND lease_owner=%s "
        "AND lease_fence=%s AND lease_expires_at > now()",
        (queue_id, lease_owner, lease_fence),
    ).fetchone() is None:
        raise LeaseFenceLost("formula trace event lease fence is no longer live")
    existing = conn.execute(
        "SELECT authoring_run_id,seq,kind,llm_call_ref,payload,stage,logical_turn_index,"
        "canonical_input_hash,provider_contract_hash,tool_context_hash,canonical_output_hash,"
        "payload_hash,queue_id,lease_owner,lease_fence "
        "FROM formula_authoring_trace_event WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    expected = (
        run_id,
        seq,
        kind,
        llm_call_ref,
        payload,
        stage,
        logical_turn_index,
        canonical_input_hash,
        provider_contract_hash,
        tool_context_hash,
        canonical_output_hash,
        payload_hash,
        queue_id,
        lease_owner,
        lease_fence,
    )
    if existing != expected:
        raise ValueError(
            f"formula trace idempotency conflict for {idempotency_key!r}")


def append_event(
    conn,
    run_id: str,
    kind: str,
    *,
    seq: int,
    idempotency_key: str,
    payload: dict,
    llm_call_ref: str | None = None,
    stage: str | None = None,
    logical_turn_index: int | None = None,
    canonical_input_hash: str | None = None,
    provider_contract_hash: str | None = None,
    tool_context_hash: str | None = None,
    canonical_output_hash: str | None = None,
    lease_fence: LeaseFence | None = None,
) -> None:
    if kind not in _KINDS:
        raise ValueError(f"unknown formula authoring trace kind {kind!r}")
    _durable_write(
        conn,
        lambda write_conn: _insert_event(
            write_conn,
            run_id,
            kind,
            seq=seq,
            idempotency_key=idempotency_key,
            payload=payload,
            llm_call_ref=llm_call_ref,
            stage=stage or kind.upper(),
            logical_turn_index=logical_turn_index,
            canonical_input_hash=canonical_input_hash,
            provider_contract_hash=provider_contract_hash,
            tool_context_hash=tool_context_hash,
            canonical_output_hash=canonical_output_hash,
            fence=lease_fence,
        ),
    )


def run_status(conn, run_id: str) -> Literal["incomplete", "completed", "failed"]:
    row = conn.execute(
        "SELECT kind FROM formula_authoring_trace_event "
        "WHERE authoring_run_id = %s AND kind IN ('completed', 'failed')",
        (run_id,),
    ).fetchone()
    if row is None:
        return "incomplete"
    return "completed" if row[0] == "completed" else "failed"


@dataclass(frozen=True, slots=True)
class VerifiedCheckpoint:
    authoring_run_id: str
    next_stage: str
    next_seq: int
    events: tuple[dict, ...]
    author_turns: tuple[dict, ...]
    raw_proposal: dict | None
    critic_result: dict | None
    output_policy_result: dict | None
    terminal_result: dict | None
    tokens_spent: int


def _verify_stage_transition(
    prior_stage: str | None,
    stage: str,
    *,
    author_index: int,
) -> int:
    if stage.startswith("AUTHOR_TURN_"):
        if prior_stage is not None and not prior_stage.startswith("AUTHOR_TURN_"):
            raise RecoveryRequiresReconciliation("author turn appears after a later stage")
        try:
            index = int(stage.removeprefix("AUTHOR_TURN_"))
        except ValueError as exc:
            raise RecoveryRequiresReconciliation("invalid author turn stage") from exc
        if index != author_index:
            raise RecoveryRequiresReconciliation("author turn sequence is not contiguous")
        return author_index + 1
    allowed_prior = {
        "AUTHOR_PROPOSAL_PARSED": lambda value: (
            value is not None and value.startswith("AUTHOR_TURN_")),
        "EXPECTATION_VALIDATED": lambda value: value == "AUTHOR_PROPOSAL_PARSED",
        "CRITIC_COMPLETED": lambda value: value == "EXPECTATION_VALIDATED",
        "OUTPUT_POLICY_RESOLVED": lambda value: value == "CRITIC_COMPLETED",
        "TERMINAL": lambda _value: True,
    }
    predicate = allowed_prior.get(stage)
    if predicate is None or not predicate(prior_stage):
        raise RecoveryRequiresReconciliation(
            f"invalid formula stage transition {prior_stage!r} -> {stage!r}")
    return author_index


def load_verified_checkpoint(
    conn,
    authoring_run_id: str,
    *,
    intent_hash: str,
    versions: dict,
) -> VerifiedCheckpoint:
    """Verify immutable trace/dispatch identity and reconstruct the next replay stage."""
    run = conn.execute(
        "SELECT intent_hash,versions FROM formula_authoring_run WHERE authoring_run_id=%s",
        (authoring_run_id,),
    ).fetchone()
    if run is None:
        return VerifiedCheckpoint(
            authoring_run_id,
            "AUTHOR_TURN_0",
            0,
            (),
            (),
            None,
            None,
            None,
            None,
            0,
        )
    if run != (intent_hash, versions):
        raise RecoveryRequiresReconciliation("formula run manifest identity changed")
    columns = (
        "seq,kind,idempotency_key,llm_call_ref,payload,stage,logical_turn_index,"
        "canonical_input_hash,provider_contract_hash,tool_context_hash,"
        "canonical_output_hash,payload_hash"
    )
    rows = conn.execute(
        f"SELECT {columns} FROM formula_authoring_trace_event "
        "WHERE authoring_run_id=%s ORDER BY seq",
        (authoring_run_id,),
    ).fetchall()
    events: list[dict] = []
    turns: list[dict] = []
    raw_proposal = None
    critic_result = None
    output_result = None
    terminal_result = None
    tokens_spent = 0
    prior_stage: str | None = None
    author_index = 0
    for expected_seq, row in enumerate(rows):
        (
            seq,
            kind,
            idempotency_key,
            llm_call_ref,
            payload,
            stage,
            logical_turn_index,
            canonical_input_hash,
            provider_contract_hash,
            tool_context_hash,
            canonical_output_hash,
            payload_hash,
        ) = row
        if seq != expected_seq or not isinstance(payload, dict):
            raise RecoveryRequiresReconciliation("formula trace sequence is not contiguous")
        if not isinstance(stage, str) or _hash(payload) != payload_hash:
            raise RecoveryRequiresReconciliation("formula trace payload integrity failed")
        author_index = _verify_stage_transition(
            prior_stage, stage, author_index=author_index)
        if kind in _TERMINAL_KINDS and expected_seq != len(rows) - 1:
            raise RecoveryRequiresReconciliation("formula terminal event is not final")
        if llm_call_ref is not None:
            reconciled = conn.execute(
                "SELECT count(*) > 0,"
                "bool_and(d.canonical_turn_input_hash=%s),"
                "bool_and(d.provider_contract_hash=%s),"
                "bool_and(EXISTS (SELECT 1 FROM llm_dispatch_outcome o "
                "                    WHERE o.dispatch_ref=d.dispatch_ref)) "
                "FROM llm_dispatch d JOIN llm_call_dispatch l USING (dispatch_ref) "
                "JOIN llm_call c ON c.llm_call_ref=l.llm_call_ref "
                "WHERE l.llm_call_ref=%s AND d.authoring_run_id=%s",
                (
                    canonical_input_hash,
                    provider_contract_hash,
                    llm_call_ref,
                    authoring_run_id,
                ),
            ).fetchone()
            if reconciled is None or not all(reconciled):
                raise RecoveryRequiresReconciliation(
                    "formula provider dispatch cannot be reconciled")
        event = {
            "seq": seq,
            "kind": kind,
            "idempotency_key": idempotency_key,
            "llm_call_ref": llm_call_ref,
            "payload": payload,
            "stage": stage,
            "logical_turn_index": logical_turn_index,
            "canonical_input_hash": canonical_input_hash,
            "provider_contract_hash": provider_contract_hash,
            "tool_context_hash": tool_context_hash,
            "canonical_output_hash": canonical_output_hash,
        }
        events.append(event)
        if stage.startswith("AUTHOR_TURN_"):
            turns.append(payload)
            usage = payload.get("usage")
            if isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        tokens_spent += value
        elif stage == "AUTHOR_PROPOSAL_PARSED":
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                raise RecoveryRequiresReconciliation("parsed proposal is missing")
            raw_proposal = proposal
        elif stage == "CRITIC_COMPLETED":
            critic_result = payload
        elif stage == "OUTPUT_POLICY_RESOLVED":
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RecoveryRequiresReconciliation("output-policy result is missing")
            output_result = result
        elif stage == "TERMINAL":
            terminal_result = payload
        prior_stage = stage
    stray_dispatch = conn.execute(
        "SELECT 1 FROM llm_dispatch d WHERE d.authoring_run_id=%s AND ("
        "NOT EXISTS (SELECT 1 FROM llm_dispatch_outcome o "
        "            WHERE o.dispatch_ref=d.dispatch_ref) OR "
        "NOT EXISTS (SELECT 1 FROM llm_call_dispatch l "
        "            WHERE l.dispatch_ref=d.dispatch_ref) OR "
        "NOT EXISTS (SELECT 1 FROM formula_authoring_trace_event e "
        "            JOIN llm_call_dispatch l ON l.llm_call_ref=e.llm_call_ref "
        "            WHERE e.authoring_run_id=%s AND l.dispatch_ref=d.dispatch_ref)"
        ") LIMIT 1",
        (authoring_run_id, authoring_run_id),
    ).fetchone()
    if stray_dispatch is not None:
        raise RecoveryRequiresReconciliation(
            "formula run contains an ambiguous provider dispatch")
    next_stage = (
        "TERMINAL"
        if terminal_result is not None
        else "OUTPUT_POLICY_RESOLVED"
        if output_result is not None
        else "CRITIC_COMPLETED"
        if critic_result is not None
        else "EXPECTATION_VALIDATED"
        if raw_proposal is not None
        else f"AUTHOR_TURN_{author_index}"
    )
    return VerifiedCheckpoint(
        authoring_run_id,
        next_stage,
        len(rows),
        tuple(events),
        tuple(turns),
        raw_proposal,
        critic_result,
        output_result,
        terminal_result,
        tokens_spent,
    )

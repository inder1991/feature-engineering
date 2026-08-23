"""Pre-dispatch LLM egress audit writer (Delivery C5 Task 2).

BEFORE each physical provider request the caller records an immutable ``llm_dispatch`` header +
``llm_dispatch_subject`` attribution rows (migration 1005) via ``record_dispatch``, which commits
on an OWN independent connection resolved from ``get_settings().dsn`` — the
``enrich_llm._record_llm_call_durable`` / ``ingestion_run.open_run`` connection discipline — so a
bank regulator can prove the egress was authorized + attributed even if the surrounding upload
transaction later rolls back.

FAIL-CLOSED, deliberately unlike ``_record_llm_call_durable``'s best-effort degrade: that record
is written AFTER egress (transactional evidence beats none), but a PRE-dispatch record that cannot
be durably committed means the provider request MUST NOT happen — ``AuditUnavailable`` is the
caller's no-dispatch signal (wired at the dispatch seam in C5-T3). There is no fallback-connection
path here on purpose: a fallback write would share the upload transaction's fate, which is exactly
the evidence loss this writer exists to prevent.

SENSITIVE-data rule: ``redacted_input`` MUST already be the egress-approved LLM-safe inputs (the
``LLMRequest.inputs`` produced by the redaction seams — hash them with ``compute_input_hash``).
This writer stores them verbatim and NEVER re-scans; ``llm_dispatch`` inherits ``llm_call``'s
SENSITIVE / read-controlled classification precisely because it holds that redacted request —
never raw upload text.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from featuregen.config import get_settings
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.formula.control import LeaseFence, LeaseFenceLost
from featuregen.idgen import mint_id
from featuregen.intake.llm import (
    PROVIDER_AUTH_ERROR,
    PROVIDER_TRANSIENT,
    LLMClient,
    LLMRequest,
    LLMResult,
    compute_input_hash,
)
from featuregen.overlay.upload.llm_spend import SpendExhausted

logger = logging.getLogger(__name__)


class AuditUnavailable(Exception):
    """The pre-dispatch audit could not be durably committed — the caller must NOT dispatch."""


class AuditIntegrityError(AuditUnavailable):
    """An idempotency key was reused for materially different provider egress."""


def compute_physical_request_hash(request: LLMRequest) -> str:
    """Hash every provider-facing request field, including transient repair input."""
    material = {
        "task": request.task,
        "prompt_id": request.prompt_id,
        "prompt_version": request.prompt_version,
        "inputs": dict(request.inputs),
        "output_schema_id": request.output_schema_id,
        "output_schema_version": request.output_schema_version,
        "output_schema": request.output_schema,
        "generation_settings": request.generation_settings,
        "cacheable_metadata_keys": list(request.cacheable_metadata_keys),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _physical_generation_settings(
    generation_settings: Mapping[str, Any],
    repair_attempts: object,
    attempt_no: object,
) -> dict:
    """The generation_settings the Nth PHYSICAL attempt actually carried.

    The logical ``llm_call`` row stores the STARTING settings — deliberately, because they are the
    idempotency key (``find_llm_call`` compares them, so an escalated copy would fork dedup
    identity). A truncation retry is nonetheless dispatched at a RAISED ``max_tokens``, so
    rebuilding an attempt from the logical row alone describes a request that was never sent.

    The driver already audits each escalation on that same row: ``repair_attempts`` holds ONE entry
    per physical re-call, and a truncation retry's entry carries the ``max_tokens`` it was raised
    to. Replaying that ledger up to ``attempt_no`` reconstructs the real physical request without a
    schema change and WITHOUT weakening the hash — ``generation_settings`` stays in the material in
    full, so a ceiling that disagrees with the recorded ledger still fails reconciliation. An
    escalation that was never audited cannot pass by virtue of being an escalation.

    Physical attempt N is preceded by exactly N-1 ledger entries (the driver appends one, then
    calls), so the prefix ``[:N-1]`` is the escalation history that attempt saw. Repair entries
    carry no ``max_tokens`` and are skipped while still consuming their position."""
    settings = dict(generation_settings)
    # An unusable attempt_no rebuilds the BASELINE rather than inventing a ceiling: the hash check
    # then fails honestly on a real mismatch instead of being talked into a value nobody recorded.
    if not isinstance(attempt_no, int) or not isinstance(repair_attempts, list):
        return settings
    preceding = attempt_no - 1
    if preceding <= 0:
        return settings
    for entry in repair_attempts[:preceding]:
        if isinstance(entry, dict) and entry.get("max_tokens"):
            settings["max_tokens"] = entry["max_tokens"]
    return settings


def _content_hash(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def record_dispatch(*, logical_call_ref: str, attempt_no: int, ingestion_run_id: str | None,
                    stage: str, task: str, redacted_input: dict, input_hash: str,
                    subjects: list[dict], redaction_version: str | None = None,
                    provider: str | None = None, model: str | None = None,
                    prompt_version: int | None = None,
                    schema_version: int | None = None,
                    physical_request_hash: str | None = None,
                    authoring_run_id: str | None = None,
                    spend_authorization_id: str | None = None,
                    spend_call_tokens: int = 0,
                    spend_call_cost: str = "0",
                    call_role: str | None = None,
                    turn_index: int | None = None,
                    canonical_turn_input_hash: str | None = None,
                    provider_contract_hash: str | None = None,
                    prompt_content_hash: str | None = None,
                    schema_content_hash: str | None = None,
                    lease_fence: LeaseFence | None = None) -> str:
    """Mint a dispatch_ref; on an OWN connection (``get_settings().dsn``) INSERT one immutable
    ``llm_dispatch`` header + one ``llm_dispatch_subject`` row per subject, and COMMIT
    independently (survives an upload rollback). Returns the dispatch_ref.

    Each subject is ``{catalog_source, object_ref, logical_ref, field_names}`` — WHICH catalog
    objects/fields this physical request is about; ``ingestion_run_id`` is None for dispatches
    outside an ingestion run (recorded honestly as NULL).

    Idempotent replay: the migration's UNIQUE(logical_call_ref, attempt_no) is the retry/replay
    key — when this attempt is already audited, the EXISTING dispatch_ref is returned (via
    ``ON CONFLICT DO NOTHING`` + read-back) rather than raising; the write-once rows are never
    touched, and no duplicate subjects are appended.

    Raises ``AuditUnavailable`` when the write cannot be durably committed (no DSN configured,
    connect/commit failure) — the caller must then NOT dispatch to the provider (C5-T3)."""
    dsn = get_settings().dsn
    if not dsn:
        raise AuditUnavailable(
            "pre-dispatch audit requires a configured FEATUREGEN_DSN — refusing to authorize "
            f"egress for logical_call_ref={logical_call_ref!r} attempt {attempt_no}")
    dispatch_ref = mint_id("disp")
    try:
        with psycopg.connect(dsn) as audit_conn:   # own tx, committed on `with` exit
            queue_id = lease_fence.queue_id if lease_fence is not None else None
            lease_owner = lease_fence.lease_owner if lease_fence is not None else None
            fence_epoch = lease_fence.lease_fence if lease_fence is not None else None
            values = (
                dispatch_ref, logical_call_ref, attempt_no, ingestion_run_id, stage, task,
                input_hash, Jsonb(redacted_input), redaction_version, provider, model,
                prompt_version, schema_version, physical_request_hash, authoring_run_id,
                call_role, turn_index, canonical_turn_input_hash, provider_contract_hash,
                prompt_content_hash, schema_content_hash, queue_id, lease_owner, fence_epoch,
            )
            columns = (
                "(dispatch_ref,logical_call_ref,attempt_no,ingestion_run_id,stage,task,input_hash,"
                "redacted_input,redaction_version,provider,model,prompt_version,schema_version,"
                "physical_request_hash,authoring_run_id,call_role,turn_index,"
                "canonical_turn_input_hash,provider_contract_hash,prompt_content_hash,"
                "schema_content_hash,queue_id,lease_owner,lease_fence) "
            )
            # ▲ THE RESERVATION RIDES THE SAME TRANSACTION AS THE DISPATCH ROW, bound to its ref.
            # SpendExhausted raises out of this `with`, the transaction rolls back, and the
            # dispatch row vanishes with it — no audited egress exists for an attempt the budget
            # refused, which is exactly the fail-closed shape AuditUnavailable already has. Placed
            # BEFORE the dispatch INSERT so an exhausted budget costs zero writes.
            if spend_authorization_id is not None:
                from datetime import UTC, datetime

                from featuregen.overlay.upload.llm_spend import reserve_spend

                reserve_spend(
                    audit_conn, spend_authorization_id=spend_authorization_id,
                    calls=1, tokens=spend_call_tokens, cost=spend_call_cost,
                    now=datetime.now(UTC), dispatch_ref=dispatch_ref)
            if lease_fence is None:
                row = audit_conn.execute(
                    "INSERT INTO llm_dispatch " + columns
                    + "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (logical_call_ref, attempt_no) DO NOTHING RETURNING dispatch_ref",
                    values,
                ).fetchone()
            else:
                row = audit_conn.execute(
                    "INSERT INTO llm_dispatch " + columns
                    + "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s WHERE EXISTS (SELECT 1 FROM queue "
                    "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
                    "AND lease_expires_at > now()) "
                    "ON CONFLICT (logical_call_ref, attempt_no) DO NOTHING RETURNING dispatch_ref",
                    (*values, queue_id, lease_owner, fence_epoch),
                ).fetchone()
            if row is None:
                if lease_fence is not None and audit_conn.execute(
                    "SELECT 1 FROM queue WHERE id=%s AND status='leased' "
                    "AND lease_owner=%s AND lease_fence=%s AND lease_expires_at > now()",
                    (queue_id, lease_owner, fence_epoch),
                ).fetchone() is None:
                    raise LeaseFenceLost(
                        "formula pre-dispatch lease fence is no longer live")
                existing = audit_conn.execute(
                    "SELECT dispatch_ref,ingestion_run_id,stage,task,input_hash,redacted_input,"
                    "redaction_version,provider,model,prompt_version,schema_version,"
                    "physical_request_hash,authoring_run_id,call_role,turn_index,"
                    "canonical_turn_input_hash,provider_contract_hash,prompt_content_hash,"
                    "schema_content_hash,queue_id,lease_owner,lease_fence FROM llm_dispatch "
                    "WHERE logical_call_ref = %s AND attempt_no = %s",
                    (logical_call_ref, attempt_no)).fetchone()
                if existing is None:   # unreachable outside a torn DB — still fail closed
                    raise AuditUnavailable(
                        f"pre-dispatch audit conflict for {logical_call_ref!r} attempt "
                        f"{attempt_no} but the prior record could not be read back")
                expected = (
                    ingestion_run_id,
                    stage,
                    task,
                    input_hash,
                    redacted_input,
                    redaction_version,
                    provider,
                    model,
                    prompt_version,
                    schema_version,
                    physical_request_hash,
                    authoring_run_id,
                    call_role,
                    turn_index,
                    canonical_turn_input_hash,
                    provider_contract_hash,
                    prompt_content_hash,
                    schema_content_hash,
                    queue_id,
                    lease_owner,
                    fence_epoch,
                )
                if existing[1:] != expected:
                    raise AuditIntegrityError(
                        f"pre-dispatch identity {logical_call_ref!r} attempt {attempt_no} "
                        "was reused for materially different egress")
                return existing[0]
            for subject in subjects:
                audit_conn.execute(
                    "INSERT INTO llm_dispatch_subject (dispatch_ref, catalog_source, "
                    "object_ref, logical_ref, field_names) VALUES (%s, %s, %s, %s, %s)",
                    (dispatch_ref, subject.get("catalog_source"), subject.get("object_ref"),
                     subject.get("logical_ref"), Jsonb(subject.get("field_names") or [])))
        return dispatch_ref
    except (AuditUnavailable, LeaseFenceLost):
        raise
    except Exception as exc:  # noqa: BLE001 — ANY durability failure means: do not dispatch
        logger.exception("pre-dispatch audit write failed for logical_call_ref=%s attempt=%s",
                         logical_call_ref, attempt_no)
        raise AuditUnavailable(
            f"pre-dispatch audit could not be durably committed for {logical_call_ref!r} "
            f"attempt {attempt_no} — egress is not authorized") from exc


def link_llm_call(*, llm_call_ref: str, dispatch_refs: Sequence[str],
                  ingestion_run_id: str | None, stage: str) -> bool:
    """C5-T4: associate the logical ``llm_call`` back to the physical dispatch(es) that carried it
    (``llm_call_dispatch``) and, when it served an ingestion run, to that run
    (``ingestion_run_llm_call``). Same OWN-connection discipline as ``record_dispatch`` (fresh
    ``get_settings().dsn`` connection, committed independently); both INSERTs are ``ON CONFLICT DO
    NOTHING`` against the migration-1005 UNIQUEs, so a replay never duplicates an association.

    Returns ``ok``: ``True`` when the linkage was durably committed OR there is no durable link
    store to write to (no DSN — the tests / no-DB harness, a designed path, not a failure);
    ``False`` when the own-connection link write could not be committed. The best-effort commit
    posture is UNCHANGED — this NEVER raises. The ingestion seam (``audited_structured_call`` /
    ``audited_batch_call``) reads the bool to enforce C5 eligibility ordering: a ``False`` means the
    logical outcome audit did NOT commit, so the enrichment result is DISCARDED (not eligible for
    cache/evidence). The pre-dispatch authorization AND the immutable llm_call are already durable,
    so a ``False`` loses convenience joins + defers the enrichment, never evidence."""
    dsn = get_settings().dsn
    if not dsn:
        logger.warning("no FEATUREGEN_DSN configured — llm_call linkage for %s not durably "
                       "recorded", llm_call_ref)
        return True   # no durable link store to write to — the designed no-DB path, not a failure
    try:
        with psycopg.connect(dsn) as link_conn:   # own tx, committed on `with` exit
            for dispatch_ref in dispatch_refs:
                link_conn.execute(
                    "INSERT INTO llm_call_dispatch (llm_call_ref, dispatch_ref) "
                    "VALUES (%s, %s) ON CONFLICT (llm_call_ref, dispatch_ref) DO NOTHING",
                    (llm_call_ref, dispatch_ref))
            if ingestion_run_id is not None:
                link_conn.execute(
                    "INSERT INTO ingestion_run_llm_call (ingestion_run_id, llm_call_ref, stage) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (ingestion_run_id, llm_call_ref, stage) DO NOTHING",
                    (ingestion_run_id, llm_call_ref, stage))
        return True
    except Exception:  # noqa: BLE001 — post-egress linkage must never mask the real result
        logger.exception("llm_call linkage write failed for llm_call_ref=%s (run=%s, %d "
                         "dispatches)", llm_call_ref, ingestion_run_id, len(dispatch_refs))
        return False


def record_dispatch_outcome(*, dispatch_ref: str, outcome: str) -> None:
    """AFTER egress: append one ``llm_dispatch_outcome`` row (``response_received`` |
    ``transport_failed``) for a dispatch header, on the SAME own-connection discipline as
    ``record_dispatch`` (fresh ``get_settings().dsn`` connection, committed independently).

    Raises ``AuditUnavailable`` when the append cannot be durably committed. The CALLER owns the
    policy: unlike the pre-dispatch header (whose absence must block egress), an outcome-write
    failure happens after the provider request already went out under a durable authorization
    record — ``AuditingClient`` treats it as best-effort (logged, never masking the real
    result/exception), mirroring ``_record_llm_call_durable``'s post-egress stance."""
    dsn = get_settings().dsn
    if not dsn:
        raise AuditUnavailable(
            "dispatch-outcome audit requires a configured FEATUREGEN_DSN — no durable commit is "
            f"possible for dispatch_ref={dispatch_ref!r}")
    try:
        with psycopg.connect(dsn) as audit_conn:   # own tx, committed on `with` exit
            audit_conn.execute(
                "INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, %s)",
                (dispatch_ref, outcome))
    except Exception as exc:  # noqa: BLE001 — any durability failure is the same caller signal
        logger.exception("dispatch-outcome audit write failed for dispatch_ref=%s outcome=%s",
                         dispatch_ref, outcome)
        raise AuditUnavailable(
            f"dispatch outcome could not be durably committed for {dispatch_ref!r}") from exc


def dispatch_egress_status(conn, dispatch_ref: str) -> str:
    """The latest llm_dispatch_outcome ('response_received'|'transport_failed') for the dispatch, or
    'egress_outcome_unknown' when the llm_dispatch header exists but has NO outcome row (crash between
    record_dispatch and the outcome write) — never 'not sent': the pre-dispatch record proves egress
    was AUTHORIZED and may have occurred.

    Plain read on the PASSED connection (no own connection): the caller owns the transaction. The
    latest outcome wins (order by recorded_at then id) because a retry attempt-boundary may append
    more than one outcome row per dispatch_ref. A dispatch with no outcome row is
    'egress_outcome_unknown' — the immutable pre-dispatch header in ``llm_dispatch`` is the proof
    that egress was authorized, so the honest classification is UNKNOWN, never 'not sent'."""
    outcome = conn.execute(
        "SELECT outcome FROM llm_dispatch_outcome WHERE dispatch_ref = %s "
        "ORDER BY recorded_at DESC, id DESC LIMIT 1", (dispatch_ref,)).fetchone()
    if outcome is not None:
        return outcome[0]
    return "egress_outcome_unknown"


def formula_dispatch_reconciliation_failure(
    conn,
    authoring_run_id: str,
) -> str | None:
    """Return the first strict formula-dispatch reconciliation failure, if any."""
    dispatches = conn.execute(
        "SELECT dispatch_ref,task,input_hash,redacted_input,provider,model,prompt_version,"
        "schema_version,physical_request_hash,call_role,canonical_turn_input_hash,"
        "provider_contract_hash,prompt_content_hash,schema_content_hash,attempt_no "
        "FROM llm_dispatch WHERE authoring_run_id=%s ORDER BY dispatch_ref",
        (authoring_run_id,),
    ).fetchall()
    if not dispatches:
        return "NO_PHYSICAL_DISPATCH"
    registry = DocumentSchemaRegistry(conn)
    for dispatch in dispatches:
        (
            dispatch_ref,
            task,
            input_hash,
            redacted_input,
            provider,
            model,
            prompt_version,
            schema_version,
            physical_hash,
            call_role,
            canonical_hash,
            provider_contract_hash,
            prompt_content_hash,
            schema_content_hash,
            attempt_no,
        ) = dispatch
        links = conn.execute(
            "SELECT c.llm_call_ref,c.task,c.provider,c.model,c.prompt_id,c.prompt_version,"
            "c.output_schema_id,c.output_schema_version,c.generation_settings,c.input_hash,"
            "c.repair_attempts "
            "FROM llm_call_dispatch l JOIN llm_call c USING (llm_call_ref) "
            "WHERE l.dispatch_ref=%s",
            (dispatch_ref,),
        ).fetchall()
        if len(links) != 1:
            return "LOGICAL_CALL_LINK_CARDINALITY_INVALID"
        (
            llm_call_ref,
            call_task,
            call_provider,
            call_model,
            prompt_id,
            call_prompt_version,
            schema_id,
            call_schema_version,
            generation_settings,
            call_input_hash,
            repair_attempts,
        ) = links[0]
        schema = registry.schema_for(schema_id, call_schema_version)
        if (
            schema is None
            or not isinstance(redacted_input, dict)
            or not isinstance(generation_settings, dict)
        ):
            return "REQUEST_SHAPE_UNVERIFIABLE"
        # What this ATTEMPT physically carried: the logical row holds the starting settings, and a
        # truncation retry was dispatched at a raised ceiling recorded in repair_attempts.
        physical_settings = _physical_generation_settings(
            generation_settings, repair_attempts, attempt_no)
        computed_prompt_hash = _content_hash(
            redacted_input.get("instruction")
        )
        computed_schema_hash = _content_hash(schema)
        computed_contract_hash = _content_hash({
            "call_role": call_role or task,
            "provider": physical_settings.get("provider"),
            "model": physical_settings.get("model"),
            "generation_settings": physical_settings,
            "prompt_id": prompt_id,
            "prompt_version": call_prompt_version,
            "prompt_content_hash": computed_prompt_hash,
            "output_schema_id": schema_id,
            "output_schema_version": call_schema_version,
            "schema_content_hash": computed_schema_hash,
        })
        physical_request = LLMRequest(
            task=task,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            inputs=redacted_input,
            output_schema_id=schema_id,
            output_schema_version=schema_version,
            generation_settings=physical_settings,
            output_schema=schema,
        )
        outcomes = conn.execute(
            "SELECT outcome FROM llm_dispatch_outcome WHERE dispatch_ref=%s",
            (dispatch_ref,),
        ).fetchall()
        trace_link = conn.execute(
            "SELECT count(*) FROM formula_authoring_trace_event "
            "WHERE authoring_run_id=%s AND llm_call_ref=%s",
            (authoring_run_id, llm_call_ref),
        ).fetchone()
        if (
            not isinstance(physical_hash, str)
            or len(physical_hash) != 64
            or any(char not in "0123456789abcdef" for char in physical_hash)
        ):
            return "PHYSICAL_REQUEST_HASH_INVALID"
        checks = (
            (compute_input_hash(redacted_input) == input_hash, "DISPATCH_INPUT_HASH_MISMATCH"),
            (canonical_hash == input_hash, "CANONICAL_INPUT_HASH_MISMATCH"),
            (call_input_hash == input_hash, "LOGICAL_CALL_INPUT_HASH_MISMATCH"),
            (call_task == task, "TASK_MISMATCH"),
            (call_provider == provider, "PROVIDER_MISMATCH"),
            (call_model == model, "MODEL_MISMATCH"),
            (call_prompt_version == prompt_version, "PROMPT_VERSION_MISMATCH"),
            (call_schema_version == schema_version, "SCHEMA_VERSION_MISMATCH"),
            (prompt_content_hash == computed_prompt_hash, "PROMPT_CONTENT_HASH_MISMATCH"),
            (schema_content_hash == computed_schema_hash, "SCHEMA_CONTENT_HASH_MISMATCH"),
            (provider_contract_hash == computed_contract_hash, "PROVIDER_CONTRACT_HASH_MISMATCH"),
            (
                physical_hash == compute_physical_request_hash(physical_request),
                "PHYSICAL_REQUEST_HASH_MISMATCH",
            ),
            (len(outcomes) == 1, "DISPATCH_OUTCOME_CARDINALITY_INVALID"),
            (
                bool(outcomes)
                and outcomes[0][0] in {"response_received", "transport_failed"},
                "DISPATCH_OUTCOME_INVALID",
            ),
            (trace_link is not None and trace_link[0] == 1, "TRACE_LINK_INVALID"),
        )
        for passed, reason in checks:
            if not passed:
                return reason
    return None


def formula_dispatches_reconciled(conn, authoring_run_id: str) -> bool:
    """Require one strict, internally consistent audit chain for every physical dispatch."""
    return formula_dispatch_reconciliation_failure(conn, authoring_run_id) is None


# ---- C5-T3: the auditing-client wrapper (the dispatch seam) --------------------------------------


@dataclass(frozen=True)
class SpendBindingV1:
    """One job's approved ceiling, bound to EVERY physical call an authoring run makes (§11.2).

    ``call_tokens``/``call_cost`` are the WORST CASE reserved per call — the approval's own
    arithmetic (``max_tokens / max_calls``, ``max_cost / max_calls``), so the two ceilings are
    enforced JOINTLY: a call that would push past either refuses before egress.
    """

    spend_authorization_id: str
    call_tokens: int
    call_cost: object   # Decimal-compatible; the reservation writer casts


@dataclass(frozen=True)
class DispatchAuditContext:
    """The ingestion-audit context a call site threads into ``audited_structured_call``: WHICH
    ingestion run + stage this logical call serves, and WHICH catalog objects/fields it is about.
    Each subject is a ``{catalog_source, object_ref, logical_ref, field_names}`` mapping (the
    ``llm_dispatch_subject`` attribution grain). ``ingestion_run_id`` may be None for a dispatch
    outside an ingestion run — recorded honestly as NULL."""

    ingestion_run_id: str | None
    stage: str
    subjects: Sequence[dict] = ()
    authoring_run_id: str | None = None
    call_role: str | None = None
    turn_index: int | None = None
    provider_contract_hash: str | None = None
    prompt_content_hash: str | None = None
    schema_content_hash: str | None = None
    lease_fence: LeaseFence | None = None
    #: ▲ THE MONEY, at the ONE seam that sees every PHYSICAL attempt (owner ruling 2026-08-23
    #: item 3). `drive_structured_call` retries and repairs beneath the logical call — up to
    #: 1 + 2 retries + 2 repairs = 5 physical calls per structured call — so a ceiling enforced
    #: anywhere above this seam under-counts by exactly the retries. When set, every physical
    #: attempt reserves WORST CASE before egress, in the same pre-dispatch transaction as its
    #: audit row, and settles actuals after the response.
    spend_authorization_id: str | None = None
    #: Worst case for ONE physical call, priced by the caller on the SAME basis as the
    #: authorization's ceiling — pricing policy stays out of the audit seam.
    spend_call_tokens: int = 0
    spend_call_cost: str = "0"


class AuditingClient:
    """LLMClient wrapper: audits EVERY physical provider attempt BEFORE egress, fail-closed.

    ``drive_structured_call`` re-invokes ``client.call`` for each repair/retry attempt, so
    wrapping the client is the ONE seam that sees every physical request. Per attempt:
    increment ``attempt_no`` (1-based, shared ``logical_call_ref`` — the caller mints it once per
    logical call so UNIQUE(logical_call_ref, attempt_no) keys the attempts), ``record_dispatch``
    BEFORE egress, then call the inner provider, then append the transport outcome.

    FAIL-CLOSED: on ``AuditUnavailable`` the inner provider is NEVER called; the wrapper returns
    the exact signal a real pre-response transport failure produces today — ``ClaudeLLM.call``
    maps ``anthropic.APIConnectionError`` (no response from the provider) to a RETURNED
    ``PROVIDER_TRANSIENT`` result (``llm_claude._fail``), never a raise — so
    ``drive_structured_call`` bounded-retries (each retry re-attempts the audit; a store that
    recovers mid-call yields a properly audited egress) and otherwise fails into STATUS_FAILED
    with no egress ever having happened."""

    def __init__(self, inner: LLMClient, ctx: DispatchAuditContext, *, logical_call_ref: str,
                 redaction_version: str | None = None) -> None:
        self._inner = inner
        self._ctx = ctx
        self._logical_call_ref = logical_call_ref
        self._redaction_version = redaction_version
        self._attempt_no = 0
        self._dispatch_refs: list[str] = []

    @property
    def dispatch_refs(self) -> tuple[str, ...]:
        """The dispatch_ref of every successfully audited physical attempt, in call order
        (C5-T4): the caller links the logical llm_call back to these via ``link_llm_call``.
        Read-only snapshot — fail-closed attempts (AuditUnavailable, no egress) never appear."""
        return tuple(self._dispatch_refs)

    def call(self, request: LLMRequest) -> LLMResult:
        if not get_settings().dsn:
            # No durable audit STORE is configured (dev/test/degraded config) — dispatch provenance
            # is UNAVAILABLE, not FAILED: proceed UNAUDITED rather than halt all enrichment. The
            # fail-closed guarantee below (a CONFIGURED store whose write fails -> no egress) still
            # holds wherever the store exists; in production FEATUREGEN_DSN is always set, and a
            # startup check should assert it so egress is never silently unaudited there.
            return self._inner.call(request)
        self._attempt_no += 1
        gen = request.generation_settings or {}
        prompt_content_hash = self._ctx.prompt_content_hash or _content_hash(
            request.inputs.get("instruction"))
        schema_content_hash = self._ctx.schema_content_hash or _content_hash(
            request.output_schema)
        provider_contract_hash = self._ctx.provider_contract_hash or _content_hash({
            "call_role": self._ctx.call_role or request.task,
            "provider": gen.get("provider"),
            "model": gen.get("model"),
            "generation_settings": gen,
            "prompt_id": request.prompt_id,
            "prompt_version": request.prompt_version,
            "prompt_content_hash": prompt_content_hash,
            "output_schema_id": request.output_schema_id,
            "output_schema_version": request.output_schema_version,
            "schema_content_hash": schema_content_hash,
        })
        try:
            dispatch_ref = record_dispatch(
                logical_call_ref=self._logical_call_ref, attempt_no=self._attempt_no,
                ingestion_run_id=self._ctx.ingestion_run_id, stage=self._ctx.stage,
                task=request.task, redacted_input=dict(request.inputs),
                input_hash=compute_input_hash(request.inputs),
                subjects=[dict(s) for s in self._ctx.subjects],
                redaction_version=self._redaction_version,
                provider=gen.get("provider"), model=gen.get("model"),
                prompt_version=request.prompt_version,
                schema_version=request.output_schema_version,
                physical_request_hash=compute_physical_request_hash(request),
                authoring_run_id=self._ctx.authoring_run_id,
                spend_authorization_id=self._ctx.spend_authorization_id,
                spend_call_tokens=self._ctx.spend_call_tokens,
                spend_call_cost=self._ctx.spend_call_cost,
                call_role=self._ctx.call_role,
                turn_index=self._ctx.turn_index,
                canonical_turn_input_hash=compute_input_hash(request.inputs),
                provider_contract_hash=provider_contract_hash,
                prompt_content_hash=prompt_content_hash,
                schema_content_hash=schema_content_hash,
                lease_fence=self._ctx.lease_fence)
        except AuditUnavailable:
            logger.warning(
                "pre-dispatch audit unavailable for %s attempt %s — provider NOT called "
                "(fail closed)", self._logical_call_ref, self._attempt_no)
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_TRANSIENT)
        except SpendExhausted as exc:
            # ▲ NO EGRESS, and NOT transient. A transient result would have the driver retry —
            # burning its retry budget to re-ask a ceiling that cannot change mid-run. AUTH_ERROR
            # is the driver's fail-closed-immediately arm (security-audit signalled), which is the
            # honest classification: this deployment is not authorized to spend on this call. The
            # reservation AND the dispatch row rolled back together — an attempt the budget refused
            # leaves no audited egress, because none happened.
            logger.warning(
                "spend authorization refused %s attempt %s — provider NOT called: %s",
                self._logical_call_ref, self._attempt_no, exc)
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_AUTH_ERROR)
        # The attempt is durably authorized — record it for llm_call linkage (C5-T4) BEFORE the
        # provider call, so even a transport raise stays attributable to the logical call.
        self._dispatch_refs.append(dispatch_ref)
        try:
            result = self._inner.call(request)
        except Exception:
            # a REAL transport raise stays a raise — recorded first, then re-raised unchanged.
            # ▲ The reservation is deliberately NOT settled here: whether the provider billed a
            # transport-level failure is unknowable at this seam, so the reservation ages out and
            # the RECONCILER settles it against this very outcome row (transport_failed -> zero).
            self._record_outcome(dispatch_ref, "transport_failed")
            raise
        self._record_outcome(dispatch_ref, "response_received")
        self._settle_spend(dispatch_ref, result)
        return result

    def _settle_spend(self, dispatch_ref: str, result: LLMResult) -> None:
        # POST-egress best-effort, the `_record_outcome` stance: the money was already committed at
        # reservation time, so a settlement failure must never mask the provider's result — the
        # reconciler settles an aged unsettled reservation from the outcome row instead. Actuals
        # come from the provider's own usage when reported; a response with NO usage settles at
        # WORST CASE, because assuming an unreported call was free buys its tokens twice.
        if self._ctx.spend_authorization_id is None:
            return
        try:
            from featuregen.overlay.upload.llm_spend import (
                reservation_for_dispatch,
                settle_spend,
            )

            usage = result.cost_metadata or {}
            tokens = sum(int(v) for k, v in usage.items()
                         if k.endswith("_tokens") and isinstance(v, (int, float))
                         and not isinstance(v, bool))
            with psycopg.connect(get_settings().dsn) as conn:
                reservation = reservation_for_dispatch(conn, dispatch_ref)
                if reservation is not None:
                    settle_spend(
                        conn, reservation,
                        actual_calls=1,
                        actual_tokens=tokens if tokens else self._ctx.spend_call_tokens,
                        actual_cost=(usage.get("cost")
                                     if isinstance(usage.get("cost"), (int, float))
                                     else self._ctx.spend_call_cost))
        except Exception:  # noqa: BLE001
            logger.exception("spend settlement failed for %s (reconciler will settle it)",
                             dispatch_ref)

    def _record_outcome(self, dispatch_ref: str, outcome: str) -> None:
        # POST-egress best-effort: the dispatch already happened under a durable pre-dispatch
        # record, so an outcome-write failure must never mask the real result/exception
        # (mirrors _record_llm_call_durable's post-egress stance). Logged, never raised.
        try:
            record_dispatch_outcome(dispatch_ref=dispatch_ref, outcome=outcome)
        except Exception:  # noqa: BLE001
            logger.exception("dispatch outcome write failed for %s (%s)", dispatch_ref, outcome)

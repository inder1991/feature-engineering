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

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from featuregen.config import get_settings
from featuregen.idgen import mint_id
from featuregen.intake.llm import (
    PROVIDER_TRANSIENT,
    LLMClient,
    LLMRequest,
    LLMResult,
    compute_input_hash,
)

logger = logging.getLogger(__name__)


class AuditUnavailable(Exception):
    """The pre-dispatch audit could not be durably committed — the caller must NOT dispatch."""


def record_dispatch(*, logical_call_ref: str, attempt_no: int, ingestion_run_id: str | None,
                    stage: str, task: str, redacted_input: dict, input_hash: str,
                    subjects: list[dict], redaction_version: str | None = None,
                    provider: str | None = None, model: str | None = None,
                    prompt_version: int | None = None,
                    schema_version: int | None = None) -> str:
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
            row = audit_conn.execute(
                "INSERT INTO llm_dispatch (dispatch_ref, logical_call_ref, attempt_no, "
                "ingestion_run_id, stage, task, input_hash, redacted_input, redaction_version, "
                "provider, model, prompt_version, schema_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (logical_call_ref, attempt_no) DO NOTHING RETURNING dispatch_ref",
                (dispatch_ref, logical_call_ref, attempt_no, ingestion_run_id, stage, task,
                 input_hash, Jsonb(redacted_input), redaction_version, provider, model,
                 prompt_version, schema_version)).fetchone()
            if row is None:
                # Idempotent replay: this (logical_call_ref, attempt_no) is already audited.
                # The header is write-once, so the prior record is authoritative — return it.
                existing = audit_conn.execute(
                    "SELECT dispatch_ref FROM llm_dispatch "
                    "WHERE logical_call_ref = %s AND attempt_no = %s",
                    (logical_call_ref, attempt_no)).fetchone()
                if existing is None:   # unreachable outside a torn DB — still fail closed
                    raise AuditUnavailable(
                        f"pre-dispatch audit conflict for {logical_call_ref!r} attempt "
                        f"{attempt_no} but the prior record could not be read back")
                return existing[0]
            for subject in subjects:
                audit_conn.execute(
                    "INSERT INTO llm_dispatch_subject (dispatch_ref, catalog_source, "
                    "object_ref, logical_ref, field_names) VALUES (%s, %s, %s, %s, %s)",
                    (dispatch_ref, subject.get("catalog_source"), subject.get("object_ref"),
                     subject.get("logical_ref"), Jsonb(subject.get("field_names") or [])))
        return dispatch_ref
    except AuditUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — ANY durability failure means: do not dispatch
        logger.exception("pre-dispatch audit write failed for logical_call_ref=%s attempt=%s",
                         logical_call_ref, attempt_no)
        raise AuditUnavailable(
            f"pre-dispatch audit could not be durably committed for {logical_call_ref!r} "
            f"attempt {attempt_no} — egress is not authorized") from exc


def link_llm_call(*, llm_call_ref: str, dispatch_refs: Sequence[str],
                  ingestion_run_id: str | None, stage: str) -> None:
    """C5-T4: associate the logical ``llm_call`` back to the physical dispatch(es) that carried it
    (``llm_call_dispatch``) and, when it served an ingestion run, to that run
    (``ingestion_run_llm_call``). Same OWN-connection discipline as ``record_dispatch`` (fresh
    ``get_settings().dsn`` connection, committed independently); both INSERTs are ``ON CONFLICT DO
    NOTHING`` against the migration-1005 UNIQUEs, so a replay never duplicates an association.

    FAIL-SOFT, deliberately unlike ``record_dispatch``'s raise: by the time the associations are
    written, the pre-dispatch authorization AND the immutable llm_call are already durable — a
    link-write failure loses convenience joins, not evidence — so it is logged and swallowed,
    mirroring ``AuditingClient._record_outcome``'s post-egress posture. No DSN (tests / no-DB
    harness) means no durable link store: logged, nothing written."""
    dsn = get_settings().dsn
    if not dsn:
        logger.warning("no FEATUREGEN_DSN configured — llm_call linkage for %s not durably "
                       "recorded", llm_call_ref)
        return
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
    except Exception:  # noqa: BLE001 — post-egress linkage must never mask the real result
        logger.exception("llm_call linkage write failed for llm_call_ref=%s (run=%s, %d "
                         "dispatches)", llm_call_ref, ingestion_run_id, len(dispatch_refs))


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


# ---- C5-T3: the auditing-client wrapper (the dispatch seam) --------------------------------------


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
        self._attempt_no += 1
        gen = request.generation_settings or {}
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
                schema_version=request.output_schema_version)
        except AuditUnavailable:
            logger.warning(
                "pre-dispatch audit unavailable for %s attempt %s — provider NOT called "
                "(fail closed)", self._logical_call_ref, self._attempt_no)
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_TRANSIENT)
        # The attempt is durably authorized — record it for llm_call linkage (C5-T4) BEFORE the
        # provider call, so even a transport raise stays attributable to the logical call.
        self._dispatch_refs.append(dispatch_ref)
        try:
            result = self._inner.call(request)
        except Exception:
            # a REAL transport raise stays a raise — recorded first, then re-raised unchanged.
            self._record_outcome(dispatch_ref, "transport_failed")
            raise
        self._record_outcome(dispatch_ref, "response_received")
        return result

    def _record_outcome(self, dispatch_ref: str, outcome: str) -> None:
        # POST-egress best-effort: the dispatch already happened under a durable pre-dispatch
        # record, so an outcome-write failure must never mask the real result/exception
        # (mirrors _record_llm_call_durable's post-egress stance). Logged, never raised.
        try:
            record_dispatch_outcome(dispatch_ref=dispatch_ref, outcome=outcome)
        except Exception:  # noqa: BLE001
            logger.exception("dispatch outcome write failed for %s (%s)", dispatch_ref, outcome)

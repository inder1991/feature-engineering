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

import psycopg
from psycopg.types.json import Jsonb

from featuregen.config import get_settings
from featuregen.idgen import mint_id

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

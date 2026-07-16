"""Durable ingestion-run manifest — the lifecycle module (first-release hardening design #3, CORE).

Every ingestion attempt leaves a queryable ``ingestion_run`` row (who / what / when / under what
settings / with what outcome) plus an append-only ``ingestion_run_status_event`` history. The
durability model, per the design's review-corrected lifecycle:

* ``open_run`` writes the ``in_progress`` row on a FRESH independent connection resolved from
  ``get_settings().dsn`` and COMMITS immediately (the ``enrich_llm._record_llm_call_durable``
  pattern) — so the manifest survives the request transaction rolling back, and a parse/oversize/
  unsupported failure still has a run row. The fresh connection performs bare INSERTs and NEVER
  takes an advisory lock (it must not be able to wait on the ingest's source lock — the
  program-audit I-3 self-deadlock class). No DSN configured (the rolled-back test harness) or a
  failed connect degrades, best-effort, to the caller's connection: a transactional manifest
  beats none, and the failure is logged.
* ``terminalize_run`` runs on the GIVEN connection, so an ``ingested`` terminal state commits
  ATOMICALLY with the ingest transaction it describes — ``ingested`` can never be recorded for a
  transaction that then fails. Idempotent-safe: only an ``in_progress`` run transitions, so a
  double-terminalize neither clobbers the terminal state nor duplicates history.
* ``terminalize_run_durable`` is the route's exception-path variant: the request connection is
  rolling back, so the terminal state goes on its own fresh committing connection.

The ``pre/post_source_fingerprint`` pair (``source_fingerprint``) is CORRELATION state — "did the
source's graph state change around this run" — versioned by algo so it can evolve; it is not a
drift contract. ``file_sha256`` likewise supports correlation, not byte-level reproducibility
(the file itself is not retained). ``_effective_config_snapshot`` pins the ALLOWLISTED flags that
governed the run ONCE at open — never secrets, never a late re-read of the environment.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope

logger = logging.getLogger(__name__)

FINGERPRINT_ALGO_VERSION = "gn-v1"

_TERMINAL_STATUSES = frozenset({"ingested", "held", "rejected", "failed", "abandoned"})

_RUN_COLUMNS = (
    "id, origin_type, catalog_source, filename, file_sha256, actor_subject, actor_role_claims, "
    "authorization_decision, pre_source_fingerprint, post_source_fingerprint, "
    "fingerprint_algo_version, effective_config, row_count, quarantined_count, status, "
    "started_at, completed_at, heartbeat_at, redacted_failure_code")


def _effective_config_snapshot() -> dict:
    """The allowlisted, schema-versioned snapshot of the flags governing a run — pinned ONCE at
    ``open_run`` and stored in ``effective_config``. Exactly the design-#3 allowlist (feature
    switches + provider on/off + model); NEVER secrets, so the DSN / API keys / HMAC key must
    never be added here. The flag helpers are imported lazily: ingest.py is a heavy module and
    this one is imported by the route layer."""
    from featuregen.overlay.upload.graph import governed_joins_enabled
    from featuregen.overlay.upload.ingest import pass_c_enabled, table_synth_enabled

    return {
        "config_schema_version": 1,
        "governed_joins": governed_joins_enabled(),
        "pass_c": pass_c_enabled(),
        "table_synth": table_synth_enabled(),
        "llm_provider": os.environ.get("FEATUREGEN_LLM_PROVIDER") or None,
        "llm_model": os.environ.get("FEATUREGEN_LLM_MODEL") or None,
    }


def source_fingerprint(conn, catalog_source: str) -> tuple[str, str]:
    """``(hash, algo_version)`` for the source's CURRENT graph state, so a run's pre/post can be
    compared (unchanged re-upload? did this run change anything?).

    Contract ``gn-v1``: sha256 over the source's ``graph_node`` rows — the semantic columns
    (object_ref, kind, data_type, is_grain, is_as_of, concept, domain, sensitivity), sorted by
    object_ref, each value rendered as its ``str()`` with NULL as the empty string, unit-separated
    (0x1f) within a row and record-separated (0x1e) between rows. Deterministic because object_ref
    is unique per source and the ordering is total. This is CORRELATION state, not a full drift
    contract — decorative columns (search_doc, decision links) are deliberately excluded."""
    rows = conn.execute(
        "SELECT object_ref, kind, data_type, is_grain, is_as_of, concept, domain, sensitivity "
        "FROM graph_node WHERE catalog_source = %s ORDER BY object_ref",
        (catalog_source,)).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update("\x1f".join("" if v is None else str(v) for v in row).encode())
        digest.update(b"\x1e")
    return digest.hexdigest(), FINGERPRINT_ALGO_VERSION


def _clean_filename(filename: str | None) -> str | None:
    """Sanitized + length-capped (design #3): basename only (an upload's filename is client input —
    never store a path), capped at 200 chars."""
    if not filename:
        return None
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:200] or None


def _insert_run(conn, run_id: str, *, origin_type: str, catalog_source: str,
                filename: str | None, actor: IdentityEnvelope, effective_config: dict,
                now: datetime) -> None:
    conn.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, filename, actor_subject, "
        "actor_role_claims, effective_config, status, started_at, heartbeat_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'in_progress', %s, %s)",
        (run_id, origin_type, catalog_source, _clean_filename(filename), actor.subject,
         list(actor.role_claims), Jsonb(effective_config), now, now))
    _append_status_event(conn, run_id, status="in_progress", at=now, reason_code=None)


def _append_status_event(conn, run_id: str, *, status: str, at: datetime,
                         reason_code: str | None) -> None:
    conn.execute(
        "INSERT INTO ingestion_run_status_event (ingestion_run_id, status, at, reason_code) "
        "VALUES (%s, %s, %s, %s)", (run_id, status, at, reason_code))


def open_run(conn, *, origin_type: str, catalog_source: str, filename: str | None,
             actor: IdentityEnvelope, effective_config: dict, now: datetime) -> str:
    """Mint + durably record an ``in_progress`` run; returns the run id.

    Independent-commit rule: the INSERT goes on a FRESH connection from ``get_settings().dsn``,
    committed immediately, so the run row survives the request transaction rolling back (a parse
    failure still has a manifest). Best-effort degradation to the caller's ``conn`` when no DSN is
    configured (test harness) or the connect fails — logged, never fatal to the upload. The fresh
    connection takes NO advisory lock (I-3 self-deadlock class)."""
    run_id = mint_id("ingrun")
    kwargs = dict(origin_type=origin_type, catalog_source=catalog_source, filename=filename,
                  actor=actor, effective_config=effective_config, now=now)
    dsn = get_settings().dsn
    if dsn:
        try:
            with psycopg.connect(dsn) as run_conn:   # own tx, committed on `with` exit
                _insert_run(run_conn, run_id, **kwargs)
            return run_id
        except Exception:  # noqa: BLE001 — degraded manifest must never fail the upload itself
            logger.exception(
                "durable ingestion_run open failed; falling back to the request connection")
    _insert_run(conn, run_id, **kwargs)
    return run_id


def terminalize_run(conn, run_id: str, *, status: str, now: datetime,
                    row_count: int | None = None, quarantined_count: int | None = None,
                    file_sha256: str | None = None, pre_fingerprint: str | None = None,
                    post_fingerprint: str | None = None,
                    fingerprint_algo_version: str | None = None,
                    redacted_failure_code: str | None = None,
                    reason_code: str | None = None) -> bool:
    """Transition an ``in_progress`` run to a terminal status ON THE GIVEN CONNECTION (an
    ``ingested`` terminalize must commit atomically with the ingest transaction — never record
    ``ingested`` for a tx that then fails). Returns True when this call performed the transition;
    False when the run was already terminal (or unknown) — idempotent-safe, nothing clobbered."""
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"{status!r} is not a terminal ingestion_run status "
                         f"(expected one of {sorted(_TERMINAL_STATUSES)})")
    row = conn.execute(
        "UPDATE ingestion_run SET status = %s, completed_at = %s, heartbeat_at = %s, "
        "row_count = %s, quarantined_count = %s, file_sha256 = %s, "
        "pre_source_fingerprint = %s, post_source_fingerprint = %s, "
        "fingerprint_algo_version = %s, redacted_failure_code = %s "
        "WHERE id = %s AND status = 'in_progress' RETURNING id",
        (status, now, now, row_count, quarantined_count, file_sha256, pre_fingerprint,
         post_fingerprint, fingerprint_algo_version, redacted_failure_code, run_id)).fetchone()
    if row is None:
        return False
    _append_status_event(conn, run_id, status=status, at=now, reason_code=reason_code)
    return True


def terminalize_run_durable(run_id: str, *, status: str, now: datetime,
                            row_count: int | None = None, quarantined_count: int | None = None,
                            file_sha256: str | None = None, pre_fingerprint: str | None = None,
                            post_fingerprint: str | None = None,
                            fingerprint_algo_version: str | None = None,
                            redacted_failure_code: str | None = None,
                            reason_code: str | None = None, fallback_conn=None) -> None:
    """``terminalize_run`` on a FRESH independent connection — the route's exception path, where
    the request transaction is rolling back and would take the terminal state down with it.
    Best-effort: no DSN / failed connect degrades to ``fallback_conn`` when given (the test
    harness; in production that write shares the rolling-back tx's fate — logged either way)."""
    kwargs = dict(status=status, now=now, row_count=row_count,
                  quarantined_count=quarantined_count, file_sha256=file_sha256,
                  pre_fingerprint=pre_fingerprint, post_fingerprint=post_fingerprint,
                  fingerprint_algo_version=fingerprint_algo_version,
                  redacted_failure_code=redacted_failure_code, reason_code=reason_code)
    dsn = get_settings().dsn
    if dsn:
        try:
            with psycopg.connect(dsn) as run_conn:   # own tx, committed on `with` exit
                terminalize_run(run_conn, run_id, **kwargs)
            return
        except Exception:  # noqa: BLE001 — a lost terminal state must not mask the real failure
            logger.exception(
                "durable ingestion_run terminalize failed; falling back to the request connection")
    if fallback_conn is not None:
        try:
            terminalize_run(fallback_conn, run_id, **kwargs)
            return
        except Exception:  # noqa: BLE001 — called from except paths: never mask the real failure
            logger.exception("fallback ingestion_run terminalize failed for %s", run_id)
    logger.warning("ingestion_run %s could not be terminalized to %s — the reconciliation sweep "
                   "will abandon it once its heartbeat lease expires", run_id, status)


def get_run(conn, run_id: str) -> dict | None:
    """The run row + its append-only status history (``status_history``), or None."""
    cur = conn.execute(f"SELECT {_RUN_COLUMNS} FROM ingestion_run WHERE id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        return None
    run = dict(zip((d.name for d in cur.description), row, strict=True))
    run["status_history"] = [
        {"status": status, "at": at, "reason_code": reason_code}
        for status, at, reason_code in conn.execute(
            "SELECT status, at, reason_code FROM ingestion_run_status_event "
            "WHERE ingestion_run_id = %s ORDER BY at, id", (run_id,)).fetchall()]
    return run

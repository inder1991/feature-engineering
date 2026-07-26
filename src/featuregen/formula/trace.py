"""Child-1 Task 11 — the WRITE-ONCE, CRASH-SAFE authoring trace (design §H).

Three functions over the two append-only tables of migration 1020: ``open_authoring_run`` writes the
MANIFEST first, ``append_event`` appends immutable trace events, ``run_status`` derives the run's
disposition. Two load-bearing invariants:

* CRASH-SAFE HONESTY `[c12]` — there is NO run status column anywhere. ``run_status`` derives
  *incomplete* purely from the ABSENCE of a terminal (``COMPLETED``/``FAILED``) event, so a process
  that dies mid-authoring leaves an honestly incomplete run, and the durable ``llm_call`` rows that
  OUTLIVE a rolled-back request transaction can never make a run read as completed. No writer here
  ever UPDATEs a row that may not exist — every write is a single bare INSERT. A run this module has
  never heard of is *incomplete* too: the derivation can only ever fail TOWARD incomplete, never
  toward completed.
* WRITE-ONCE — enforced physically by migration 1020's triggers on BOTH tables (UPDATE / DELETE /
  DELETE-by-TRUNCATE all raise), and no event may be appended after a terminal event. This module
  simply has no code path that could mutate a row: INSERT and SELECT only.

DURABLE FRESH-CONNECTION PATTERN (mirror of ``overlay.upload.enrich_llm._record_llm_call_durable``):
the provider audit (``llm_call``) is committed on its OWN connection, so if the trace shared the
request transaction's fate a rolled-back request would leave audited provider calls with NO
manifest and NO trace — evidence that content egressed, with nothing saying which run made it. So
the manifest and every event are written on a FRESH connection opened from ``get_settings().dsn``
(the FULL configured DSN — NOT ``conn.info.dsn``, which psycopg3 strips the password from: that was
a real bug fixed on this codebase), each committing independently, performing ONE bare INSERT, and
NEVER taking an advisory lock (a second lock-taking connection self-deadlocked in program-audit
I-3). When no DSN is configured (the tests / no-DB harness) the write goes on the caller's
connection — the designed harness path, not a degradation; any CONNECTION-DEPENDENT failure with a
DSN configured (a connect/commit blip, or a ``ForeignKeyViolation`` against a referent that only
exists on the caller's uncommitted transaction because an upstream durable write itself degraded)
logs and degrades the same way (transactional evidence beats none, and an evidence-free crash then
reads as *incomplete*, which is the honest outcome). Only CONNECTION-INDEPENDENT rejections
propagate — see ``_DETERMINISTIC_REJECTIONS``. ``run_status`` READS through the same fresh-connection
path (``_durable_read``), because a read on the caller's connection cannot see those independent
commits when the caller is pinned to ``REPEATABLE READ`` — which ``api.deps.get_feature_gen_conn``,
the natural host for authoring, does.

``payload`` is CANONICAL REDACTED METADATA ONLY — tool result identities/verdicts/hashes,
dispositions, reason codes — NEVER raw catalog data values, matching the metadata-only discipline of
``formula.tools``. It must be a JSON object of JSON-primitive values; ``payload_hash`` is the sha256
over its RFC 8785 (JCS) bytes, so a stored tool result is tamper-evident and the hash is
recomputable from the read-back jsonb.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.config import get_settings
from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.formula._jcs import dumps as _jcs_dumps

__all__ = [
    "TERMINAL_KINDS",
    "RunStatus",
    "TraceEventKind",
    "append_event",
    "open_authoring_run",
    "run_status",
]

logger = logging.getLogger(__name__)

RunStatus = Literal["incomplete", "completed", "failed"]


class TraceEventKind(StrEnum):
    """The CLOSED §H trace vocabulary: STARTED -> LLM_CALL_RECORDED | TOOL_CALLED |
    TOOL_RESULT_RECORDED | CRITIC_RECORDED -> COMPLETED | FAILED. Mirrored EXACTLY by migration
    1020's ``kind`` CHECK, so an unknown kind is rejected in code AND at the database."""

    STARTED = "STARTED"
    LLM_CALL_RECORDED = "LLM_CALL_RECORDED"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_RESULT_RECORDED = "TOOL_RESULT_RECORDED"
    CRITIC_RECORDED = "CRITIC_RECORDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: The two kinds that CLOSE a run. Their presence is the ONLY thing that makes a run non-incomplete,
#: and migration 1020's partial UNIQUE index permits at most one of them per run.
TERMINAL_KINDS: frozenset[TraceEventKind] = frozenset(
    {TraceEventKind.COMPLETED, TraceEventKind.FAILED})

_INSERT_RUN = (
    "INSERT INTO authoring_run (authoring_run_id, intent_hash, versions, actor) "
    "VALUES (%s, %s, %s, %s)"
)
_INSERT_EVENT = (
    "INSERT INTO authoring_trace_event (authoring_trace_event_id, authoring_run_id, seq, kind, "
    "llm_call_ref, idempotency_key, payload, payload_hash) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)
_SELECT_TERMINAL = (
    "SELECT kind FROM authoring_trace_event "
    "WHERE authoring_run_id = %s AND kind IN (%s, %s) LIMIT 1"
)


def open_authoring_run(conn: DbConn, *, intent_hash: str, versions: Mapping[str, Any],
                       actor: IdentityEnvelope) -> str:
    """Open a run by writing its MANIFEST first (before any provider call) and return its id.

    ``versions`` is the object of every rule/registry version stamped for this run (grammar /
    capability / critic / disposition policy…); ``actor`` is serialized with the sanctioned
    ``identity_to_jsonb``. Written on the durable fresh connection (module docstring), so a
    rolled-back request cannot leave audited provider calls without a manifest."""
    run_id = mint_id("arun")
    _durable_write(
        conn, _INSERT_RUN,
        (run_id, intent_hash, Jsonb(_json_object(versions, "versions")),
         Jsonb(identity_to_jsonb(actor))),
        what=f"authoring_run {run_id}")
    return run_id


def append_event(conn: DbConn, run_id: str, kind: TraceEventKind | str, *, seq: int,
                 idempotency_key: str, llm_call_ref: str | None = None,
                 payload: Mapping[str, Any]) -> str:
    """Append ONE immutable trace event and return its ``authoring_trace_event_id``.

    ``kind`` must be in the closed §H vocabulary (``ValueError`` otherwise, before anything touches
    the DB). ``seq`` is the caller's position in the run (unique per run). ``idempotency_key`` is the
    globally unique retry/replay key. ``llm_call_ref`` links the immutable provider-call record this
    event evidences (None for events that made no call). ``payload`` is canonical redacted metadata
    only (module docstring) and is hashed into ``payload_hash``.

    FAILURE TAXONOMY. A CONNECTION-INDEPENDENT rejection RAISES the underlying ``psycopg`` error: a
    duplicate seq or idempotency key (``UniqueViolation``), an append AFTER a terminal event or a
    write-once violation (``RaiseException``), a kind/payload the DB CHECKs reject
    (``CheckViolation``). Those are real integrity failures and are never retried anywhere.

    A CONNECTION-DEPENDENT failure does NOT raise: it degrades to the caller's connection
    (``_durable_write``). ``ForeignKeyViolation`` is the load-bearing case, because the FK is checked
    on the TRACE connection: ``run_id`` and ``llm_call_ref`` are only visible there once DURABLY
    COMMITTED. The audited seam normally guarantees that for ``llm_call``
    (``_record_llm_call_durable`` commits it on its own connection) — but ONLY when that seam did not
    itself degrade. When it did (a counted, real production event: ``consume_audit_degradations``)
    the ``llm_call`` row sits on the caller's UNCOMMITTED transaction; likewise the manifest when
    ``open_authoring_run`` degraded. Falling back is what recovers those appends, on the one
    connection where the referent IS visible — the alternative was losing every subsequent event of
    that run. A transient ``OperationalError`` (a connect blip, a connection lost mid-INSERT, a
    commit failure) degrades for the same reason. A referent that exists NOWHERE still raises
    ``ForeignKeyViolation``, just from the caller's connection."""
    event_kind = TraceEventKind(kind)
    body = _json_object(payload, "payload")
    event_id = mint_id("atev")
    _durable_write(
        conn, _INSERT_EVENT,
        (event_id, run_id, seq, event_kind.value, llm_call_ref, idempotency_key, Jsonb(body),
         hashlib.sha256(_jcs_dumps(body)).hexdigest()),
        what=f"{event_kind.value} seq={seq} on {run_id}")
    return event_id


def run_status(conn: DbConn, run_id: str) -> RunStatus:
    """Derive the run's disposition from the trace ALONE: ``"incomplete"`` when the run has no
    terminal event (a live run, a process that died mid-authoring, or a run whose manifest never
    committed), else ``"completed"``/``"failed"`` from the single terminal event.

    There is no status column to drift, and nothing is inferred from ``llm_call`` rows — a run with
    dozens of durable, committed provider calls and no terminal event is still ``"incomplete"``.

    SYMMETRIC WITH THE WRITES, and deliberately so: the events are committed on the durable fresh
    connection, so the read goes through the SAME path (``_durable_read``) whenever a DSN is
    configured. Reading on the CALLER's connection instead was an isolation trap —
    ``api.deps.get_feature_gen_conn`` pins the feature-generation connection (the natural host for
    authoring: ``api.routes.contract._FeatureGenConn``) to ``REPEATABLE READ``, whose snapshot
    predates the terminal event this very request just committed elsewhere. It fails SAFE
    (``"incomplete"``), but an orchestrator reading its OWN terminal event would mislabel the run or
    re-author it.

    Isolation assumption (mirrors ``overlay.upload.ingestion_run.terminalize_run``): the fresh
    connection reads at the Postgres default ``READ COMMITTED``, which is what lets it see events
    committed a moment earlier by the durable writes. If that default is ever raised process-wide
    (a ``PGOPTIONS``/``ALTER DATABASE SET default_transaction_isolation``), this path needs a
    re-check — a fresh REPEATABLE READ snapshot taken at the SELECT is still after those commits, so
    it stays correct, but SERIALIZABLE would newly admit 40001 here. DSN-less (the tests / no-DB
    harness) it reads the caller's connection, where the writes also landed."""
    row = _durable_read(
        conn, _SELECT_TERMINAL,
        (run_id, TraceEventKind.COMPLETED.value, TraceEventKind.FAILED.value),
        what=f"run_status {run_id}")
    if row is None:
        return "incomplete"
    return "completed" if row[0] == TraceEventKind.COMPLETED.value else "failed"


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    """A trace payload/versions bundle is a JSON OBJECT (jsonb_typeof CHECK in 1020). Reject
    anything else HERE with a clear TypeError rather than letting a list/scalar reach the DB."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object (Mapping), got {type(value).__name__}")
    return dict(value)


#: Rejections that are CONNECTION-INDEPENDENT: Postgres would refuse the IDENTICAL statement on ANY
#: connection, so replaying it on the caller's would only reproduce the failure while poisoning the
#: caller's transaction. These PROPAGATE. Everything else is treated as CONNECTION-DEPENDENT and
#: falls back — deliberately the DEFAULT, so an unanticipated failure mode loses a transaction, not
#: the trace (the blanket-except contract of ``_record_llm_call_durable``, minus this refinement).
_DETERMINISTIC_REJECTIONS: tuple[type[Exception], ...] = (
    # A duplicate seq / idempotency key: an index entry is visible to the uniqueness check even from
    # an uncommitted concurrent transaction, so this verdict does not depend on WHICH connection asks.
    psycopg.errors.UniqueViolation,
    # The write-once triggers and the post-terminal guard (RAISE EXCEPTION → RaiseException, a
    # ProgrammingError subclass). ProgrammingError also covers a malformed statement, which is
    # malformed everywhere. NOTE: NOT SerializationFailure/DeadlockDetected — those are
    # OperationalError subclasses, i.e. transient, i.e. they DO fall back.
    psycopg.errors.RaiseException,
    psycopg.ProgrammingError,
    # A kind outside the closed vocabulary, a non-object payload/versions, seq < 0, a NULL column.
    psycopg.errors.CheckViolation,
    psycopg.errors.NotNullViolation,
    psycopg.DataError,
)


def _durable_write(conn: DbConn, sql: str, params: tuple, *, what: str) -> None:
    """Perform ONE bare INSERT on a FRESH connection from ``get_settings().dsn``, committed
    independently of the caller's transaction (module docstring for why). No advisory lock is ever
    taken, so this can never self-deadlock against a lock the request holds.

    * no DSN configured → write on the caller's connection (the designed tests / no-DB path);
    * a CONNECTION-DEPENDENT failure with a DSN configured → log and degrade to the caller's
      connection (transactional evidence beats none). That covers the connect/commit failure AND —
      critically — ``ForeignKeyViolation``, whose verdict depends on WHICH connection asks: a
      manifest or ``llm_call`` row that degraded onto the caller's UNCOMMITTED transaction is
      invisible from a fresh connection, so its dependent appends would otherwise be lost forever
      (every subsequent event of that run failing identically) despite succeeding on the caller's
      connection. Both degrade paths are real, counted production events upstream
      (``enrich_llm.consume_audit_degradations``), not hypotheticals;
    * a CONNECTION-INDEPENDENT rejection (``_DETERMINISTIC_REJECTIONS``) → PROPAGATES. Replaying a
      known-bad statement on the caller's connection would poison that transaction while producing
      the identical failure.

    The degrade path replays through ``_replay_on_caller``, never bare: it is replaying a statement
    the database may already have REJECTED once, and that rejection must not cost the caller its
    transaction."""
    dsn = get_settings().dsn
    if not dsn:
        conn.execute(sql, params)   # the designed tests / no-DB path, NOT a degradation
        return
    try:
        with psycopg.connect(dsn) as trace_conn:  # own tx, committed on clean `with` exit
            trace_conn.execute(sql, params)
        return
    except _DETERMINISTIC_REJECTIONS:
        raise
    except Exception:  # noqa: BLE001 — a connection-dependent failure must not lose the trace
        logger.exception(
            "durable authoring-trace write failed (%s); falling back to the request connection",
            what)
    _replay_on_caller(conn, sql, params)


def _replay_on_caller(conn: DbConn, sql: str, params: tuple) -> None:
    """Replay a degraded durable write on the CALLER's connection WITHOUT poisoning it.

    The fallback deliberately replays statements the database has ALREADY REJECTED once — the FK
    class is the whole point of it (``_durable_write``) — so it MUST leave the caller's transaction
    exactly as usable as it found it. A bare ``conn.execute`` did not: an FK the caller's connection
    also refuses aborted the request transaction, so the orchestrator lost every uncommitted thing it
    had done and could not execute another statement — not even its own terminal ``FAILED`` event
    through this very degraded path. Precedent for the shape: ``overlay.upload.enrich`` (program-audit
    finding I-2). A savepoint takes no advisory lock, so the I-3 self-deadlock does not apply.

    Two shapes, because ``conn.transaction()`` is not a savepoint when there is no transaction:

    * IN a transaction (the request shape) → SAVEPOINT. The rejection is contained and the caller's
      uncommitted work — including trace rows that degraded here earlier, the only copy there is —
      survives untouched. A blanket ``rollback()`` would keep the connection usable by destroying
      exactly the evidence this module exists to preserve.
    * IDLE → replay bare. ``conn.transaction()`` would BEGIN *and COMMIT* here, committing a degraded
      row whose whole point is to share the request's fate (and breaking the caller's control of its
      own transaction boundary). Nothing of the caller's is in flight, so if the replay is rejected
      the implicit single-statement transaction is discarded — that loses nothing and keeps the
      connection usable."""
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        try:
            conn.execute(sql, params)
        except Exception:
            conn.rollback()   # discard ONLY the rejected statement's implicit tx; nothing else is in it
            raise
        return
    with conn.transaction():   # savepoint: contain a rejected replay without poisoning the txn
        conn.execute(sql, params)


def _durable_read(conn: DbConn, sql: str, params: tuple, *, what: str) -> tuple | None:
    """The READ mirror of ``_durable_write``: one row, on a FRESH connection from
    ``get_settings().dsn`` so it SEES what the durable writes committed (``run_status`` for the
    isolation trap this closes). Read-only and single-statement, so there is nothing to commit and
    nothing to poison; a fresh connection cannot deadlock (no advisory lock, no write).

    Any failure degrades to the caller's connection — where a DEGRADED write's rows are the only
    copy anyway — so a connect blip can never turn a read into a request-failing exception. Nothing
    here is deterministic-rejection territory: a SELECT of committed rows either works or the
    connection is unusable."""
    dsn = get_settings().dsn
    if dsn:
        try:
            with psycopg.connect(dsn) as trace_conn:
                return trace_conn.execute(sql, params).fetchone()
        except Exception:  # noqa: BLE001 — a trace-read failure must never fail the caller
            logger.exception(
                "durable authoring-trace read failed (%s); falling back to the request connection",
                what)
    return conn.execute(sql, params).fetchone()

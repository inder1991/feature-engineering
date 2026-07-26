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
connection — the designed harness path, not a degradation; a genuine connect failure with a DSN
configured logs and degrades the same way (transactional evidence beats none, and an evidence-free
crash then reads as *incomplete*, which is the honest outcome).

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
    event evidences (None for events that made no call); it must be a DURABLY COMMITTED ``llm_call``
    row — which the audited seam guarantees, since ``_record_llm_call_durable`` commits it on its own
    connection — because this event's own INSERT rides a different connection and the FK is checked
    there. ``payload`` is canonical redacted metadata only (module docstring) and is hashed into
    ``payload_hash``.

    Raises the underlying ``psycopg`` error when the DB rejects the append: a duplicate seq or
    idempotency key (``UniqueViolation``), an unknown run or ``llm_call_ref``
    (``ForeignKeyViolation``), or an append AFTER a terminal event (``RaiseException``). Those are
    real integrity failures, never retried anywhere else."""
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
    dozens of durable, committed provider calls and no terminal event is still ``"incomplete"``."""
    row = conn.execute(
        "SELECT kind FROM authoring_trace_event "
        "WHERE authoring_run_id = %s AND kind IN (%s, %s) LIMIT 1",
        (run_id, TraceEventKind.COMPLETED.value, TraceEventKind.FAILED.value)).fetchone()
    if row is None:
        return "incomplete"
    return "completed" if row[0] == TraceEventKind.COMPLETED.value else "failed"


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    """A trace payload/versions bundle is a JSON OBJECT (jsonb_typeof CHECK in 1020). Reject
    anything else HERE with a clear TypeError rather than letting a list/scalar reach the DB."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object (Mapping), got {type(value).__name__}")
    return dict(value)


def _durable_write(conn: DbConn, sql: str, params: tuple, *, what: str) -> None:
    """Perform ONE bare INSERT on a FRESH connection from ``get_settings().dsn``, committed
    independently of the caller's transaction (module docstring for why). No advisory lock is ever
    taken, so this can never self-deadlock against a lock the request holds.

    * no DSN configured → write on the caller's connection (the designed tests / no-DB path);
    * connect failure with a DSN configured → log and degrade to the caller's connection
      (transactional evidence beats none);
    * the INSERT itself rejected (unique / FK / write-once / terminal-guard) → the error PROPAGATES.
      It is NOT retried on the caller's connection: replaying a known-bad statement there would
      poison the caller's transaction while producing the same failure."""
    dsn = get_settings().dsn
    if dsn:
        try:
            trace_conn = psycopg.connect(dsn)
        except Exception:  # noqa: BLE001 — a trace-connection failure must not lose the trace
            logger.exception(
                "durable authoring-trace connection failed (%s); falling back to the request "
                "connection", what)
        else:
            with trace_conn:   # own tx, committed on clean `with` exit; closed either way
                trace_conn.execute(sql, params)
            return
    conn.execute(sql, params)

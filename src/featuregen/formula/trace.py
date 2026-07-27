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
a real bug fixed on this codebase), each committing independently, performing ONE bare INSERT with
BOTH of its waits bounded — the CONNECT by a default ``connect_timeout`` (``_CONNECT_TIMEOUT_SECONDS``:
a blackholed host would otherwise hang the request in ``connect()``) and the INSERT's lock by
``lock_timeout`` (it can self-block on an index entry the request itself holds —
``_SET_LOCK_TIMEOUT``) — and NEVER taking an advisory lock (a second lock-taking connection
self-deadlocked in program-audit I-3). When no DSN is configured (the tests / no-DB harness) the
write goes on the caller's connection — the designed harness path, not a degradation; a
CONNECTION-DEPENDENT failure with a
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
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
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
    "TerminalEvent",
    "TraceEventKind",
    "append_event",
    "open_authoring_run",
    "read_run_intent_hash",
    "read_terminal_event",
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
#: The same row `_SELECT_TERMINAL` derives `run_status` from, WITH its tamper-evidence pair. At most
#: one row can match (1020's partial UNIQUE index on the terminal kinds), so `LIMIT 1` is the whole
#: result set, not an arbitrary pick.
_SELECT_TERMINAL_EVENT = (
    "SELECT kind, payload, payload_hash FROM authoring_trace_event "
    "WHERE authoring_run_id = %s AND kind IN (%s, %s) LIMIT 1"
)
_SELECT_RUN_INTENT_HASH = "SELECT intent_hash FROM authoring_run WHERE authoring_run_id = %s"

#: FALLBACK-ONLY variants of the two INSERTs (``_replay_on_caller``). COMMIT AMBIGUITY: when the
#: fresh connection's INSERT commits server-side but the ack is lost, the fallback replays a
#: statement the database HAS durably recorded — so unguarded it handed the caller a duplicate-key
#: error for an event that IS recorded. The replay therefore uses the row's own replay key as an
#: ON CONFLICT arbiter (``idempotency_key``, "the globally unique retry/replay key", for an event;
#: the minted ``authoring_run_id`` PK for a manifest — the only key ``open_authoring_run`` can
#: repeat) and CONVERGES on the durable row instead of raising.
#:
#: The PRIMARY statements above deliberately carry NO such clause: a genuine uniqueness breach on
#: the normal durable path is a CONNECTION-INDEPENDENT rejection and must still PROPAGATE
#: (``_DETERMINISTIC_REJECTIONS``). The narrowing this accepts is the mirror image: a duplicate key
#: reached ONLY via a degraded replay is treated as convergence rather than a breach, which is the
#: right call precisely because the replay's provenance is "the database already accepted this".
#: RESIDUAL, and DELIBERATELY DEFERRED — not impossible: a lost-ack replay of a TERMINAL event can
#: still surface the write-once RAISE from migration 1020's terminal guard, because that trigger
#: fires BEFORE any conflict check, so ``ON CONFLICT`` cannot absorb it. It IS fixable in this
#: module without touching the frozen migration (catch ``RaiseException`` on a terminal replay and
#: confirm convergence by re-reading the ``idempotency_key``); it is deferred because the outcome is
#: already honest — a run the caller can see as closed reads as closed, so ``run_status`` stays
#: correct either way — and the extra read is not worth its own failure mode for that.
_REPLAY_RUN = _INSERT_RUN + " ON CONFLICT (authoring_run_id) DO NOTHING"
_REPLAY_EVENT = _INSERT_EVENT + " ON CONFLICT (idempotency_key) DO NOTHING"

#: A BOUNDED wait on the durable connection's INSERT, because that INSERT can SELF-BLOCK against the
#: request that issued it: an event which degraded onto the caller's UNCOMMITTED transaction owns
#: 1020's (run, seq) and partial-terminal unique-index entries, and a later durable INSERT touching
#: the same entry waits on a lock only the caller can release — i.e. never, inside the request. Local
#: to the fresh connection's own transaction (``SET LOCAL``, discarded at its commit), so it can never
#: leak onto the caller's session. On expiry Postgres raises ``LockNotAvailable`` — an
#: ``OperationalError``, hence CONNECTION-DEPENDENT, hence the fallback replay, which is exactly
#: right: the caller's connection is the one place the blocking row is visible at all.
_SET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"

#: ``_SET_LOCK_TIMEOUT`` bounds the LOCK, not the CONNECT — and ``get_settings().dsn`` is the raw
#: configured DSN, so a BLACKHOLED database host (a dropped route, a firewall that discards rather
#: than refuses) left ``psycopg.connect`` waiting on libpq's default: forever, inside a request, on
#: the very path whose whole point is that losing the trace must never cost the caller. A DEFAULT,
#: not an override: an operator DSN that names its own ``connect_timeout`` still wins (the merge
#: order in ``_bounded_connect_dsn``), which is also why the tests' own ``connect_timeout=1`` blip
#: DSNs keep failing in one second rather than five.
_CONNECT_TIMEOUT_SECONDS = 5


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
        replay_sql=_REPLAY_RUN, what=f"authoring_run {run_id}")
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
        replay_sql=_REPLAY_EVENT, what=f"{event_kind.value} seq={seq} on {run_id}")
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
    harness) it reads the caller's connection, where the writes also landed.

    BOTH COPIES, ABSENCE-DERIVED: a terminal event that DEGRADED onto the caller's transaction is
    invisible to the fresh connection, so reading only there reintroduced the same mislabelling in
    mirror image. ``_durable_read`` therefore derives over the UNION of the durable and caller
    connections — still exclusively from event ABSENCE, so the union can only ever move a run from
    ``"incomplete"`` toward the terminal event some connection really holds, never manufacture one."""
    row = _durable_read(
        conn, _SELECT_TERMINAL,
        (run_id, TraceEventKind.COMPLETED.value, TraceEventKind.FAILED.value),
        what=f"run_status {run_id}")
    if row is None:
        return "incomplete"
    return "completed" if row[0] == TraceEventKind.COMPLETED.value else "failed"


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    """The single terminal event of a run, WITH the record that makes it tamper-evident.

    ``payload`` is the read-back canonical redacted metadata (a JSON object of JSON-primitive
    values) and ``payload_hash`` is the sha256 over its RFC 8785 (JCS) bytes that ``append_event``
    computed at write time. This type deliberately does NOT verify one against the other: the row is
    physically immutable (migration 1020), so a disagreement means the stored bytes were altered out
    of band, and deciding what to do about that belongs to the caller that is trusting the record —
    see ``featuregen.materialize.admission`` (spec §1.2 check 2), which refuses with
    ``TERMINAL_PAYLOAD_TAMPERED``. A reader that silently dropped a mismatching event would report
    the run as INCOMPLETE and hide the tampering instead of surfacing it.

    ``payload`` is exposed as a read-only mapping so a caller cannot mutate the record it is about
    to hash. ``featuregen.formula._jcs`` dispatches on ``isinstance(obj, dict)``, so a hasher takes
    ``dict(payload)`` (which ``materialize.canonical.materialize_hash`` already does)."""

    kind: TraceEventKind
    payload: Mapping[str, Any]
    payload_hash: str


def read_terminal_event(conn: DbConn, run_id: str) -> TerminalEvent | None:
    """The run's terminal (``COMPLETED``/``FAILED``) event, or ``None`` when it has none.

    The READ counterpart of the single terminal ``append_event`` writes, and the evidence any
    consumer of an authoring outcome must verify against: an ``AuthoringResult`` is a publicly
    constructible frozen dataclass, so only this immutable, ``payload_hash``-protected row can say
    what a run actually decided.

    ⚠️ ``kind`` ALONE ANSWERS ALMOST NOTHING. ``authoring._TERMINAL_FOR_DISPOSITION`` maps only
    ``TECHNICAL_FAILURE`` to ``FAILED``, so a ``REJECTED`` or ``UNSUPPORTED`` run also writes
    ``COMPLETED``. "A COMPLETED event exists" therefore does NOT mean the run resolved — read
    ``payload["authoring_disposition"]``.

    Goes through ``_durable_read`` for exactly the reasons ``run_status`` does: the events are
    committed on the durable fresh connection, and the union over both connections also surfaces one
    that DEGRADED onto the caller's uncommitted transaction. A reader that missed a just-committed
    terminal event would report a complete run as incomplete."""
    row = _durable_read(
        conn, _SELECT_TERMINAL_EVENT,
        (run_id, TraceEventKind.COMPLETED.value, TraceEventKind.FAILED.value),
        what=f"read_terminal_event {run_id}")
    if row is None:
        return None
    kind, payload, payload_hash = row
    return TerminalEvent(
        kind=TraceEventKind(kind),
        # 1020 CHECKs `jsonb_typeof(payload) = 'object'`, so psycopg's jsonb loader always hands
        # back a dict here; the copy is what makes the frozen record's mapping non-aliasing.
        payload=MappingProxyType(dict(payload)),
        payload_hash=payload_hash,
    )


def read_run_intent_hash(conn: DbConn, run_id: str) -> str | None:
    """The ``intent_hash`` stamped on the run's MANIFEST, or ``None`` when no manifest is visible.

    The manifest is written FIRST, before any provider call, and is write-once — so this is the
    immutable record of WHAT was asked, against which a caller re-hashing its own
    ``AuthoringIntent`` (``authoring.authoring_intent_hash``) can prove it holds the intent this run
    was actually opened for. Same ``_durable_read`` visibility semantics as
    :func:`read_terminal_event`; ``None`` is the honest ABSENCE and callers must fail closed on it
    rather than treating it as a match."""
    row = _durable_read(
        conn, _SELECT_RUN_INTENT_HASH, (run_id,), what=f"read_run_intent_hash {run_id}")
    return None if row is None else str(row[0])


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


def _bounded_connect_dsn(dsn: str) -> str:
    """``dsn`` with a DEFAULT ``connect_timeout`` merged in, so no durable connect can hang forever.

    The DSN's own value WINS (it is spread second), so this only supplies the bound libpq otherwise
    leaves unset — an operator who has tuned ``connect_timeout`` **in the DSN** keeps it, and the
    password the DSN carries survives the round-trip in both keyword and URI form.

    SCOPE, precisely: ``conninfo_to_dict`` parses the DSN STRING ONLY — it does not expand libpq's
    environment defaults. So an operator who tuned the timeout through ``PGCONNECT_TIMEOUT`` rather
    than the DSN does NOT keep it: this default is materialized into the conninfo, and an explicit
    conninfo parameter outranks the environment in libpq. That is the deliberate trade — an
    unbounded connect on this path hangs a request, and 5s is a far better failure than forever —
    but it IS an override of that env var, not merely a fallback, and an operator who needs a
    different bound here must set it in ``FEATUREGEN_DSN``."""
    return make_conninfo(
        "", **{"connect_timeout": _CONNECT_TIMEOUT_SECONDS, **conninfo_to_dict(dsn)})


def _durable_write(conn: DbConn, sql: str, params: tuple, *, replay_sql: str, what: str) -> None:
    """Perform ONE bare INSERT on a FRESH connection from ``get_settings().dsn``, committed
    independently of the caller's transaction (module docstring for why), under a bounded
    ``lock_timeout``.

    No advisory lock is ever taken (program-audit I-3: a fresh connection re-acquiring the request's
    advisory lock hangs forever). That is NOT the same as "cannot self-block", which this docstring
    used to claim: the fresh connection can still wait on ROW/INDEX contention owned by the CALLER's
    own uncommitted transaction — a degraded event holds 1020's (run, seq) and partial-terminal unique
    index entries, and a later durable INSERT for the same run blocked on them until the request
    ended, i.e. never. ``_SET_LOCK_TIMEOUT`` bounds that into ``LockNotAvailable``, which degrades
    like any other connection-dependent failure instead of hanging the request.

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

    The degrade path replays ``replay_sql`` (the idempotent variant — commit ambiguity, see
    ``_REPLAY_RUN``) through ``_replay_on_caller``, never bare: it is replaying a statement the
    database may already have REJECTED — or already ACCEPTED — once, and neither may cost the caller
    its transaction."""
    dsn = get_settings().dsn
    if not dsn:
        conn.execute(sql, params)   # the designed tests / no-DB path, NOT a degradation
        return
    try:
        # BOTH waits are bounded: the CONNECT by `_CONNECT_TIMEOUT_SECONDS`, the INSERT's lock by
        # `_SET_LOCK_TIMEOUT`. Either expiry is an `OperationalError`, i.e. connection-dependent,
        # i.e. the fallback replay — never a hung request.
        with psycopg.connect(_bounded_connect_dsn(dsn)) as trace_conn:  # committed on clean exit
            trace_conn.execute(_SET_LOCK_TIMEOUT)
            trace_conn.execute(sql, params)
        return
    except _DETERMINISTIC_REJECTIONS:
        raise
    except Exception:  # noqa: BLE001 — a connection-dependent failure must not lose the trace
        logger.exception(
            "durable authoring-trace write failed (%s); falling back to the request connection",
            what)
    _replay_on_caller(conn, replay_sql, params, what=what)


def _replay_on_caller(conn: DbConn, sql: str, params: tuple, *, what: str) -> None:
    """Replay a degraded durable write on the CALLER's connection WITHOUT poisoning it.

    The fallback deliberately replays statements the database has ALREADY REJECTED once — the FK
    class is the whole point of it (``_durable_write``) — or ALREADY ACCEPTED, when a commit ack was
    lost (``_REPLAY_RUN``, which is why ``sql`` here is the idempotent variant). Either way it MUST
    leave the caller's transaction exactly as usable as it found it.

    A bare ``conn.execute`` did not: an FK the caller's connection
    also refuses aborted the request transaction, so the orchestrator lost every uncommitted thing it
    had done and could not execute another statement — not even its own terminal ``FAILED`` event
    through this very degraded path. Precedent for the shape: ``overlay.upload.enrich`` (program-audit
    finding I-2). A savepoint takes no advisory lock, so the I-3 self-deadlock does not apply.

    THREE shapes over the FIVE libpq transaction statuses — ``conn.transaction()`` is not a savepoint
    when there is no transaction, and it is not usable at all once one has aborted. The enumeration
    below is CLOSED (``IDLE``, ``ACTIVE``, ``INTRANS``, ``INERROR``, ``UNKNOWN``): every status has a
    named home, so none can fall into a branch by accident.

    * ``INTRANS`` (the healthy request shape) and ``ACTIVE`` → SAVEPOINT. The rejection is contained
      and the caller's uncommitted work — including trace rows that degraded here earlier, the only
      copy there is — survives untouched. A blanket ``rollback()`` would keep the connection usable
      by destroying exactly the evidence this module exists to preserve.

      ``ACTIVE`` means a statement is in flight on this connection, which this module never produces
      itself (it is single-threaded on the caller's connection by contract — see the module
      docstring) but which a caller sharing a connection across threads could. It is routed to the
      SAVEPOINT branch because that is the only branch that resolves the status SAFELY: psycopg's
      ``Transaction.__enter__`` takes ``conn.lock`` and re-reads ``transaction_status`` underneath it,
      so it sees the TRUE post-statement state (``INTRANS`` → ``SAVEPOINT``; ``IDLE`` → ``BEGIN``)
      rather than the racing snapshot read above. The bare-execute branch has no such re-read: it
      would blindly replay against whatever state the other statement left behind.
    * ``IDLE`` → replay bare. ``conn.transaction()`` would BEGIN *and COMMIT* here, committing a
      degraded row whose whole point is to share the request's fate (and breaking the caller's
      control of its own transaction boundary). Nothing of the caller's is in flight, so if the
      replay is rejected the implicit single-statement transaction is discarded — that loses nothing
      and keeps the connection usable.
    * ``INERROR`` (the caller's transaction ALREADY aborted, on its own earlier statement) and
      ``UNKNOWN`` (the connection is bad — libpq cannot report a status at all) → replay
      bare, and let ``InFailedSqlTransaction`` (or the connection's own error) propagate. Nothing can
      be written on an aborted or broken transaction and this module must not pretend otherwise.
      Taking the savepoint branch here
      BRICKED the connection: psycopg's ``Transaction.__enter__`` increments
      ``conn._num_transactions`` BEFORE issuing ``SAVEPOINT``, that ``SAVEPOINT`` fails,
      ``__enter__`` raises, ``__exit__`` never runs, and the counter LEAKS at 1 permanently — after
      which ``conn.rollback()``/``conn.commit()`` raise ``ProgrammingError("Explicit rollback()
      forbidden within a Transaction context")`` and the connection is unrecoverable in-process.
      That is reachable in exactly the shape Task 12 produces (an orchestrator recording its
      terminal ``FAILED`` *because* its own SQL failed, while the durable connection blips), and
      ``api.deps.get_feature_gen_conn``'s ``except Exception: conn.rollback(); raise`` would then
      have MASKED the request's real error with that ``ProgrammingError``. No ``rollback()`` here
      either: that would discard the caller's uncommitted trace evidence, which is the one thing
      this function may never do.

    OBSERVABLE, whichever way the ``ON CONFLICT DO NOTHING`` lands. ``rowcount == 0`` means the
    arbiter matched an existing row and this replay wrote nothing — the DESIGNED convergence on a
    lost commit ack, but also, indistinguishably from here, a genuinely DIFFERENT event whose
    ``idempotency_key`` was reused and which is therefore silently dropped while ``append_event``
    still returns a freshly minted id for a row that exists nowhere. The accepted behaviour is
    unchanged (non-raising, failing toward ``"incomplete"``); it is merely no longer INVISIBLE."""
    status = conn.info.transaction_status
    if status in (psycopg.pq.TransactionStatus.INTRANS, psycopg.pq.TransactionStatus.ACTIVE):
        with conn.transaction():   # savepoint: contain a rejected replay without poisoning the txn
            cur = conn.execute(sql, params)
    elif status == psycopg.pq.TransactionStatus.IDLE:
        try:
            cur = conn.execute(sql, params)
        except Exception:
            conn.rollback()   # discards ONLY the rejected statement's implicit tx — nothing else
            raise
    else:
        # INERROR / UNKNOWN: bare, so psycopg's savepoint stack can't leak.
        cur = conn.execute(sql, params)
    if cur.rowcount == 0:
        logger.warning(
            "degraded authoring-trace replay of %s wrote NOTHING: the ON CONFLICT arbiter matched a "
            "row that already exists. Either this CONVERGED on the durable row of a lost commit ack "
            "(the designed outcome), or a DIFFERENT event reused this replay key and THIS one was "
            "dropped. The id returned to the caller then names no row anywhere; run_status stays "
            "absence-derived and can only read the run as 'incomplete'", what)


def _durable_read(conn: DbConn, sql: str, params: tuple, *, what: str) -> tuple | None:
    """The READ mirror of ``_durable_write``: one row, on a FRESH connection from
    ``get_settings().dsn`` so it SEES what the durable writes committed (``run_status`` for the
    isolation trap this closes). Read-only and single-statement, so there is nothing to commit and
    nothing to poison; a fresh connection cannot deadlock (no advisory lock, no write).

    UNION OVER BOTH CONNECTIONS, and that is load-bearing: the fresh connection is blind to any row
    a durable write DEGRADED onto the caller's uncommitted transaction, so reading only there reported
    a run as terminal-less while the caller itself held its terminal event. So the caller's connection
    is read too — whenever the durable read FAILS (where a degraded write's rows are the only copy
    anyway, so a connect blip can never turn a read into a request-failing exception) AND whenever it
    simply finds NOTHING. First row wins; both empty is the honest ABSENCE. The union is monotone in
    evidence — it can only surface a terminal event that one of the two connections really holds, so
    it can never manufacture a false ``"completed"``.

    Nothing here is deterministic-rejection territory: a SELECT of committed rows either works or the
    connection is unusable. The caller-connection read is guarded too, because the caller's
    transaction may ALREADY be aborted (an earlier failed statement of its own) — a read contracted to
    "never fail the caller" may not raise ``InFailedSqlTransaction``; absence of evidence is the safe
    answer.

    COST OF THE UNION, accepted, and BOTH halves of it are side effects on the caller's connection —
    reached on EVERY terminal-less run, which is every run until it closes:

    * under ``REPEATABLE READ`` the extra read may PIN the caller's snapshot slightly earlier than
      before;
    * on a non-autocommit connection sitting ``IDLE``, psycopg issues an implicit ``BEGIN`` for it —
      so ``run_status``, a PURE READ, leaves the caller ``INTRANS`` where it found it idle. Harmless
      for the request-scoped connections this module is hosted on (``api.deps.get_feature_gen_conn``
      commits or rolls back at the end of the request either way), but it is a real state change and
      a caller that reasons about its own transaction boundary must know about it."""
    dsn = get_settings().dsn
    if dsn:
        try:
            with psycopg.connect(_bounded_connect_dsn(dsn)) as trace_conn:   # bounded connect too
                row = trace_conn.execute(sql, params).fetchone()
            if row is not None:
                return row
        except Exception:  # noqa: BLE001 — a trace-read failure must never fail the caller
            logger.exception(
                "durable authoring-trace read failed (%s); falling back to the request connection",
                what)
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001 — ditto: an aborted caller tx must not surface from a read
        logger.exception(
            "request-connection authoring-trace read failed (%s); deriving from ABSENCE", what)
        return None

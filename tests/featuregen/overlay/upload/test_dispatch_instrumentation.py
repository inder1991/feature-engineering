"""Delivery C5 Task 3 — driver instrumentation: the ``AuditingClient`` wrapper at the dispatch seam.

``drive_structured_call`` re-invokes ``client.call`` for every repair/retry attempt, so wrapping
the client is the ONE seam that sees every PHYSICAL provider request. Under test here:

  1. an ingestion call (``dispatch_audit`` ctx present) writes one ``llm_dispatch`` header +
     subject attribution + a ``response_received`` outcome, committed on the writer's OWN
     connection (visible to a fresh connection, surviving the request rollback);
  2. a transient-then-ok script audits BOTH physical attempts (attempt_no 1 and 2, one shared
     logical_call_ref) — every provider request is pre-audited, not just the first;
  3. FAIL-CLOSED: ``AuditUnavailable`` means the provider is NEVER called; the wrapper returns the
     exact signal a real pre-response transport failure produces today (``ClaudeLLM.call`` maps
     ``anthropic.APIConnectionError`` to a RETURNED ``PROVIDER_TRANSIENT`` result, never a raise),
     so the driver bounded-retries and the call fails into None with zero egress;
  4. ``dispatch_audit=None`` (contract authoring / feature generation) is byte-identical to
     today — the client is used untouched and no dispatch rows exist.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.intake.llm import (
    PROVIDER_OK,
    PROVIDER_TRANSIENT,
    FakeLLM,
    FakeResponse,
    LLMResult,
    compute_input_hash,
)
from featuregen.overlay.upload import dispatch_audit as dispatch_audit_module
from featuregen.overlay.upload.dispatch_audit import AuditUnavailable, DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import (
    audited_structured_call,
    register_enrichment_schemas,
)

_META = {"table": "accounts", "column": "balance", "type": "numeric"}
_RUN_ID = "ingrun_c5t3_test"
_SUBJECTS = [
    {"catalog_source": "deposits", "object_ref": "public.accounts", "logical_ref": "accounts",
     "field_names": ["balance"]},
]


def _ctx(run_id: str | None = _RUN_ID) -> DispatchAuditContext:
    return DispatchAuditContext(ingestion_run_id=run_id, stage="enrichment", subjects=_SUBJECTS)


def _call(db, client, *, task: str, ctx: DispatchAuditContext | None):
    register_enrichment_schemas(db)
    return audited_structured_call(
        db, client, task=task, prompt_id="overlay_concept_v1", schema_id="overlay_concept",
        catalog_metadata=_META, instruction="Classify the concept of this column.",
        dispatch_audit=ctx)


@pytest.fixture
def durable_dsn(monkeypatch, _dsn):
    """Point FEATUREGEN_DSN at the test cluster so the own-connection dispatch writes really
    commit, and durably create the ingestion_run row the dispatch header FK-references. Cleanup
    removes everything committed OUTSIDE the rolled-back request tx (mirror of
    test_dispatch_audit's fixture): outcome → subject → dispatch (write-once triggers dropped
    just long enough), the durable llm_call rows the DSN flips on, then the run row itself."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
            "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'c5t3-test', "
            "'in_progress', now(), now()) ON CONFLICT (id) DO NOTHING", (_RUN_ID,))
    yield _RUN_ID
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "DISABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch DISABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("DELETE FROM llm_dispatch_outcome WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)",
                  (_RUN_ID,))
        c.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)",
                  (_RUN_ID,))
        c.execute("DELETE FROM llm_dispatch WHERE ingestion_run_id = %s", (_RUN_ID,))
        c.execute("ALTER TABLE llm_dispatch ENABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "ENABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM llm_call WHERE task LIKE %s", ("test.c5t3.%",))
        c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM ingestion_run WHERE id = %s", (_RUN_ID,))


# ── 1. the successful ingestion call: header + subjects + outcome, durably committed ─────────────


def test_ingestion_call_audits_dispatch_subjects_and_outcome(db, durable_dsn, _dsn) -> None:
    task = "test.c5t3.success"
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    out = _call(db, client, task=task, ctx=_ctx())
    assert out == {"concept": "monetary_amount"}
    with psycopg.connect(_dsn) as fresh:   # fresh conn: the audit must ALREADY be committed
        headers = fresh.execute(
            "SELECT dispatch_ref, logical_call_ref, attempt_no, ingestion_run_id, stage, "
            "input_hash, redacted_input, provider, model, prompt_version, schema_version "
            "FROM llm_dispatch WHERE task = %s", (task,)).fetchall()
        assert len(headers) == 1
        (ref, logical_ref, attempt_no, run_id, stage, input_hash, redacted_input,
         provider, model, prompt_version, schema_version) = headers[0]
        subjects = fresh.execute(
            "SELECT catalog_source, object_ref, logical_ref, field_names "
            "FROM llm_dispatch_subject WHERE dispatch_ref = %s", (ref,)).fetchall()
        outcomes = fresh.execute(
            "SELECT outcome FROM llm_dispatch_outcome WHERE dispatch_ref = %s", (ref,)).fetchall()
    assert attempt_no == 1
    assert logical_ref.startswith("lc_")                # minted once per logical call
    assert run_id == durable_dsn                        # linked to the ctx's ingestion run
    assert stage == "enrichment"
    assert (provider, model) == ("fake", "test")        # the request's pinned generation settings
    assert (prompt_version, schema_version) == (1, 1)
    # the stored redacted_input IS the egressed request.inputs — its recorded hash round-trips
    assert input_hash == compute_input_hash(redacted_input)
    assert subjects == [("deposits", "public.accounts", "accounts", ["balance"])]
    assert outcomes == [("response_received",)]


# ── 2. every PHYSICAL attempt is audited (retry/repair re-calls included) ────────────────────────


def test_each_physical_retry_attempt_is_audited(db, durable_dsn, _dsn) -> None:
    task = "test.c5t3.retry"
    client = FakeLLM(script={task: [
        FakeResponse(output={}, provider_status=PROVIDER_TRANSIENT),   # attempt 1 → bounded retry
        FakeResponse(output={"concept": "monetary_amount"}),           # attempt 2 → ok
    ]})
    out = _call(db, client, task=task, ctx=_ctx())
    assert out == {"concept": "monetary_amount"}
    with psycopg.connect(_dsn) as fresh:
        rows = fresh.execute(
            "SELECT dispatch_ref, logical_call_ref, attempt_no FROM llm_dispatch "
            "WHERE task = %s ORDER BY attempt_no", (task,)).fetchall()
        assert [r[2] for r in rows] == [1, 2]           # BOTH physical attempts pre-audited
        assert rows[0][1] == rows[1][1]                 # ...sharing ONE logical_call_ref
        for ref, _logical_ref, _attempt_no in rows:
            outcomes = fresh.execute(
                "SELECT outcome FROM llm_dispatch_outcome WHERE dispatch_ref = %s",
                (ref,)).fetchall()
            # the inner client RETURNED a result both times (a transient status is a returned
            # single-shot outcome, exactly like ClaudeLLM's APIConnectionError mapping)
            assert outcomes == [("response_received",)]


# ── 3. fail-closed: AuditUnavailable ⟹ the provider is NEVER called ─────────────────────────────


def test_audit_unavailable_fails_closed_with_no_egress(db, monkeypatch) -> None:
    """When the pre-dispatch audit cannot be durably committed the provider must NOT be called.
    The wrapper surfaces the same signal as a real pre-response transport failure (a RETURNED
    PROVIDER_TRANSIENT result — never a raise), so drive_structured_call bounded-retries (each
    retry re-attempts the audit, all blocked here) and fails into STATUS_FAILED →
    audited_structured_call returns None and caches nothing."""
    calls: list = []

    class _Provider:
        def call(self, request):
            calls.append(request)
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    def _unavailable(**_kwargs):
        raise AuditUnavailable("audit store down (test)")

    monkeypatch.setattr(dispatch_audit_module, "record_dispatch", _unavailable)
    out = _call(db, _Provider(), task="test.c5t3.failclosed", ctx=_ctx())
    assert out is None      # failed into clarification — nothing cached
    assert calls == []      # the provider was NEVER invoked — zero egress happened


# ── 4. dispatch_audit=None is byte-identical to today ───────────────────────────────────────────


def test_none_ctx_is_byte_identical_and_writes_no_dispatch_rows(db, monkeypatch) -> None:
    """A non-ingestion call (contract authoring / feature generation — no ctx threaded) behaves
    exactly as before: the client is used untouched, the validated output comes back, the
    immutable llm_call is still recorded, and NOT ONE llm_dispatch row is written."""
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    task = "test.c5t3.none"
    before = db.execute("SELECT count(*) FROM llm_dispatch").fetchone()[0]
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    out = _call(db, client, task=task, ctx=None)
    assert out == {"concept": "monetary_amount"}        # the call still returns its output
    after = db.execute("SELECT count(*) FROM llm_dispatch").fetchone()[0]
    assert after == before                              # no dispatch audit rows at all
    n = db.execute("SELECT count(*) FROM llm_call WHERE task = %s", (task,)).fetchone()[0]
    assert n == 1                                       # the llm_call audit behaves as before

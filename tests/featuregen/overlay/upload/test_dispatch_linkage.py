"""Delivery C5 Task 4 — logical-call linkage: llm_call ↔ dispatch(es) ↔ ingestion run.

C5-T2/T3 audit every PHYSICAL provider attempt (llm_dispatch + subjects + outcome) and C5's
durable recorder writes the LOGICAL ``llm_call`` — but until this task nothing associated them.
Under test here, ``audited_structured_call`` with a ``DispatchAuditContext`` now (record → link →
return, the eligibility ordering):

  1. links the recorded llm_call_ref to its dispatch_ref (``llm_call_dispatch``) and to its
     ingestion run + stage (``ingestion_run_llm_call``), committed on the writer's OWN connection
     (visible to a fresh connection) BEFORE the output is returned as cache/evidence-eligible;
  2. a transient-then-ok script links the ONE logical call to BOTH physical dispatch_refs —
     every attempt stays attributable to the final logical call;
  3. the C5 attributability point: FROM a bare dispatch_ref, joining llm_call_dispatch →
     ingestion_run_llm_call → llm_dispatch_subject answers "which run + which object subjects
     produced this llm_call";
  4. ``dispatch_audit=None`` (contract authoring / feature generation) is byte-identical to
     today — output returned, llm_call recorded, ZERO association rows.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.intake.llm import PROVIDER_TRANSIENT, FakeLLM, FakeResponse
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import (
    audited_structured_call,
    register_enrichment_schemas,
)

_META = {"table": "accounts", "column": "balance", "type": "numeric"}
_RUN_ID = "ingrun_c5t4_test"
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
    """Point FEATUREGEN_DSN at the test cluster so the own-connection dispatch/llm_call/link
    writes really commit, and durably create the ingestion_run row the audit rows FK-reference.
    Cleanup removes everything committed OUTSIDE the rolled-back request tx (mirror of the C5-T3
    fixture, extended for the T4 association tables): associations FIRST (they FK both sides),
    then outcome → subject → dispatch (write-once triggers dropped just long enough), the durable
    llm_call rows the DSN flips on, then the run row itself."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
            "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'c5t4-test', "
            "'in_progress', now(), now()) ON CONFLICT (id) DO NOTHING", (_RUN_ID,))
    yield _RUN_ID
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("DELETE FROM llm_call_dispatch WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s) "
                  "OR llm_call_ref IN (SELECT llm_call_ref FROM llm_call WHERE task LIKE %s)",
                  (_RUN_ID, "test.c5t4.%"))
        c.execute("DELETE FROM ingestion_run_llm_call WHERE ingestion_run_id = %s", (_RUN_ID,))
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "DISABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_outcome "
                  "DISABLE TRIGGER llm_dispatch_outcome_no_mutation")
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
        c.execute("ALTER TABLE llm_dispatch_outcome "
                  "ENABLE TRIGGER llm_dispatch_outcome_no_mutation")
        c.execute("ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM llm_call WHERE task LIKE %s", ("test.c5t4.%",))
        c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM ingestion_run WHERE id = %s", (_RUN_ID,))


def _linked_call_ref(fresh, task: str) -> str:
    rows = fresh.execute(
        "SELECT llm_call_ref FROM llm_call WHERE task = %s", (task,)).fetchall()
    assert len(rows) == 1                               # ONE logical call was recorded
    return rows[0][0]


# ── 1. success: llm_call ↔ dispatch + llm_call ↔ run associations, durably committed ────────────


def test_linkage_rows_written_for_ingestion_call(db, durable_dsn, _dsn) -> None:
    task = "test.c5t4.success"
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    out = _call(db, client, task=task, ctx=_ctx())
    assert out == {"concept": "monetary_amount"}
    with psycopg.connect(_dsn) as fresh:   # fresh conn: the links must ALREADY be committed
        llm_call_ref = _linked_call_ref(fresh, task)
        dispatch_refs = [r[0] for r in fresh.execute(
            "SELECT dispatch_ref FROM llm_dispatch WHERE task = %s", (task,)).fetchall()]
        assert len(dispatch_refs) == 1
        links = fresh.execute(
            "SELECT dispatch_ref FROM llm_call_dispatch WHERE llm_call_ref = %s",
            (llm_call_ref,)).fetchall()
        run_links = fresh.execute(
            "SELECT ingestion_run_id, stage FROM ingestion_run_llm_call "
            "WHERE llm_call_ref = %s", (llm_call_ref,)).fetchall()
    assert links == [(dispatch_refs[0],)]               # llm_call ↔ its ONE physical dispatch
    assert run_links == [(durable_dsn, "enrichment")]   # llm_call ↔ its run, right stage


# ── 2. a retried logical call links to BOTH dispatch_refs ────────────────────────────────────────


def test_both_retry_attempts_link_to_the_one_llm_call(db, durable_dsn, _dsn) -> None:
    task = "test.c5t4.retry"
    client = FakeLLM(script={task: [
        FakeResponse(output={}, provider_status=PROVIDER_TRANSIENT),   # attempt 1 → bounded retry
        FakeResponse(output={"concept": "monetary_amount"}),           # attempt 2 → ok
    ]})
    out = _call(db, client, task=task, ctx=_ctx())
    assert out == {"concept": "monetary_amount"}
    with psycopg.connect(_dsn) as fresh:
        llm_call_ref = _linked_call_ref(fresh, task)
        dispatch_refs = [r[0] for r in fresh.execute(
            "SELECT dispatch_ref FROM llm_dispatch WHERE task = %s ORDER BY attempt_no",
            (task,)).fetchall()]
        assert len(dispatch_refs) == 2                  # both physical attempts audited (T3)
        linked = {r[0] for r in fresh.execute(
            "SELECT dispatch_ref FROM llm_call_dispatch WHERE llm_call_ref = %s",
            (llm_call_ref,)).fetchall()}
    # attempts 1 AND 2 are both attributable to the final logical call
    assert linked == set(dispatch_refs)


# ── 3. the C5 attributability point: dispatch_ref → run + object subjects ────────────────────────


def test_dispatch_ref_resolves_run_and_subjects(db, durable_dsn, _dsn) -> None:
    """Given ONLY a dispatch_ref (e.g. from an egress log), the associations answer the audit
    question: WHICH ingestion run and WHICH catalog-object subjects produced this llm_call."""
    task = "test.c5t4.attribution"
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    assert _call(db, client, task=task, ctx=_ctx()) is not None
    with psycopg.connect(_dsn) as fresh:
        dispatch_ref = fresh.execute(
            "SELECT dispatch_ref FROM llm_dispatch WHERE task = %s", (task,)).fetchone()[0]
        rows = fresh.execute(
            "SELECT irlc.ingestion_run_id, irlc.stage, lcd.llm_call_ref, "
            "       s.catalog_source, s.object_ref, s.logical_ref, s.field_names "
            "FROM llm_call_dispatch lcd "
            "JOIN ingestion_run_llm_call irlc ON irlc.llm_call_ref = lcd.llm_call_ref "
            "JOIN llm_dispatch_subject s ON s.dispatch_ref = lcd.dispatch_ref "
            "WHERE lcd.dispatch_ref = %s", (dispatch_ref,)).fetchall()
        llm_call_ref = _linked_call_ref(fresh, task)
    assert len(rows) == 1
    run_id, stage, linked_call_ref, catalog_source, object_ref, logical_ref, field_names = rows[0]
    assert (run_id, stage) == (durable_dsn, "enrichment")     # the run that made the call
    assert linked_call_ref == llm_call_ref                    # via the recorded logical call
    assert (catalog_source, object_ref, logical_ref, field_names) == (
        "deposits", "public.accounts", "accounts", ["balance"])   # the object subjects


# ── 4. dispatch_audit=None is byte-identical: no association rows at all ─────────────────────────


def test_none_ctx_writes_no_association_rows(db, monkeypatch) -> None:
    """A non-ingestion call (no ctx threaded) behaves exactly as before: the output comes back,
    the immutable llm_call is still recorded, and NOT ONE llm_call_dispatch /
    ingestion_run_llm_call row is written."""
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    task = "test.c5t4.none"
    before_links = db.execute("SELECT count(*) FROM llm_call_dispatch").fetchone()[0]
    before_runs = db.execute("SELECT count(*) FROM ingestion_run_llm_call").fetchone()[0]
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    out = _call(db, client, task=task, ctx=None)
    assert out == {"concept": "monetary_amount"}        # the call still returns its output
    assert db.execute("SELECT count(*) FROM llm_call_dispatch").fetchone()[0] == before_links
    assert db.execute("SELECT count(*) FROM ingestion_run_llm_call").fetchone()[0] == before_runs
    n = db.execute("SELECT count(*) FROM llm_call WHERE task = %s", (task,)).fetchone()[0]
    assert n == 1                                       # the llm_call audit behaves as before

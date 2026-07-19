"""Delivery C5 Task 6 — the acceptance GATE + two correctness pieces.

C5-T1..T5 built the pre-dispatch audit machinery and wired it through the two audited-call seams
into Pass A/B ingestion. This suite closes C5 with three proofs:

  A. ``dispatch_egress_status`` classifies a dispatch's egress from its committed outcome, and — the
     load-bearing case — reports ``egress_outcome_unknown`` for a dispatch whose immutable
     pre-dispatch header exists but whose outcome row is missing (a crash between record_dispatch and
     the outcome write). Never ``not sent``: the header proves egress was AUTHORIZED and may have gone.

  B. FAIL-CLOSED end to end through a REAL ingest: with ``record_dispatch`` raising
     ``AuditUnavailable``, the provider is NEVER egressed, yet core ingestion still COMPLETES
     (fail-soft — the upload is not aborted, the graph persists) and nothing is cached from a
     non-existent response.

  C. Eligibility-ordering DISCARD: a provider result is not eligible for cache/evidence until the
     logical outcome audit (``link_llm_call``) committed. With a ``dispatch_audit`` context and a
     failed link, ``audited_structured_call`` returns None (discarded) and ``audited_batch_call``
     marks every included ref MISSING; with ``dispatch_audit=None`` the link is never called and the
     result is returned byte-identically.

Durable own-connection writes (the eligibility-discard path still RECORDS the dispatch + llm_call
before it discards, and the fail-closed ingest links its failed calls to the run) are cleaned up by
task / run id, mirroring the C5-T2..T5 fixtures (the C5-T4 fixture's drain of
llm_call_dispatch + ingestion_run_llm_call before llm_dispatch for FK order).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload import dispatch_audit as dispatch_audit_module
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload import enrich_llm
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.dispatch_audit import (
    AuditUnavailable,
    DispatchAuditContext,
    dispatch_egress_status,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_llm import (
    audited_batch_call,
    audited_structured_call,
    register_enrichment_schemas,
)
from featuregen.overlay.upload.ingest import ingest_upload

_META = {"table": "accounts", "column": "balance", "type": "numeric"}
_SUBJECTS = [
    {"catalog_source": "deposits", "object_ref": "public.accounts", "logical_ref": "accounts",
     "field_names": ["balance"]},
]


# ── A. dispatch_egress_status — the egress_outcome_unknown classifier ─────────────────────────────


def _insert_dispatch(conn, dispatch_ref: str, task: str = "test.c5t6.egress") -> None:
    """Insert one immutable pre-dispatch header on the PASSED (rolled-back) connection. The
    write-once trigger only guards UPDATE/DELETE, so the INSERT is fine and teardown's tx rollback
    (not a DELETE) removes it — no own-connection commit, no cleanup needed."""
    conn.execute(
        "INSERT INTO llm_dispatch (dispatch_ref, logical_call_ref, attempt_no, stage, task, "
        "input_hash, redacted_input) VALUES (%s, %s, 1, 'enrichment', %s, 'sha256:x', '{}'::jsonb)",
        (dispatch_ref, dispatch_ref + "_lc", task))


def test_egress_status_response_received(db) -> None:
    _insert_dispatch(db, "disp_c5t6_ok")
    db.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, %s)",
               ("disp_c5t6_ok", "response_received"))
    assert dispatch_egress_status(db, "disp_c5t6_ok") == "response_received"


def test_egress_status_transport_failed(db) -> None:
    _insert_dispatch(db, "disp_c5t6_fail")
    db.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, %s)",
               ("disp_c5t6_fail", "transport_failed"))
    assert dispatch_egress_status(db, "disp_c5t6_fail") == "transport_failed"


def test_egress_status_unknown_when_header_but_no_outcome(db) -> None:
    """The load-bearing case: a pre-dispatch header with NO outcome row (a crash between
    record_dispatch and the outcome write). It is ``egress_outcome_unknown`` — never ``not sent`` —
    because the immutable header proves egress was AUTHORIZED and may have occurred."""
    _insert_dispatch(db, "disp_c5t6_unknown")
    assert dispatch_egress_status(db, "disp_c5t6_unknown") == "egress_outcome_unknown"


def test_egress_status_latest_outcome_wins(db) -> None:
    """Outcomes are append-only (no UNIQUE on dispatch_ref): the LATEST row wins."""
    _insert_dispatch(db, "disp_c5t6_latest")
    db.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome, recorded_at) "
               "VALUES (%s, %s, now() - interval '1 minute')",
               ("disp_c5t6_latest", "transport_failed"))
    db.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, %s)",
               ("disp_c5t6_latest", "response_received"))
    assert dispatch_egress_status(db, "disp_c5t6_latest") == "response_received"


# ── B. AUDIT_UNAVAILABLE fail-closed, end to end through a REAL ingest ────────────────────────────

_B_RUN_ID = "ingrun_c5t6_failclosed"
_B_SOURCE = "c5t6src"
_NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _rows() -> list[CanonicalRow]:
    return [
        CanonicalRow(_B_SOURCE, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(_B_SOURCE, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(_B_SOURCE, "accounts", "balance", "numeric"),
    ]


def _script(rows: list[CanonicalRow]) -> FakeLLM:
    """A fully valid Pass A batch script — deliberately never reached (the inner provider must not
    be egressed once record_dispatch fails closed)."""
    hashes = [content_hash(r) for r in rows]
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": "unclassified"} for h in hashes]}),
        "overlay.enrich.definition": FakeResponse(output={"results": [
            {"ref": h, "definition": "A governed banking column."} for h in hashes]}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": "accounts", "domain": "deposits"}]}),
    })


class _CountingClient:
    """Wraps an LLMClient, counting every physical ``call``. Fail-closed pre-dispatch audit
    short-circuits inside ``AuditingClient`` BEFORE the inner client, so ``calls`` stays 0 —
    the proof that NO egress happened."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def call(self, request):
        self.calls += 1
        return self._inner.call(request)


@pytest.fixture
def durable_run(monkeypatch, _dsn, db):
    """Point FEATUREGEN_DSN at the test cluster so the own-connection llm_call + run-link writes
    really commit, and durably create the ingestion_run row they FK-reference. Cleanup first rolls
    the REQUEST tx back (its uncommitted ingestion_run_object/_fact children would block the run-row
    delete), then removes everything committed outside it — associations first, then any dispatch
    rows (none here — record_dispatch fails closed), the durable llm_call rows, then the run row.
    Mirrors the C5-T5 e2e fixture."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
            "started_at, heartbeat_at) VALUES (%s, 'upload', %s, 'c5t6-test', 'in_progress', "
            "now(), now()) ON CONFLICT (id) DO NOTHING", (_B_RUN_ID, _B_SOURCE))
    yield _B_RUN_ID
    db.rollback()   # release the request tx's FK key-share on the run row BEFORE the delete below
    with psycopg.connect(_dsn, autocommit=True) as c:
        call_refs = [r[0] for r in c.execute(
            "SELECT DISTINCT llm_call_ref FROM ingestion_run_llm_call "
            "WHERE ingestion_run_id = %s", (_B_RUN_ID,)).fetchall()]
        c.execute("DELETE FROM llm_call_dispatch WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s) "
                  "OR llm_call_ref = ANY(%s)", (_B_RUN_ID, call_refs))
        c.execute("DELETE FROM ingestion_run_llm_call WHERE ingestion_run_id = %s", (_B_RUN_ID,))
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "DISABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch DISABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("DELETE FROM llm_dispatch_outcome WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)",
                  (_B_RUN_ID,))
        c.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)",
                  (_B_RUN_ID,))
        c.execute("DELETE FROM llm_dispatch WHERE ingestion_run_id = %s", (_B_RUN_ID,))
        c.execute("ALTER TABLE llm_dispatch ENABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "ENABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM llm_call WHERE llm_call_ref = ANY(%s)", (call_refs,))
        c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM ingestion_run WHERE id = %s", (_B_RUN_ID,))


def test_pre_dispatch_audit_failure_blocks_egress_but_ingest_completes(
        db, durable_run, _dsn, monkeypatch) -> None:
    """record_dispatch raising AuditUnavailable ⟹ the provider is NEVER egressed, yet core
    ingestion still COMPLETES (fail-soft) and nothing is cached from a non-existent response."""
    _seal_config()
    rows = _rows()
    client = _CountingClient(_script(rows))

    def _unavailable(**_kwargs):
        raise AuditUnavailable("audit store down (test)")

    monkeypatch.setattr(dispatch_audit_module, "record_dispatch", _unavailable)
    res = ingest_upload(db, _B_SOURCE, rows, actor=_actor(), now=_NOW, client=client,
                        ingestion_run_id=durable_run)

    # (a) core ingestion COMPLETES — the audit failure never aborts the upload.
    assert res.status == "ingested"
    # (b) the provider was NEVER egressed — fail-closed short-circuits before the inner client.
    assert client.calls == 0
    # (c) the graph persisted — all three columns landed (ingestion is not rolled back).
    graph_cols = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s AND kind = 'column'",
        (_B_SOURCE,)).fetchone()[0]
    assert graph_cols == 3
    # (d) NOTHING was cached/persisted from a non-existent response — no LLM concept landed.
    enriched = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s AND concept IS NOT NULL",
        (_B_SOURCE,)).fetchone()[0]
    assert enriched == 0
    # (e) no physical dispatch was recorded — fail-closed happens BEFORE any egress authorization.
    with psycopg.connect(_dsn) as fresh:
        dispatches = fresh.execute(
            "SELECT count(*) FROM llm_dispatch WHERE ingestion_run_id = %s",
            (durable_run,)).fetchone()[0]
    assert dispatches == 0


# ── C. eligibility-ordering DISCARD — the outcome audit gates cache/evidence ──────────────────────


def _accept_known(raw):
    return (raw, "valid") if raw in {"monetary_stock", "unclassified"} else (None, "invalid_value")


@pytest.fixture
def durable_by_task(monkeypatch, _dsn):
    """Point FEATUREGEN_DSN at the test cluster so the own-connection dispatch/outcome/llm_call
    writes really commit — the eligibility-discard path still RECORDS them (record → link → return)
    before it discards. Clean up by task afterwards; both dispatch tables are write-once, so drop the
    trigger guards just long enough to delete (mirrors the C5-T2..T4 fixtures). The discard uses a
    None run id (dispatch header records NULL), so no ingestion_run row is needed."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    yield
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("DELETE FROM llm_call_dispatch WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE task LIKE %s)", ("test.c5t6.%",))
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "DISABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_dispatch DISABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("DELETE FROM llm_dispatch_outcome WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE task LIKE %s)", ("test.c5t6.%",))
        c.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE task LIKE %s)", ("test.c5t6.%",))
        c.execute("DELETE FROM llm_dispatch WHERE task LIKE %s", ("test.c5t6.%",))
        c.execute("ALTER TABLE llm_dispatch ENABLE TRIGGER llm_dispatch_no_mutation")
        c.execute("ALTER TABLE llm_dispatch_subject "
                  "ENABLE TRIGGER llm_dispatch_subject_no_mutation")
        c.execute("ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM llm_call WHERE task LIKE %s", ("test.c5t6.%",))
        c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")


def _ctx() -> DispatchAuditContext:
    # run id None: the ingestion (dispatch_audit-present) path is triggered by the CONTEXT, not the
    # run id — recorded honestly as a NULL ingestion_run_id, so no ingestion_run row is required.
    return DispatchAuditContext(ingestion_run_id=None, stage="enrichment", subjects=_SUBJECTS)


def test_link_failure_discards_structured_result(db, durable_by_task, _dsn, monkeypatch) -> None:
    """dispatch_audit set + the outcome audit (link) FAILS ⟹ audited_structured_call returns None
    (the provider result is DISCARDED — not eligible for cache/evidence). The dispatch + llm_call
    are still recorded durably FIRST (record → link → return ordering)."""
    register_enrichment_schemas(db)
    monkeypatch.setattr(enrich_llm, "link_llm_call", lambda **_kwargs: False)
    task = "test.c5t6.structured.discard"
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    out = audited_structured_call(
        db, client, task=task, prompt_id="overlay_concept_v1", schema_id="overlay_concept",
        catalog_metadata=_META, instruction="Classify the concept of this column.",
        dispatch_audit=_ctx())
    assert out is None      # DISCARDED — the logical outcome audit did not commit
    # record → link → return: the dispatch header + immutable llm_call ARE durably recorded BEFORE
    # the discard (only the convenience linkage failed — evidence of egress is preserved).
    with psycopg.connect(_dsn) as fresh:
        assert fresh.execute("SELECT count(*) FROM llm_dispatch WHERE task = %s",
                             (task,)).fetchone()[0] == 1
        assert fresh.execute("SELECT count(*) FROM llm_call WHERE task = %s",
                             (task,)).fetchone()[0] == 1


def test_link_failure_discards_batch_result(db, durable_by_task, monkeypatch) -> None:
    """dispatch_audit set + the outcome audit (link) FAILS ⟹ audited_batch_call marks every
    INCLUDED ref MISSING — the response would have classified h1 VALID, but the failed outcome audit
    discards it so nothing is harvested/cached."""
    register_enrichment_schemas(db)
    monkeypatch.setattr(enrich_llm, "link_llm_call", lambda **_kwargs: False)
    task = "test.c5t6.batch.discard"
    items = [eb.BatchItem("h1", {"table": "accounts", "column": "balance", "type": "numeric"})]
    client = FakeLLM(script={task: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})})
    res = audited_batch_call(
        db, client, task=task, prompt_id="overlay_concept_batch_v1",
        schema_id="overlay_concept_batch",
        shared_metadata={"vocabulary": [{"name": "monetary_stock"}]}, items=items,
        out_key="concept", instruction="Classify each column.", accept=_accept_known,
        dispatch_audit=_ctx())
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == eb.MISSING             # discarded, not VALID
    assert all(o.value is None for o in res.outcomes)   # nothing harvestable / cacheable


def test_none_ctx_never_links_and_returns_result_byte_identical(db, monkeypatch) -> None:
    """dispatch_audit=None (contract authoring / feature generation): link_llm_call is NEVER called
    and the validated output is returned unchanged — byte-identical to pre-C5-T6 behavior."""
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    register_enrichment_schemas(db)
    calls: list = []
    monkeypatch.setattr(enrich_llm, "link_llm_call",
                        lambda **kwargs: (calls.append(kwargs), True)[1])
    task = "test.c5t6.none"   # no DSN → llm_call written on the rolled-back request conn, no cleanup
    client = FakeLLM(script={task: FakeResponse(output={"concept": "monetary_amount"})})
    out = audited_structured_call(
        db, client, task=task, prompt_id="overlay_concept_v1", schema_id="overlay_concept",
        catalog_metadata=_META, instruction="Classify the concept of this column.",
        dispatch_audit=None)
    assert out == {"concept": "monetary_amount"}     # result returned unchanged
    assert calls == []                               # link_llm_call NEVER called when ctx is None

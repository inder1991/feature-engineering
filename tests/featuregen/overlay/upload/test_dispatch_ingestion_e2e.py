"""Delivery C5 Task 5 — the attributability proof, end to end through a REAL ingest.

C5-T1..T4 built the dispatch-audit tables + seam, but no ingestion caller passed a context, so the
audit never fired in production. This suite drives ``ingest_upload`` itself (the real call sites:
Pass A concept/definition/domain and Pass B table synthesis) with an ``ingestion_run_id`` and a
FakeLLM scripted for the enrichment stages, then proves on a FRESH connection that:

  1. every enrichment stage's PHYSICAL dispatch was pre-audited under THIS run id with the exact
     object subjects (the columns/table enriched), durably (survives the request-tx rollback);
  2. the C5 attributability JOIN — llm_dispatch → llm_call_dispatch → ingestion_run_llm_call +
     llm_dispatch_subject — resolves every dispatched enrichment batch to THIS ingestion run and
     its subjects (a regulator can answer "which run + which objects produced this egress");
  3. Pass B (flag-gated) attributes its synthesis dispatch the same way, at TABLE grain.

Durable-row cleanup mirrors the C5-T2..T4 suites (write-once triggers dropped just long enough);
the fixture ALSO rolls the request tx back first so the run row's uncommitted provenance children
(ingestion_run_object/_fact) can't block the durable delete.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.ingest import ingest_upload

_RUN_ID = "ingrun_c5t5_e2e"
_SOURCE = "c5t5src"
_NOW = datetime(2026, 7, 18, tzinfo=UTC)


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
        CanonicalRow(_SOURCE, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(_SOURCE, "accounts", "balance", "numeric"),
    ]


def _client(rows: list[CanonicalRow], *, pass_b: bool = False) -> FakeLLM:
    """FakeLLM scripted for the batch-mode (default) enrichment stages, keyed by the REAL batch item
    refs (the content hashes Pass A batches on; the normalized table name for domain/Pass B)."""
    hashes = [content_hash(r) for r in rows]
    script = {
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": "unclassified"} for h in hashes]}),
        "overlay.enrich.definition": FakeResponse(output={"results": [
            {"ref": h, "definition": "A governed banking column."} for h in hashes]}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": "accounts", "domain": "deposits"}]}),
    }
    if pass_b:
        script["table_synth"] = FakeResponse(output={"results": [
            {"ref": "accounts", "synthesis": {
                "grain_columns": ["id"], "as_of_column": "posted_at",
                "as_of_basis": "posted_at", "primary_entity": None,
                "table_role": None, "event_or_snapshot": None}}]})
    return FakeLLM(script=script)


@pytest.fixture
def durable_run(monkeypatch, _dsn, db):
    """Point FEATUREGEN_DSN at the test cluster so the own-connection dispatch/llm_call/link writes
    really commit, and durably create the ingestion_run row they FK-reference. Cleanup first rolls
    the REQUEST tx back (its uncommitted ingestion_run_object/_fact children would otherwise block
    the run-row delete), then removes everything committed outside it — associations first (they FK
    both sides), then outcome → subject → dispatch and the durable llm_call rows (write-once
    triggers dropped just long enough), then the run row itself. Mirrors the C5-T4 fixture."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
            "started_at, heartbeat_at) VALUES (%s, 'upload', %s, 'c5t5-test', 'in_progress', "
            "now(), now()) ON CONFLICT (id) DO NOTHING", (_RUN_ID, _SOURCE))
    yield _RUN_ID
    db.rollback()   # release the request tx's FK key-share on the run row BEFORE the delete below
    with psycopg.connect(_dsn, autocommit=True) as c:
        call_refs = [r[0] for r in c.execute(
            "SELECT DISTINCT llm_call_ref FROM ingestion_run_llm_call "
            "WHERE ingestion_run_id = %s", (_RUN_ID,)).fetchall()]
        c.execute("DELETE FROM llm_call_dispatch WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s) "
                  "OR llm_call_ref = ANY(%s)", (_RUN_ID, call_refs))
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
        c.execute("DELETE FROM llm_call WHERE llm_call_ref = ANY(%s)", (call_refs,))
        c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM ingestion_run WHERE id = %s", (_RUN_ID,))


def _attributed_rows(fresh, run_id: str) -> list[tuple]:
    """The C5 attributability JOIN: for every dispatch of this run, resolve its logical llm_call,
    the run+stage association, and the object subjects — the query a regulator would run."""
    return fresh.execute(
        "SELECT d.stage, irlc.ingestion_run_id, s.catalog_source, s.object_ref, "
        "       s.logical_ref, s.field_names "
        "FROM llm_dispatch d "
        "JOIN llm_call_dispatch lcd ON lcd.dispatch_ref = d.dispatch_ref "
        "JOIN ingestion_run_llm_call irlc ON irlc.llm_call_ref = lcd.llm_call_ref "
        "     AND irlc.stage = d.stage "
        "JOIN llm_dispatch_subject s ON s.dispatch_ref = d.dispatch_ref "
        "WHERE d.ingestion_run_id = %s", (run_id,)).fetchall()


# ── 1+2. Pass A: a real technical ingest attributes every enrichment dispatch to run + columns ──


def test_real_ingest_attributes_pass_a_dispatches_to_run_and_columns(db, durable_run, _dsn):
    _seal_config()
    rows = _rows()
    res = ingest_upload(db, _SOURCE, rows, actor=_actor(), now=_NOW, client=_client(rows),
                        ingestion_run_id=durable_run)
    assert res.status == "ingested"

    with psycopg.connect(_dsn) as fresh:   # fresh conn: the audit must ALREADY be durable
        stages = {r[0] for r in fresh.execute(
            "SELECT DISTINCT stage FROM llm_dispatch WHERE ingestion_run_id = %s",
            (durable_run,)).fetchall()}
        attributed = _attributed_rows(fresh, durable_run)
        orphan_dispatches = fresh.execute(
            "SELECT count(*) FROM llm_dispatch d WHERE d.ingestion_run_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM llm_call_dispatch lcd "
            "                WHERE lcd.dispatch_ref = d.dispatch_ref)",
            (durable_run,)).fetchone()[0]

    # All three Pass A stages fired a pre-audited dispatch under THIS run.
    assert {"enrich_concept", "enrich_definition", "enrich_domain"} <= stages
    # Every dispatch resolves back to a recorded logical llm_call (none left unattributable).
    assert orphan_dispatches == 0
    # Every joined row belongs to THIS run, with a non-empty subject set overall.
    assert attributed and all(r[1] == durable_run for r in attributed)

    columns = ("id", "posted_at", "balance")
    expected_column_subjects = {
        (_SOURCE, f"public.accounts.{c}", f"{_SOURCE}::public.accounts.{c}", (c,))
        for c in columns}
    by_stage: dict[str, set[tuple]] = {}
    for stage, _run, cat, obj, logical, fields in attributed:
        by_stage.setdefault(stage, set()).add((cat, obj, logical, tuple(fields)))
    # Concept + definition batches were about EXACTLY the three uploaded columns…
    assert by_stage["enrich_concept"] == expected_column_subjects
    assert by_stage["enrich_definition"] == expected_column_subjects
    # …and the domain batch about the table, carrying its column roster.
    assert by_stage["enrich_domain"] == {
        (_SOURCE, "public.accounts", f"{_SOURCE}::public.accounts", tuple(sorted(columns)))}


# ── 3. Pass B (flag-gated): the synthesis dispatch attributes at table grain ─────────────────────


def test_real_ingest_attributes_pass_b_synthesis_dispatch(db, durable_run, _dsn, monkeypatch):
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    _seal_config()
    rows = _rows()
    res = ingest_upload(db, _SOURCE, rows, actor=_actor(), now=_NOW,
                        client=_client(rows, pass_b=True), ingestion_run_id=durable_run)
    assert res.status == "ingested"
    assert res.passb_proposed >= 1           # the synthesis really resolved (not a silent miss)

    with psycopg.connect(_dsn) as fresh:
        pass_b = [r for r in _attributed_rows(fresh, durable_run) if r[0] == "pass_b"]
    assert pass_b, "Pass B produced no attributed dispatch"
    assert all(r[1] == durable_run for r in pass_b)
    assert {(r[2], r[3], r[4], tuple(r[5])) for r in pass_b} == {
        (_SOURCE, "public.accounts", f"{_SOURCE}::public.accounts",
         ("balance", "id", "posted_at"))}

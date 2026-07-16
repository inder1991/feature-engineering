"""Upload-path ingestion-run manifest (first-release hardening #3 CORE): every POST /uploads
attempt — ingested, held, rejected, parse-failed, or ingest-faulted — leaves a durable, queryable
run record, and the run id rides the ``X-Ingestion-Run-Id`` RESPONSE HEADER on success AND on
every post-open error (headers don't change the JSON body, preserving flag-off byte-for-byte).
``GET /ingestion-runs/{run_id}`` (catalog_read) is the primary surface — it works for failed
requests too. In this harness the request connection is the suite's shared rolled-back conn and
FEATUREGEN_DSN is unset, so the lifecycle writes take the module's request-conn fallback path;
the independent-commit durability itself is proven in
tests/featuregen/overlay/upload/test_ingestion_run.py."""
from __future__ import annotations

import hashlib
import io

import psycopg
from fastapi.testclient import TestClient
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, VIEWER, upload_csv

RUN_HEADER = "X-Ingestion-Run-Id"

TINY_CSV = "source,table,column,type,is_grain\ndeposits,accounts,id,integer,y\n"


def _get_run(client, run_id: str, headers=AUTH):
    return client.get(f"/ingestion-runs/{run_id}", headers=headers)


# ── the happy path records a full manifest ────────────────────────────────────────────────────────


def test_successful_upload_records_ingested_run(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 200
    run_id = res.headers[RUN_HEADER]
    assert run_id.startswith("ingrun_")

    run = _get_run(client, run_id).json()
    assert run["id"] == run_id
    assert run["status"] == "ingested"
    assert run["origin_type"] == "upload"
    assert run["catalog_source"] == "deposits"
    assert run["filename"] == "deposits.csv"
    assert run["actor_subject"] == "user:tester"
    assert run["actor_role_claims"] == ["platform_admin"]
    # review FIX 4: the run records the permission-gate outcome that admitted it —
    # POST /uploads is gated by require_catalog_write, so the decision is never NULL here
    assert run["authorization_decision"] == "granted:catalog_write"
    assert run["row_count"] == 9
    assert run["quarantined_count"] == 0
    assert run["file_sha256"] == hashlib.sha256(DEPOSITS_CSV.encode()).hexdigest()
    assert run["fingerprint_algo_version"] == "gn-v1"
    # first upload: the pre fingerprint is the empty-graph hash, the post reflects the built graph
    assert run["pre_source_fingerprint"] != run["post_source_fingerprint"]
    assert run["completed_at"] is not None
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "ingested"]
    # the effective_config snapshot is exactly the allowlist — flags + provider/model, no secrets
    assert set(run["effective_config"]) == {
        "config_schema_version", "governed_joins", "pass_c", "table_synth",
        "llm_provider", "llm_model"}


def test_unchanged_reupload_fingerprints_match(client):
    """The fingerprint's purpose: an unchanged re-upload's pre/post correlate as 'nothing moved'."""
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "ingested"
    assert run["pre_source_fingerprint"] == run["post_source_fingerprint"]


# ── every non-success outcome still records its run ───────────────────────────────────────────────


def test_held_upload_records_held_run(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = upload_csv(client, "deposits", TINY_CSV)   # truncated re-upload -> the large-change brake
    assert res.json()["status"] == "held"
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "held"
    assert run["row_count"] == 1
    assert run["completed_at"] is not None
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "held"]


def test_all_quarantined_upload_records_rejected_run(client):
    res = upload_csv(client, "deposits", "source,table,column,type\n")   # empty -> rejected
    assert res.json()["status"] == "rejected"
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "rejected"
    assert run["row_count"] == 0


def test_parse_failure_still_records_queryable_run(client):
    """The durability point of design #3: the request 400'd (its transaction would roll back in
    production), yet the attempt left a manifest row retrievable by the header id."""
    res = client.post("/uploads", data={"source": "gl"},
                      files={"file": ("gl.xlsx", b"not a workbook",
                                      "application/octet-stream")}, headers=AUTH)
    assert res.status_code == 400
    run_id = res.headers[RUN_HEADER]

    got = _get_run(client, run_id)
    assert got.status_code == 200
    run = got.json()
    assert run["status"] == "rejected"
    assert run["completed_at"] is not None
    assert run["file_sha256"] == hashlib.sha256(b"not a workbook").hexdigest()
    assert run["redacted_failure_code"]                    # the exception CLASS, never its message
    assert "not a workbook" not in str(run)                # redaction: no file content in the run
    assert [e["status"] for e in run["status_history"]] == ["in_progress", "rejected"]
    assert run["status_history"][-1]["reason_code"] == "http_400"


def test_unsupported_extension_records_rejected_run(client):
    res = client.post("/uploads", data={"source": "deposits"},
                      files={"file": ("notes.txt", b"hello", "text/plain")}, headers=AUTH)
    assert res.status_code == 400
    assert _get_run(client, res.headers[RUN_HEADER]).json()["status"] == "rejected"


def test_oversized_upload_records_rejected_run_without_sha(client, monkeypatch):
    """413: the file was never fully read, so file_sha256 is honestly NULL (design #3)."""
    from featuregen.api.routes import uploads
    monkeypatch.setattr(uploads, "_MAX_UPLOAD_BYTES", 100)
    payload = b"source,table,column,type\n" + b"x" * 500
    res = client.post("/uploads", data={"source": "bank"},
                      files={"file": ("big.csv", io.BytesIO(payload), "text/csv")}, headers=AUTH)
    assert res.status_code == 413
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "rejected"
    assert run["file_sha256"] is None


def test_ingest_fault_records_failed_run(client, monkeypatch):
    """A post-parse ingest-stage fault is a FAILED attempt (the file itself was not rejected)."""
    from featuregen.api.routes import uploads

    def _raise(*args, **kwargs):
        raise RuntimeError("wat")

    monkeypatch.setattr(uploads, "ingest_upload", _raise)
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 500
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "failed"
    assert run["redacted_failure_code"] == "RuntimeError"
    assert run["status_history"][-1]["reason_code"] == "http_500"


def test_raw_db_fault_records_failed_run_on_fresh_conn_and_500_carries_header(
        client, conn, monkeypatch, _dsn):
    """Review FIX 2 (+ M-5): a REAL psycopg.Error escaping create_upload — here from the
    source_fingerprint seam, which only the outer handlers see — POISONS the request transaction
    (any later statement on it raises InFailedSqlTransaction), so the terminal state can only be
    recorded by ``terminalize_run_durable`` on a FRESH connection. The raw 500 must still carry
    the run-id header (body byte-for-byte Starlette's default), and the 'failed' run must be
    retrievable via GET afterwards. Pre-fix the exception escaped the route entirely: no header,
    and the run stayed 'in_progress' forever."""
    from featuregen.api.routes import uploads

    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)   # arm the fresh-connection durable path

    def _poisoning_fingerprint(c, source):
        # genuinely-bad SQL on the REQUEST connection: raises a real psycopg.Error AND aborts
        # the tx — the in-request fallback conn is now unusable, only a fresh conn can write
        return c.execute("SELECT definitely_not_a_column FROM ingestion_run").fetchone()

    monkeypatch.setattr(uploads, "source_fingerprint", _poisoning_fingerprint)

    run_id = None
    try:
        with TestClient(client.app, raise_server_exceptions=False) as raw_client:
            res = upload_csv(raw_client, "deposits", DEPOSITS_CSV)
        assert res.status_code == 500
        run_id = res.headers[RUN_HEADER]                    # the header survives the raw fault
        assert run_id.startswith("ingrun_")
        assert res.text == "Internal Server Error"          # body-compat: the default 500, untouched

        conn.rollback()   # clear the poisoned suite tx so the GET below can run on it
        run = _get_run(client, run_id).json()
        assert run["status"] == "failed"                    # written on the FRESH conn (durable)
        assert run["redacted_failure_code"] == "UndefinedColumn"   # the CLASS, never the message
        assert "definitely_not_a_column" not in str(run)
        assert [e["status"] for e in run["status_history"]] == ["in_progress", "failed"]
        assert run["status_history"][-1]["reason_code"] == "unhandled_exception"
    finally:
        if run_id:   # the durable rows committed for real — clean them up
            with psycopg.connect(_dsn) as c:
                c.execute("DELETE FROM ingestion_run_stage WHERE ingestion_run_id = %s",
                          (run_id,))
                c.execute("DELETE FROM ingestion_run_status_event WHERE ingestion_run_id = %s",
                          (run_id,))
                c.execute("DELETE FROM ingestion_run WHERE id = %s", (run_id,))


# ── per-stage status (#22) rides the SAME run surface ─────────────────────────────────────────────


def test_successful_upload_records_ordered_stage_reports(client):
    """Design #22: the run answers 'what actually ran' per stage — parse (recorded by the route)
    first, then every ingest stage in execution order with its honest state. The suite app has no
    LLM client, so the three enrichment stages are skipped_no_client (not a fake 'succeeded');
    the flag-gated passes are disabled."""
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    stages = {s["stage"]: s for s in run["stages"]}
    assert [s["stage"] for s in run["stages"]] == [
        "parse", "validation", "brake", "fact_assertion", "drift", "glossary_classification",
        "enrich_concept", "enrich_definition", "enrich_domain", "graph_persistence",
        "governed_joins", "pass_c", "pass_b", "glossary_evidence", "projection_drain",
        "table_fact_projection", "join_projection", "join_drift", "quarantine"]
    assert stages["parse"]["state"] == "succeeded"
    assert stages["validation"]["state"] == "succeeded"
    assert stages["fact_assertion"]["detail"] == {"asserted": 4}
    assert stages["enrich_concept"]["state"] == "skipped_no_client"
    assert stages["enrich_definition"]["state"] == "skipped_no_client"
    assert stages["enrich_domain"]["state"] == "skipped_no_client"
    assert stages["pass_b"]["state"] == "disabled"
    assert stages["pass_c"]["state"] == "disabled"
    assert stages["quarantine"]["detail"] == {"rows": 0}
    assert all(s["attempt"] == 1 and s["completed_at"] is not None for s in run["stages"])


def test_quarantining_upload_reports_validation_partial(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,,integer\n")
    assert res.json()["quarantined"] == 1
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    stages = {s["stage"]: s for s in run["stages"]}
    assert stages["validation"]["state"] == "partial"       # per-row failures surface, not launder
    assert stages["validation"]["detail"] == {"good": 9, "quarantined": 1}
    assert stages["quarantine"]["detail"] == {"rows": 1}


def test_held_upload_reports_brake_deferred_and_stops(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = upload_csv(client, "deposits", TINY_CSV)
    assert res.json()["status"] == "held"
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert [(s["stage"], s["state"]) for s in run["stages"]] == [
        ("parse", "succeeded"), ("validation", "succeeded"), ("brake", "deferred")]


def test_parse_failure_reports_a_failed_parse_stage(client):
    """A failed request still gets its stage account (flushed durably beside the failed run):
    parse failed, nothing after it — the honest 'we never reached ingest'."""
    res = client.post("/uploads", data={"source": "gl"},
                      files={"file": ("gl.xlsx", b"not a workbook",
                                      "application/octet-stream")}, headers=AUTH)
    assert res.status_code == 400
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert [(s["stage"], s["state"]) for s in run["stages"]] == [("parse", "failed")]
    assert run["stages"][0]["reason_code"] == "http_400"


def test_unsupported_extension_reports_failed_parse(client):
    res = client.post("/uploads", data={"source": "deposits"},
                      files={"file": ("notes.txt", b"hello", "text/plain")}, headers=AUTH)
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert [(s["stage"], s["state"]) for s in run["stages"]] == [("parse", "failed")]


def test_ingest_fault_still_reports_stages_reached(client, monkeypatch):
    """An ingest-stage fault: the run is failed, and the stages recorded BEFORE the fault (parse)
    are still flushed on the durable path — a failed run shows how far it got."""
    from featuregen.api.routes import uploads

    def _raise(*args, **kwargs):
        raise RuntimeError("wat")

    monkeypatch.setattr(uploads, "ingest_upload", _raise)
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 500
    run = _get_run(client, res.headers[RUN_HEADER]).json()
    assert run["status"] == "failed"
    assert [(s["stage"], s["state"]) for s in run["stages"]] == [("parse", "succeeded")]


# ── GET /ingestion-runs/{run_id} ──────────────────────────────────────────────────────────────────


def test_get_run_requires_auth_and_allows_catalog_viewer(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    run_id = res.headers[RUN_HEADER]
    assert _get_run(client, run_id, headers=None).status_code == 401
    assert _get_run(client, run_id, headers=VIEWER).status_code == 200   # catalog_read suffices


def test_get_unknown_run_404s(client):
    assert _get_run(client, "ingrun_DOES_NOT_EXIST").status_code == 404

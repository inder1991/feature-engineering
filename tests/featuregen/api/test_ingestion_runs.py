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


# ── GET /ingestion-runs/{run_id} ──────────────────────────────────────────────────────────────────


def test_get_run_requires_auth_and_allows_catalog_viewer(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    run_id = res.headers[RUN_HEADER]
    assert _get_run(client, run_id, headers=None).status_code == 401
    assert _get_run(client, run_id, headers=VIEWER).status_code == 200   # catalog_read suffices


def test_get_unknown_run_404s(client):
    assert _get_run(client, "ingrun_DOES_NOT_EXIST").status_code == 404

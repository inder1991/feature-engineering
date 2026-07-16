from io import BytesIO

from openpyxl import Workbook
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_upload_requires_stub_auth(client):
    res = client.post("/uploads", data={"source": "deposits"},
                      files={"file": ("d.csv", b"source,table,column,type\n", "text/csv")})
    assert res.status_code == 401


def test_first_upload_ingests_and_flags(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ingested"
    assert body["asserted"] == 4          # 3 grain facts + 1 availability_time
    assert body["quarantined"] == 0
    assert "first upload" in body["flagged"]


def test_bad_rows_quarantined_good_rows_ingested(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,,integer\n")
    body = res.json()
    assert body["status"] == "ingested"
    assert body["quarantined"] == 1


def test_invalid_boolean_quarantines_row_not_whole_upload(client):
    """#18 follow-up: one typo'd is_grain token must quarantine that ROW with a reason — consistent
    with the enum fields (cardinality/additivity/as_of_basis) — not 400 the entire file, and never
    silently coerce to False (the original #18 finding)."""
    res = upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,savings,flag,integer,maybe\n")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ingested"                   # the rest of the file still ingests
    assert body["quarantined"] == 1                       # only the typo'd row is held for review


def test_padded_source_resolves_to_same_catalog(client):
    """#16: a padded source id must resolve to the SAME catalog as its trimmed form. The padded
    truncated re-upload hits the large-change brake — pre-fix it silently minted a SECOND catalog
    (' deposits ') and reported a fresh 'first upload' ingest instead."""
    upload_csv(client, "deposits", DEPOSITS_CSV)
    tiny = "source,table,column,type,is_grain\ndeposits,accounts,id,integer,y\n"
    res = client.post("/uploads", data={"source": " deposits "},
                      files={"file": ("deposits.csv", tiny.encode(), "text/csv")}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["status"] == "held"     # same catalog -> the brake sees the truncation


def test_case_variant_source_resolves_to_same_catalog(client):
    """#16 follow-up: the catalog source is case-normalized like every other identity component
    (object_ref._norm is strip+LOWER). Pre-fix the boundary only stripped, so 'Deposits' missed the
    prior 'deposits' refs (case-sensitive) and a truncated re-upload bypassed the large-change brake
    as a fresh 'first upload' — while its facts still keyed on the lowered 'deposits' stream."""
    upload_csv(client, "deposits", DEPOSITS_CSV)
    tiny = "source,table,column,type,is_grain\ndeposits,accounts,id,integer,y\n"
    res = client.post("/uploads", data={"source": "Deposits"},
                      files={"file": ("deposits.csv", tiny.encode(), "text/csv")}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["status"] == "held"     # same catalog -> the brake sees the truncation


def test_whitespace_only_source_400(client):
    """#16: a source that strips to nothing is a client error, not a catalog named '   '."""
    res = client.post("/uploads", data={"source": "   "},
                      files={"file": ("d.csv", DEPOSITS_CSV.encode(), "text/csv")}, headers=AUTH)
    assert res.status_code == 400


def test_truncated_reupload_is_held(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    tiny = "source,table,column,type,is_grain\ndeposits,accounts,id,integer,y\n"
    body = upload_csv(client, "deposits", tiny).json()
    assert body["status"] == "held"       # 200 — a brake, not an error
    assert body["reason"]


def test_empty_upload_rejected(client):
    res = upload_csv(client, "deposits", "source,table,column,type\n")
    assert res.status_code == 200         # first-class state, not an HTTP error
    assert res.json()["status"] == "rejected"


def test_ingest_result_counts_changed_objects_not_staled_facts(client):
    """#30: the drift counter counts CHANGED CATALOG OBJECTS (drop/type_change/rename). A type
    change on a column carrying NO facts still counts 1 while ZERO facts stale — so the old
    'staled' name lied. The field now says what it counts."""
    upload_csv(client, "deposits", DEPOSITS_CSV)
    changed = DEPOSITS_CSV.replace("balance,numeric", "balance,text")   # one type_change, no facts
    body = upload_csv(client, "deposits", changed).json()
    assert body["status"] == "ingested"
    assert "staled" not in body
    assert body["changed_objects"] == 1   # one object changed; no fact was staled by it


def test_excel_upload_ingests(client):
    data = _xlsx([["source", "table", "column", "type", "is_grain"],
                  ["gl", "ledger", "entry_id", "integer", "y"],
                  ["gl", "ledger", "amount", "numeric", ""]])
    res = client.post(
        "/uploads", data={"source": "gl"},
        files={"file": ("gl.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=AUTH)
    assert res.json()["status"] == "ingested"


def test_unsupported_extension_400(client):
    res = client.post("/uploads", data={"source": "deposits"},
                      files={"file": ("notes.txt", b"hello", "text/plain")}, headers=AUTH)
    assert res.status_code == 400
    # #28: the reject message lists EVERYTHING the reader accepts — .xlsm was accepted but unnamed.
    detail = res.json()["detail"]
    for ext in (".csv", ".xlsx", ".xlsm"):
        assert ext in detail


def test_unparseable_excel_400(client):
    res = client.post("/uploads", data={"source": "gl"},
                      files={"file": ("gl.xlsx", b"not a workbook", "application/octet-stream")},
                      headers=AUTH)
    assert res.status_code == 400


def test_upload_enriches_via_configured_llm_client(make_client):
    """The one thing the API owns post-M2/M4: passing the app's client through to ingest.
    Enrichment content is backend-tested (test_enrich_llm.py); here we prove pass-through and
    that a misbehaving provider degrades (no enrichment), never breaks the upload."""
    from featuregen.intake.llm import PROVIDER_OK, LLMResult

    class RecordingClient:
        def __init__(self):
            self.tasks: list[str] = []

        def call(self, request):
            self.tasks.append(request.task)
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_OK)

    recording = RecordingClient()
    client = make_client(llm_client=recording)
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.json()["status"] == "ingested"   # empty/invalid LLM output degrades gracefully
    assert recording.tasks                      # enrichment was attempted via the app's client


def _boom_ingest(monkeypatch, exc: Exception):
    """Make the route's ingest_upload seam raise `exc` (the route imports it module-level)."""
    from featuregen.api.routes import uploads

    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(uploads, "ingest_upload", _raise)


def test_concurrent_ingest_conflict_maps_to_409(client, monkeypatch):
    """#27: an OCC ConcurrencyError (a concurrent upload/confirm bumped a fact stream) is a
    retryable conflict, not an opaque 500."""
    from featuregen.contracts.errors import ConcurrencyError

    _boom_ingest(monkeypatch, ConcurrencyError("expected_version 3 != stream_version 4"))
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 409
    assert "concurrent" in res.json()["detail"]


def test_persist_fault_maps_to_422_with_stage_marker(client, monkeypatch):
    """#27: a graph-constraint / persist DB fault names its stage instead of an opaque 500."""
    import psycopg

    _boom_ingest(monkeypatch, psycopg.errors.UniqueViolation("duplicate key"))
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "persist" in detail
    assert "UniqueViolation" in detail


def test_unknown_ingest_fault_surfaces_500_with_stage_marker(client, monkeypatch):
    """#27: an unknown fault still surfaces as a 500 — but with a stage marker, never swallowed."""
    _boom_ingest(monkeypatch, RuntimeError("wat"))
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert "ingest stage" in detail
    assert "RuntimeError" in detail


def test_upload_response_body_unchanged_by_run_manifest(client):
    """FLAG-OFF / compatibility (design #3, CRITICAL): the run manifest is purely additive — the
    run id rides the X-Ingestion-Run-Id HEADER only, and the POST /uploads JSON body stays the
    exact IngestResult shape (same fields, same order) with the run id appearing nowhere in it."""
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 200
    body = res.json()
    assert list(body.keys()) == ["status", "reason", "asserted", "changed_objects",
                                 "quarantined", "flagged"]
    run_id = res.headers["X-Ingestion-Run-Id"]
    assert run_id.encode() not in res.content


def test_upload_rejects_oversized_file(client, monkeypatch):
    import io

    from featuregen.api.routes import uploads
    monkeypatch.setattr(uploads, "_MAX_UPLOAD_BYTES", 100)   # tiny cap for the test
    payload = b"source,table,column,type\n" + b"x" * 500
    resp = client.post("/uploads", data={"source": "bank"},
                       files={"file": ("big.csv", io.BytesIO(payload), "text/csv")}, headers=AUTH)
    assert resp.status_code == 413

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


def test_upload_rejects_oversized_file(client, monkeypatch):
    import io

    from featuregen.api.routes import uploads
    monkeypatch.setattr(uploads, "_MAX_UPLOAD_BYTES", 100)   # tiny cap for the test
    payload = b"source,table,column,type\n" + b"x" * 500
    resp = client.post("/uploads", data={"source": "bank"},
                       files={"file": ("big.csv", io.BytesIO(payload), "text/csv")}, headers=AUTH)
    assert resp.status_code == 413

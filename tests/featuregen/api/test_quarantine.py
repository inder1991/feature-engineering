from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv


def test_quarantine_lists_bad_rows_with_reason_and_raw(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    items = client.get("/sources/deposits/quarantine", headers=AUTH).json()
    assert len(items) == 1
    assert "missing required field(s): type" in items[0]["reason"]
    assert items[0]["raw"]["column"] == "opened_at"
    assert items[0]["row_index"] == 9     # 10th data row (0-indexed)


def test_clean_reupload_clears_queue(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert client.get("/sources/deposits/quarantine", headers=AUTH).json() == []


def test_unknown_source_empty_queue(client):
    assert client.get("/sources/nope/quarantine", headers=AUTH).json() == []


def test_quarantine_requires_auth(client):
    assert client.get("/sources/deposits/quarantine").status_code == 401


def test_resolve_endpoint_fixes_and_clears(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    r = client.post("/sources/deposits/quarantine/9/resolve",
                    json={"edits": {"type": "timestamp"}}, headers=AUTH)
    assert r.status_code == 200 and r.json()["resolved"] is True
    assert client.get("/sources/deposits/quarantine", headers=AUTH).json() == []


def test_resolve_endpoint_reports_still_invalid(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    r = client.post("/sources/deposits/quarantine/9/resolve",
                    json={"edits": {"type": ""}}, headers=AUTH)   # still blank -> not resolved
    assert r.status_code == 200 and r.json()["resolved"] is False and r.json()["reason"]
    assert len(client.get("/sources/deposits/quarantine", headers=AUTH).json()) == 1


def test_dismiss_endpoint(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    assert client.post("/sources/deposits/quarantine/9/dismiss", headers=AUTH).status_code == 200
    assert client.get("/sources/deposits/quarantine", headers=AUTH).json() == []
    assert client.post("/sources/deposits/quarantine/999/dismiss", headers=AUTH).status_code == 404


# Ingest normalizes the source (uploads.py: source.strip().lower()), so quarantine rows live
# under the lowercased source. The quarantine routes must normalize the path param the SAME way,
# or /sources/Deposits/quarantine silently answers [] for a queue stored under 'deposits' (#11).

def test_list_and_resolve_normalize_mixed_case_source(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    items = client.get("/sources/Deposits/quarantine", headers=AUTH).json()
    assert len(items) == 1
    assert "missing required field(s): type" in items[0]["reason"]

    r = client.post("/sources/DEPOSITS/quarantine/9/resolve",
                    json={"edits": {"type": "timestamp"}}, headers=AUTH)
    assert r.status_code == 200 and r.json()["resolved"] is True
    assert client.get("/sources/deposits/quarantine", headers=AUTH).json() == []


def test_dismiss_normalizes_mixed_case_source(client):
    upload_csv(client, "deposits", DEPOSITS_CSV + "deposits,accounts,opened_at,\n")
    assert client.post("/sources/Deposits/quarantine/9/dismiss", headers=AUTH).status_code == 200
    assert client.get("/sources/deposits/quarantine", headers=AUTH).json() == []

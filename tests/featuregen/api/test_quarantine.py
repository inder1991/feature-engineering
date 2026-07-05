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

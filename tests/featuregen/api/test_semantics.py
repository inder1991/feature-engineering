"""Semantics-pending queue + owner completion (#22).

`semantics_pending` used to be only an arithmetic COUNT on the import summary ("N columns need
owner confirmation" — #25 honesty). These tests pin the REAL workflow: a source-scoped queue
listing every column that arrived without its semantic facts (as-of / additivity / unit /
currency / entity), and a completion write that fills them in — validated against the SAME
closed vocabularies validate_rows enforces, node-updated, search-doc-rebuilt, and audited.

The queue's predicate is SHARED with the connector's `semantics_pending_count` (a column is
pending iff it lacks ALL five semantic fields), so the list and the count can never disagree.
Grain/availability facts are governed (Pass B) and deliberately NOT touchable here.
"""
from tests.featuregen.api._helpers import AUTH, PII_AUTH, VIEWER, upload_csv

from featuregen.connectors.openmetadata import semantics_pending_count
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.csv_reader import read_csv_rows

# Three semantics-blank columns (acct_id, balance, rate_date) + two fully-anchored ones:
# posted_at (as_of=y) and base_rate (entity=Index). No 'account' token anywhere, so the
# completion test can prove the search_doc rebuild by searching the newly-set entity.
LEDGER_CSV = """\
source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,additivity,unit,currency,entity
ledger,balances,acct_id,integer,y,,primary key,,,,,,,
ledger,balances,balance,numeric,,,end-of-day ledger balance,,,,,,,
ledger,balances,posted_at,timestamp,,y,posting timestamp,,,,,,,
ledger,rates,rate_date,date,,,quote date,,,,,,,
ledger,rates,base_rate,numeric,,,reference rate,,,,,,,Index
"""

# A pii-tagged pending column next to an untagged one: the queue is read-scoped like search.
HR_CSV = """\
source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,additivity,unit,currency,entity
hr,people,ssn,text,,,social security number,pii,,,,,,
hr,people,hired_on,date,,,hire date,,,,,,,
"""

ALL_MISSING = ["as_of", "additivity", "unit", "currency", "entity"]


def _pending(client, source="ledger", headers=AUTH):
    res = client.get(f"/sources/{source}/semantics-pending", headers=headers)
    assert res.status_code == 200
    return res.json()


def _complete(client, ref, body, source="ledger", headers=AUTH):
    return client.post(f"/sources/{source}/columns/{ref}/semantics", json=body, headers=headers)


def _node(conn, ref, source="ledger"):
    return conn.execute(
        "SELECT additivity, unit, currency, entity, is_as_of FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s", (source, ref)).fetchone()


# ---- the queue (read) ------------------------------------------------------------------------


def test_queue_lists_blank_columns_and_omits_specified(client):
    upload_csv(client, "ledger", LEDGER_CSV)
    items = _pending(client)
    refs = [i["object_ref"] for i in items]
    assert refs == ["public.balances.acct_id", "public.balances.balance",
                    "public.rates.rate_date"]
    # Fully-anchored columns are OMITTED: an as-of axis or a declared entity is a semantic fact.
    assert "public.balances.posted_at" not in refs
    assert "public.rates.base_rate" not in refs
    balance = next(i for i in items if i["column"] == "balance")
    assert balance["table"] == "balances"
    assert balance["data_type"] == "numeric"
    assert balance["missing"] == ALL_MISSING       # pending == lacks the WHOLE semantic set


def test_queue_agrees_with_semantics_pending_count(client):
    """The list and the connector's count share ONE predicate — they can never diverge."""
    upload_csv(client, "ledger", LEDGER_CSV)
    vr = validate_rows(read_csv_rows(LEDGER_CSV, source="ledger"), "ledger")
    assert vr.structural_error is None and not vr.quarantined
    assert len(_pending(client)) == semantics_pending_count(vr.good) == 3


def test_queue_normalizes_mixed_case_source(client):
    upload_csv(client, "ledger", LEDGER_CSV)
    assert len(_pending(client, source="Ledger")) == 3


def test_queue_requires_auth_and_allows_readers(client):
    upload_csv(client, "ledger", LEDGER_CSV)
    assert client.get("/sources/ledger/semantics-pending").status_code == 401
    assert len(_pending(client, headers=VIEWER)) == 3          # catalog:read suffices


def test_queue_is_read_scoped_on_sensitivity(client):
    upload_csv(client, "hr", HR_CSV)
    assert [i["column"] for i in _pending(client, source="hr")] == ["hired_on"]
    assert [i["column"] for i in _pending(client, source="hr", headers=PII_AUTH)] == [
        "hired_on", "ssn"]


# ---- completion (write) ----------------------------------------------------------------------


def test_complete_updates_node_clears_queue_and_search(client, conn):
    upload_csv(client, "ledger", LEDGER_CSV)
    ref = "public.balances.balance"
    pre = client.get("/search", params={"q": "Account"}, headers=AUTH).json()
    assert ref not in {h["object_ref"] for h in pre["hits"]}   # entity not yet searchable

    res = _complete(client, ref, {"additivity": "additive", "unit": "dollars",
                                  "currency": "USD", "entity": "Account"})
    assert res.status_code == 200
    body = res.json()
    assert body["completed"] is True
    assert body["applied"] == {"additivity": "additive", "unit": "dollars",
                               "currency": "USD", "entity": "Account"}
    # The node carries the owner's facts...
    assert _node(conn, ref) == ("additive", "dollars", "USD", "Account", False)
    # ...the column leaves the queue...
    assert ref not in {i["object_ref"] for i in _pending(client)}
    assert len(_pending(client)) == 2
    # ...and the rebuilt search_doc makes it findable by the new entity (#20).
    post = client.get("/search", params={"q": "Account"}, headers=AUTH).json()
    assert ref in {h["object_ref"] for h in post["hits"]}


def test_complete_as_of_flag_clears_queue(client, conn):
    upload_csv(client, "ledger", LEDGER_CSV)
    ref = "public.rates.rate_date"
    res = _complete(client, ref, {"is_as_of": True, "as_of_basis": "posted_at"})
    assert res.status_code == 200
    # is_as_of is a node attribute; the BASIS lives only in the governed availability fact
    # stream (Pass B), so it is validated + audited here, never written onto the node.
    assert res.json()["applied"] == {"is_as_of": True}
    assert _node(conn, ref) == (None, None, None, None, True)
    assert ref not in {i["object_ref"] for i in _pending(client)}


def test_second_as_of_column_is_a_409_conflict(client, conn):
    """A table asserts ONE availability axis (validate_rows #17) — completion upholds it."""
    upload_csv(client, "ledger", LEDGER_CSV)
    res = _complete(client, "public.balances.acct_id", {"is_as_of": True})
    assert res.status_code == 409
    assert "posted_at" in res.json()["detail"]                 # names the existing axis
    assert _node(conn, "public.balances.acct_id") == (None, None, None, None, False)


def test_invalid_additivity_rejected_and_nothing_written(client, conn):
    upload_csv(client, "ledger", LEDGER_CSV)
    ref = "public.balances.balance"
    res = _complete(client, ref, {"additivity": "sometimes", "unit": "dollars"})
    assert res.status_code == 422
    assert "unrecognized additivity 'sometimes'" in res.json()["detail"]
    assert _node(conn, ref) == (None, None, None, None, False)   # unit NOT partially applied
    assert ref in {i["object_ref"] for i in _pending(client)}


def test_invalid_as_of_basis_rejected(client, conn):
    upload_csv(client, "ledger", LEDGER_CSV)
    res = _complete(client, "public.rates.rate_date", {"is_as_of": True, "as_of_basis": "whenever"})
    assert res.status_code == 422
    assert "unrecognized as_of_basis 'whenever'" in res.json()["detail"]
    assert _node(conn, "public.rates.rate_date") == (None, None, None, None, False)


def test_empty_or_blank_body_rejected(client):
    upload_csv(client, "ledger", LEDGER_CSV)
    assert _complete(client, "public.balances.balance", {}).status_code == 422
    assert _complete(client, "public.balances.balance", {"entity": "  "}).status_code == 422


def test_unknown_column_ref_404(client):
    upload_csv(client, "ledger", LEDGER_CSV)
    res = _complete(client, "public.balances.no_such_col", {"entity": "Account"})
    assert res.status_code == 404


def test_completion_is_recorded_on_the_audit_chain(client, conn):
    upload_csv(client, "ledger", LEDGER_CSV)
    _complete(client, "public.balances.balance",
              {"additivity": "additive", "entity": "Account"})
    row = conn.execute(
        "SELECT decision, attempted_action, aggregate_id, reason FROM security_audit "
        "WHERE event_type = 'SEMANTICS_COMPLETED'").fetchone()
    assert row is not None
    decision, action, aggregate_id, reason = row
    # 'flagged' is the chain's decision for a successful action recorded as evidence (its
    # closed vocabulary is denied | allowed_break_glass | flagged).
    assert decision == "flagged"
    assert "public.balances.balance" in action
    assert aggregate_id == "ledger:public.balances.balance"
    assert "additivity=additive" in reason and "entity=Account" in reason


def test_complete_requires_catalog_write(client, conn):
    upload_csv(client, "ledger", LEDGER_CSV)
    res = _complete(client, "public.balances.balance", {"entity": "Account"}, headers=VIEWER)
    assert res.status_code == 403
    assert _node(conn, "public.balances.balance") == (None, None, None, None, False)


def test_complete_is_read_scoped_hidden_pii_column_404(client, conn):
    """Audit finding [7]: complete_semantics resolves the node UNDER the actor's sensitivity scope.
    A caller with catalog:write but NO pii_reader gets a 404 (hidden == missing, no existence
    oracle) and writes NOTHING on a pii column; a pii_reader sees it and can complete it — matching
    the read-scoped semantics-pending queue and the F3 field-correction gate."""
    upload_csv(client, "hr", HR_CSV)
    ref = "public.people.ssn"                          # sensitivity='pii' (HR_CSV)
    # AUTH is platform_admin WITHOUT pii_reader: the hidden pii column is a 404, nothing written.
    res = _complete(client, ref, {"entity": "Person"}, source="hr", headers=AUTH)
    assert res.status_code == 404
    assert _node(conn, ref, source="hr")[3] is None    # entity un-written (hidden == missing)
    # A pii_reader sees the column and can complete its semantics.
    res = _complete(client, ref, {"entity": "Person"}, source="hr", headers=PII_AUTH)
    assert res.status_code == 200
    assert res.json()["applied"] == {"entity": "Person"}
    assert _node(conn, ref, source="hr")[3] == "Person"

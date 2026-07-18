"""Slice 3a-iii Task 1 — `_candidate_columns` widened with feature-correctness fields + table-node
context (single scoped query). The thin `_menu` projection stays byte-identical: nothing new
egresses until the flag-gated enrichment (Task 2)."""
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import _candidate_columns, _menu
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    rows = [
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount",
                     additivity="additive", unit="dollars", currency="USD", entity="Account"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET declared_type='numeric', semantic_terms='payment amount' "
               "WHERE object_ref='public.transactions.amount'")
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")
    db.execute("UPDATE graph_node SET availability_fact_event_id='fe_avail1' "
               "WHERE object_ref='public.transactions.txn_date'")
    db.execute("UPDATE graph_node SET definition='Accounts master', primary_entity='Account' "
               "WHERE kind='table' AND table_name='accounts'")


def test_candidate_columns_carries_feature_correctness_and_table_fields(db):
    _bank_graph(db)
    cols = _candidate_columns(db, "bank", roles=())
    by_ref = {c["object_ref"]: c for c in cols}
    amount = by_ref["public.transactions.amount"]
    assert amount["declared_type"] == "numeric"
    assert amount["semantic_terms"] == "payment amount"
    assert amount["additivity"] == "additive"
    assert amount["unit"] == "dollars"
    assert amount["currency"] == "USD"
    assert amount["entity"] == "Account"
    assert amount["is_grain"] is False
    acct = by_ref["public.accounts.account_id"]
    assert acct["is_grain"] is True
    assert acct["grain_fact_event_id"] == "fe_grain1"
    assert acct["table_definition"] == "Accounts master"
    assert acct["table_primary_entity"] == "Account"
    txn_date = by_ref["public.transactions.txn_date"]
    assert txn_date["is_as_of"] is True
    assert txn_date["availability_fact_event_id"] == "fe_avail1"


def test_read_scope_filter_still_excludes_restricted_columns(db):
    # The widened SELECT must not widen the authorization surface: a sensitivity-tagged column is
    # still excluded for a caller without the granting role, and visible with it.
    _bank_graph(db)
    db.execute("UPDATE graph_node SET sensitivity='restricted' "
               "WHERE object_ref='public.transactions.amount'")
    unprivileged = {c["object_ref"] for c in _candidate_columns(db, "bank", roles=())}
    assert "public.transactions.amount" not in unprivileged
    assert "public.accounts.account_id" in unprivileged
    privileged = {c["object_ref"]
                  for c in _candidate_columns(db, "bank", roles=("restricted_reader",))}
    assert "public.transactions.amount" in privileged


def test_thin_menu_unchanged_after_widening(db):
    _bank_graph(db)
    cols = _candidate_columns(db, "bank", roles=())
    menu = _menu(cols)
    # The thin menu still projects EXACTLY the five structural keys — flag-off byte-identity.
    assert all(set(m.keys()) == {"object_ref", "table", "column", "concept", "domain"}
               for m in menu)
    amount = next(m for m in menu if m["object_ref"] == "public.transactions.amount")
    assert amount == {"object_ref": "public.transactions.amount", "table": "transactions",
                      "column": "amount", "concept": None, "domain": None}

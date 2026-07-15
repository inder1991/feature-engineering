from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph, column_joins


def test_join_edge_resolved_when_target_present(db):
    rows = [
        CanonicalRow("deposits", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("deposits", "accounts", "account_id", "integer", is_grain=True),
    ]
    build_graph(db, "deposits", rows)
    joins = column_joins(db, "deposits", "public.transactions.acct_id")
    assert len(joins) == 1
    assert joins[0].to_ref == "public.accounts.account_id"
    assert joins[0].cardinality == "N:1"
    assert joins[0].resolved is True


def test_join_edge_pending_when_target_absent(db):
    rows = [CanonicalRow("deposits", "transactions", "acct_id", "integer",
                         joins_to="accounts.account_id", cardinality="N:1")]
    build_graph(db, "deposits", rows)
    joins = column_joins(db, "deposits", "public.transactions.acct_id")
    assert len(joins) == 1
    assert joins[0].resolved is False   # target not loaded -> pending, but the edge is recorded


def test_column_joins_read_scopes_a_sensitive_source(db):
    # CONFIDENTIALITY (#11): a pii-tagged SOURCE column's join endpoints are metadata about where
    # pii lives. A caller without pii_reader who knows the object_ref must NOT retrieve its joins;
    # a pii_reader sees them. Mirrors the existing TARGET-side filter.
    rows = [
        CanonicalRow("bank", "customers", "ssn", "text", sensitivity="pii",
                     joins_to="identities.ssn", cardinality="N:1"),
        CanonicalRow("bank", "identities", "ssn", "text", is_grain=True),
    ]
    build_graph(db, "bank", rows)
    assert column_joins(db, "bank", "public.customers.ssn") == []   # no pii_reader -> withheld
    joins = column_joins(db, "bank", "public.customers.ssn", roles=("pii_reader",))
    assert len(joins) == 1 and joins[0].to_ref == "public.identities.ssn"


def test_csv_reads_join_columns(db):
    from featuregen.overlay.upload.csv_reader import read_csv_rows
    text = ("table,column,type,FK Target,Cardinality\n"
            "transactions,acct_id,integer,accounts.account_id,N:1\n")
    rows = read_csv_rows(text, source="deposits")
    assert rows[0].joins_to == "accounts.account_id"
    assert rows[0].cardinality == "N:1"

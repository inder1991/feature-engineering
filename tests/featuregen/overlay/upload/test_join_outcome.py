"""Slice 3a-i Task 3: `JoinOutcome` discriminated result + `classify_join_path` (per-hop
classification), with `find_join_path` staying a byte-identical `list[JoinStep] | None` façade.

RF-I1 (binding): "authorized-but-unverified" fact-linked edges use status 'DRAFT' — a value in the
`graph_edge_approved_join_status_check` vocabulary (DRAFT/PARTIALLY_CONFIRMED/VERIFIED/REJECTED/
STALE/REVERIFY). Never 'PROPOSED' (not a folded status; violates the CHECK).
"""
from featuregen.overlay.upload.join_path import (
    JoinOutcome,
    JoinStep,
    classify_join_path,
    find_join_path,
)

_SRC = "bank"


def _col(db, ref, table, column, *, sensitivity=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "sensitivity) VALUES (%s, %s, 'column', %s, %s, %s)",
        (_SRC, ref, table, column, sensitivity))


def _edge(db, from_ref, to_ref, *, fact_key=None, status=None, authority="operational"):
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) VALUES (%s, 'joins', %s, %s, 'N:1', %s, %s, %s)",
        (_SRC, from_ref, to_ref, authority, fact_key, status))


def _seed_txn_accounts(db):
    _col(db, "public.transactions.acct_id", "transactions", "acct_id")
    _col(db, "public.accounts.account_id", "accounts", "account_id")


def test_same_table_is_operational_with_no_steps(db):
    out = classify_join_path(db, _SRC, "accounts", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL
    assert out.steps == ()
    assert out.clears is True


def test_declared_edge_is_operational(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")   # fact_key NULL = declared
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL
    assert out.clears is True
    assert [(s.from_ref, s.to_ref) for s in out.steps] == \
        [("public.transactions.acct_id", "public.accounts.account_id")]


def test_verified_fact_linked_edge_is_operational(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-1", status="VERIFIED")
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL


def test_unverified_fact_linked_edge_is_unverified_with_endpoints_and_fact_keys(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-9", status="DRAFT")   # authorized but NOT verified (RF-I1: in-vocab status)
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.UNVERIFIED
    assert out.clears is False
    assert out.endpoints == (("public.transactions.acct_id", "public.accounts.account_id"),)
    assert out.fact_keys == ("ajf-9",)


def test_no_edge_is_no_path(db):
    _seed_txn_accounts(db)
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.NO_PATH
    assert out.clears is False


def test_read_scope_hidden_hop_is_denied(db):
    _col(db, "public.transactions.acct_id", "transactions", "acct_id")
    _col(db, "public.accounts.account_id", "accounts", "account_id", sensitivity="pii")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")
    # roles=() cannot see pii -> the only hop is hidden -> DENIED (not NO_PATH)
    out = classify_join_path(db, _SRC, "transactions", "accounts", roles=())
    assert out.kind == JoinOutcome.DENIED
    assert out.endpoints == (("public.transactions.acct_id", "public.accounts.account_id"),)
    # with the clearing role the same edge classifies OPERATIONAL
    ok = classify_join_path(db, _SRC, "transactions", "accounts", roles=("pii_reader",))
    assert ok.kind == JoinOutcome.OPERATIONAL


def test_find_join_path_backcompat_operational_returns_steps_else_none(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")
    assert find_join_path(db, _SRC, "transactions", "accounts") == \
        [JoinStep("public.transactions.acct_id", "public.accounts.account_id", "N:1")]
    # an unverified-only edge is NOT operational -> find_join_path collapses to None (unchanged
    # contract). RF-I1: 'DRAFT' is the in-vocab authorized-but-unverified status, never 'PROPOSED'.
    db.execute("UPDATE graph_edge SET approved_join_fact_key = 'ajf-9', "
               "approved_join_status = 'DRAFT'")
    assert find_join_path(db, _SRC, "transactions", "accounts") is None

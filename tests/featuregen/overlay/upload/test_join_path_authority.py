"""Spec A Task 3 (half a): `classify_join_path` must RETAIN a step's join authority.

The planner's SQL already selects `approved_join_fact_key` and `approved_join_status`, but the
clearing tuple dropped both, so an OPERATIONAL path came back with no evidence of WHY it cleared —
a governed VERIFIED join and an ungoverned file-declared edge were indistinguishable downstream.
Materialization has to record which governed fact authorized each hop, and reconstructing that with
a SECOND read of `graph_edge` could disagree with the read that planned the path (the two would be
separate snapshots of a table the projection mutates). So the provenance travels INSIDE the
existing query and the existing tuple flow.

The load-bearing case is the REVERSE edge. `_adjacency` synthesizes a reverse traversal for every
edge and inverts its cardinality ("a reverse N:1 hop is really 1:N"). Both facts must survive that
inversion together: cardinality decides whether a hop multiplies rows, and the fact key decides
whether anyone approved it. A reverse hop that kept the inverted cardinality but lost the fact key
(or vice versa) would be exactly as wrong as dropping it on the forward edge — and harder to see.
"""
from featuregen.overlay.upload.join_path import JoinOutcome, classify_join_path, find_join_path

_SRC = "bank"


def _col(db, ref, table, column, *, sensitivity=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "sensitivity) VALUES (%s, %s, 'column', %s, %s, %s)",
        (_SRC, ref, table, column, sensitivity))


def _edge(db, from_ref, to_ref, *, cardinality="N:1", fact_key=None, status=None,
          authority="operational"):
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, %s, %s, %s, %s)",
        (_SRC, from_ref, to_ref, cardinality, authority, fact_key, status))


def _seed_txn_accounts(db):
    _col(db, "public.transactions.acct_id", "transactions", "acct_id")
    _col(db, "public.accounts.account_id", "accounts", "account_id")


def test_operational_step_carries_the_governed_fact_key_and_status(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-verified-1", status="VERIFIED")
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL
    (step,) = out.steps
    assert step.approved_join_fact_key == "ajf-verified-1"
    assert step.approved_join_status == "VERIFIED"
    assert step.authority == "operational"


def test_file_declared_step_is_operational_authority_with_no_fact_key(db):
    """A file-declared edge clears the join check WITHOUT a governed fact. It must say so — a
    `None` fact key here is the honest answer, and it is what distinguishes an ungoverned edge
    from a VERIFIED one now that both reach the same OPERATIONAL outcome."""
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")   # fact_key NULL
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    (step,) = out.steps
    assert step.approved_join_fact_key is None
    assert step.approved_join_status is None
    assert step.authority == "operational"


def test_reverse_edge_keeps_provenance_AND_inverts_cardinality(db):
    """The stored edge points accounts -> transactions; the traversal runs the other way. The
    returned step must carry the INVERTED cardinality and the SAME governed fact key."""
    _seed_txn_accounts(db)
    _edge(db, "public.accounts.account_id", "public.transactions.acct_id",
          cardinality="1:N", fact_key="ajf-reverse", status="VERIFIED")
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL
    (step,) = out.steps
    assert (step.from_ref, step.to_ref) == \
        ("public.transactions.acct_id", "public.accounts.account_id")
    assert step.cardinality == "N:1"                      # inverted for the reverse traversal
    assert step.approved_join_fact_key == "ajf-reverse"   # provenance survived the inversion
    assert step.approved_join_status == "VERIFIED"


def test_forward_and_reverse_traversals_of_one_edge_agree_on_provenance(db):
    """Same edge, both directions: cardinality flips, authority does not."""
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          cardinality="N:1", fact_key="ajf-both", status="VERIFIED")
    fwd, = classify_join_path(db, _SRC, "transactions", "accounts").steps
    rev, = classify_join_path(db, _SRC, "accounts", "transactions").steps
    assert (fwd.cardinality, rev.cardinality) == ("N:1", "1:N")
    assert fwd.approved_join_fact_key == rev.approved_join_fact_key == "ajf-both"
    assert fwd.approved_join_status == rev.approved_join_status == "VERIFIED"


def test_each_hop_of_a_multi_hop_path_carries_its_OWN_fact(db):
    """Provenance is per hop, not per path: a path that mixes a governed hop with a file-declared
    one must report exactly that, or a reviewer cannot tell which hop is unapproved."""
    _col(db, "public.transactions.acct_id", "transactions", "acct_id")
    _col(db, "public.accounts.account_id", "accounts", "account_id")
    _col(db, "public.accounts.cif_id", "accounts", "cif_id")
    _col(db, "public.customers.cif_id", "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-hop-1", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id")           # file-declared
    out = classify_join_path(db, _SRC, "transactions", "customers")
    assert out.kind == JoinOutcome.OPERATIONAL
    assert [(s.approved_join_fact_key, s.approved_join_status) for s in out.steps] == \
        [("ajf-hop-1", "VERIFIED"), (None, None)]


def test_unverified_path_steps_also_carry_their_status(db):
    """The UNVERIFIED outcome already named its fact keys at the OUTCOME level; the steps now say
    which hop each one belongs to."""
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-draft", status="DRAFT")
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.UNVERIFIED
    (step,) = out.steps
    assert step.approved_join_fact_key == "ajf-draft"
    assert step.approved_join_status == "DRAFT"


def test_the_facade_is_unchanged_for_callers_that_ignore_the_new_fields(db):
    """`find_join_path` keeps returning the same steps for the same graph — the extension is
    additive, so existing callers (api/routes/graph.py, feature_assist, contract/author) see the
    fields they already read, unchanged."""
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-facade", status="VERIFIED")
    path = find_join_path(db, _SRC, "transactions", "accounts")
    assert path is not None
    assert [(s.from_ref, s.to_ref, s.cardinality) for s in path] == \
        [("public.transactions.acct_id", "public.accounts.account_id", "N:1")]

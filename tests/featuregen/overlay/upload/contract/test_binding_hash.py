"""Delivery H1b — the confirmed role-binding hash + its fold into metadata_input_fingerprint.

Unit-level coverage of the deterministic, contract-INDEPENDENT ``binding_hash`` (reused for the /draft
exposure and the confirm-time 409 gate) and the ``confirm_contract`` fold — the HTTP gate wiring lives
in tests/featuregen/api/test_binding_confirmation.py.
"""
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    binding_exposure,
    binding_hash,
    confirm_contract,
    confirmed_role_bindings,
)
from featuregen.overlay.upload.graph import build_graph


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])


def _draft():
    return ContractDraft("avg_balance_90d", "Average 90-day ledger balance.", "accounts",
                         "avg_90d", "posted_at", ["public.accounts.balance"],
                         derives_pairs=(("bank", "public.accounts.balance"),))


def test_bindings_expose_role_ref_source_authority_warnings(db):
    _bank(db)
    bindings = confirmed_role_bindings(db, _draft())
    exposed = binding_exposure(bindings)
    assert exposed, "expected role bindings"
    for b in exposed:
        assert set(b) == {"role", "ref", "source", "authority", "warnings"}
    roles = {b["role"] for b in exposed}
    assert {"derives", "grain", "as_of"} <= roles
    assert all(isinstance(b["authority"], str) for b in exposed)


def test_binding_hash_is_deterministic_and_contract_independent(db):
    _bank(db)
    # SAME reconciled bindings ⟹ SAME hash across repeated computes (no contract_id in the input).
    h1 = binding_hash(confirmed_role_bindings(db, _draft()))
    h2 = binding_hash(confirmed_role_bindings(db, _draft()))
    assert h1 and h1 == h2


def test_binding_hash_changes_when_a_binding_column_retyped(db):
    _bank(db)
    before = binding_hash(confirmed_role_bindings(db, _draft()))
    db.execute("UPDATE graph_node SET declared_type = 'text', data_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")
    after = binding_hash(confirmed_role_bindings(db, _draft()))
    assert before != after, "a retyped binding must move the binding_hash (drift → 409)"


def test_binding_hash_changes_when_as_of_fact_retired(db):
    _bank(db)
    before = binding_hash(confirmed_role_bindings(db, _draft()))
    db.execute("UPDATE graph_node SET is_as_of = false "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.posted_at'")
    after = binding_hash(confirmed_role_bindings(db, _draft()))
    assert before != after, "a retired as_of fact must move the binding_hash (revalidation)"


def test_confirm_folds_binding_hash_into_metadata_input_fingerprint(db):
    _bank(db)
    bh = binding_hash(confirmed_role_bindings(db, _draft()))
    c = confirm_contract(db, _draft(), actor="ds1", confirmed_binding_hash=bh)
    folded = db.execute("SELECT metadata_input_fingerprint FROM contract WHERE contract_id = %s",
                        (c.contract_id,)).fetchone()[0]
    assert folded is not None
    # a plain confirm (no binding_hash) yields a DIFFERENT fingerprint — the fold genuinely composes it.
    c2 = confirm_contract(db, _draft(), actor="ds1")   # re-confirm, no binding_hash
    plain = db.execute("SELECT metadata_input_fingerprint FROM contract WHERE contract_id = %s",
                       (c2.contract_id,)).fetchone()[0]
    assert folded != plain

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.entity import entity_key_columns, entity_of, list_entities
from featuregen.overlay.upload.graph import build_graph


def _two_catalogs(db):
    # deposits and cards each have a Customer key column — different names, same entity.
    build_graph(db, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric")])
    build_graph(db, "cards", [
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer"),
        CanonicalRow("cards", "card_accounts", "spend", "numeric")])


def test_entity_key_columns_span_catalogs(db):
    _two_catalogs(db)
    keys = entity_key_columns(db, "Customer")
    refs = {(k.catalog_source, k.object_ref) for k in keys}
    assert ("deposits", "public.accounts.cust_ref") in refs
    assert ("cards", "public.card_accounts.cust_id") in refs   # cross-catalog membership
    assert all(k.entity == "Customer" for k in keys)


def test_list_entities_and_entity_of(db):
    _two_catalogs(db)
    assert "Customer" in list_entities(db)
    assert entity_of(db, "deposits", "public.accounts.cust_ref") == "Customer"
    assert entity_of(db, "deposits", "public.accounts.balance") is None


def test_cross_join_via_entity_bridges_two_catalogs(db):
    from featuregen.overlay.upload.entity import cross_join_via_entity
    _two_catalogs(db)
    bridge = cross_join_via_entity(db, "deposits", "accounts", "cards", "card_accounts")
    assert bridge is not None
    assert bridge.entity == "Customer"
    assert bridge.from_ref == "public.accounts.cust_ref"
    assert bridge.to_ref == "public.card_accounts.cust_id"


def test_cross_join_none_when_no_shared_entity(db):
    from featuregen.overlay.upload.entity import cross_join_via_entity
    _two_catalogs(db)
    build_graph(db, "loans", [CanonicalRow("loans", "loan_accounts", "loan_id", "integer")])  # no entity
    assert cross_join_via_entity(db, "deposits", "accounts", "loans", "loan_accounts") is None


def test_suggest_entity_advisory(db):
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.entity import suggest_entity
    ok = FakeLLM(script={"overlay.enrich.entity": FakeResponse(output={"entity": "Customer"})})
    assert suggest_entity(db, ok, table="accounts", column="cust_ref", type="integer") == "Customer"
    # empty / implausible suggestion is not applied
    empty = FakeLLM(script={"overlay.enrich.entity": FakeResponse(output={"entity": ""})})
    assert suggest_entity(db, empty, table="accounts", column="balance", type="numeric") is None
    listish = FakeLLM(script={"overlay.enrich.entity": FakeResponse(output={"entity": "['a','b']"})})
    assert suggest_entity(db, listish, table="accounts", column="x", type="text") is None


def test_cross_catalog_path_joins_then_entity_bridge(db):
    from featuregen.overlay.upload.entity import find_cross_catalog_path
    build_graph(db, "cards", [
        CanonicalRow("cards", "transactions", "acct_id", "integer",
                     joins_to="card_accounts.card_id", cardinality="N:1"),
        CanonicalRow("cards", "card_accounts", "card_id", "integer", is_grain=True),
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer")])
    build_graph(db, "deposits", [
        CanonicalRow("deposits", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer")])
    # cards.transactions --join--> cards.card_accounts --entity(Customer)--> deposits.accounts
    path = find_cross_catalog_path(db, "cards", "transactions", "deposits", "accounts")
    assert path is not None and len(path) == 2
    assert path[0].kind == "join"
    assert path[1].kind == "entity" and path[1].detail == "Customer"
    assert find_cross_catalog_path(db, "cards", "transactions", "deposits", "nowhere") is None


def test_suggest_then_governed_apply_proposes_not_legacy(db, catalog):
    """E4: `apply_entity_suggestion` now PROPOSES a governed entity_assignment DRAFT (E1) instead of
    the retired legacy status='applied' UPDATE — the graph is NOT written until a distinct human
    confirms (four-eyes) and E3 projects. The suggestion stays pending (never marked 'applied')."""
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.entity import (
        apply_entity_suggestion,
        list_entity_suggestions,
        suggest_entities,
    )
    from featuregen.overlay.upload.graph import build_graph
    rows = [CanonicalRow("deposits", "accounts", "cust_ref", "integer"),   # id-like, no entity
            CanonicalRow("deposits", "accounts", "balance", "numeric")]    # not id-like -> skipped
    build_graph(db, "deposits", rows)
    client = FakeLLM(script={"overlay.enrich.entity": FakeResponse(output={"entity": "Customer"})})
    assert suggest_entities(db, client, "deposits") == 1
    sugg = list_entity_suggestions(db, "deposits")
    assert len(sugg) == 1 and sugg[0].column == "cust_ref" and sugg[0].suggested_entity == "Customer"
    # E4 apply -> a governed entity_assignment DRAFT is proposed, NOT written to the graph
    res = apply_entity_suggestion(db, "deposits", sugg[0].object_ref,
                                  actor=mint_test_identity(subject="user:alice",
                                                           role_claims=("data_owner",)))
    assert res.found and res.accepted and res.fact_key
    assert fold_overlay_state(load_fact(db, res.fact_key)).status == "DRAFT"   # never auto-verified
    q = "SELECT entity FROM graph_node WHERE catalog_source='deposits' AND object_ref=%s"
    assert db.execute(q, (sugg[0].object_ref,)).fetchone()[0] is None   # graph NOT written on apply
    # the suggestion is NOT marked 'applied' (legacy UPDATE retired); it stays pending
    assert db.execute("SELECT status FROM entity_suggestion WHERE catalog_source='deposits' "
                      "AND object_ref=%s", (sugg[0].object_ref,)).fetchone()[0] == "pending"
    # idempotent: a re-apply while the DRAFT is non-terminal is denied by propose_fact
    again = apply_entity_suggestion(db, "deposits", sugg[0].object_ref,
                                    actor=mint_test_identity(subject="user:alice",
                                                             role_claims=("data_owner",)))
    assert again.found and not again.accepted

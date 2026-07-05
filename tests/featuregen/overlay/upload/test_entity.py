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

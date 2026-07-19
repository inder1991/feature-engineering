"""Phase 3 — contract authoring: deterministic facts + LLM-authored definition (audited)."""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import draft_contract
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.graph import build_graph


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric",
                     definition="end-of-day ledger balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])


def test_draft_contract_grounds_facts_and_authors_definition(db):
    _bank(db)
    feature = FeatureIdea(name="avg_balance_90d", description="", aggregation="avg_90d",
                          grain_table="accounts", derives_from=["public.accounts.balance"],
                          derives_pairs=(("bank", "public.accounts.balance"),))
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={
        "definition": "Average end-of-day ledger balance per account over the trailing 90 days."})})

    draft = draft_contract(db, feature, client)
    # LLM authored the narrative...
    assert draft.definition.startswith("Average end-of-day ledger balance")
    # ...structured facts are deterministic from the feature + catalog (not from the LLM)
    assert draft.grain_table == "accounts"
    assert draft.aggregation == "avg_90d"
    assert draft.as_of_column == "posted_at"          # the grain table's as-of column
    assert draft.derives_from == ["public.accounts.balance"]


def test_draft_records_an_audited_llm_call(db):
    _bank(db)
    feature = FeatureIdea(name="avg_balance_90d", description="", aggregation="avg_90d",
                          grain_table="accounts", derives_from=["public.accounts.balance"],
                          derives_pairs=(("bank", "public.accounts.balance"),))
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})
    before = db.execute("SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    draft_contract(db, feature, client)
    after = db.execute("SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert after == before + 1                        # the authoring call is audited


def test_authoring_respects_read_scope(db):
    # M1: a restricted column's definition must NOT be sent to the LLM
    import json
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", definition="ledger balance"),
        CanonicalRow("bank", "accounts", "ssn", "text", sensitivity="restricted",
                     definition="social security number"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])
    feature = FeatureIdea("f", "", ["public.accounts.balance", "public.accounts.ssn"],
                          "avg_90d", "accounts",
                          derives_pairs=(("bank", "public.accounts.balance"),
                                         ("bank", "public.accounts.ssn")))
    captured = {}

    class _Cap:
        def call(self, req):
            captured["inputs"] = req.inputs
            from featuregen.intake.llm import LLMResult
            return LLMResult(output={"definition": "x"}, self_reported_scores={}, call_ref="", status="ok")

    draft_contract(db, feature, _Cap(), roles=())          # caller has no restricted access
    blob = json.dumps(captured["inputs"], default=str)
    assert "ledger balance" in blob                        # allowed column's definition present
    assert "social security number" not in blob            # restricted column's definition withheld


def test_draft_authors_the_join_path(db):
    # B3 follow-on: the deterministic join path (grain -> derived table) is authored onto the draft
    build_graph(db, "bank", [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "transactions", "amount", "numeric"),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True)])
    feature = FeatureIdea("txn_count", "", ["public.transactions.amount"], "count", "accounts",
                          derives_pairs=(("bank", "public.transactions.amount"),))
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})
    draft = draft_contract(db, feature, client)
    assert draft.join_path                                  # a join step from accounts <-> transactions
    step = draft.join_path[0]
    assert step["from"] and step["to"] and "accounts" in (step["from"] + step["to"])


def test_draft_authors_cross_catalog_join_path_via_entity(db):
    # 3C.2a flag-off (is_live default False): byte-identical to pre-3C.2a — a feature spanning two catalogs
    # with no governed envelope gets an entity-bridged permissive path via find_cross_catalog_path. The
    # flag-on fail-closed (CrossCatalogPlanRequired) path is covered in test_draft_rebinding.
    build_graph(db, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric")])
    build_graph(db, "cards", [
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer"),
        CanonicalRow("cards", "card_accounts", "spend", "numeric")])
    feature = FeatureIdea("cross", "", ["public.accounts.balance", "public.card_accounts.spend"],
                          "avg", "accounts",
                          derives_pairs=(("deposits", "public.accounts.balance"),
                                         ("cards", "public.card_accounts.spend")))
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})
    draft = draft_contract(db, feature, client)
    assert any(step.get("kind") == "entity" and step.get("via") == "Customer"
               for step in draft.join_path)   # accounts --entity(Customer)--> card_accounts

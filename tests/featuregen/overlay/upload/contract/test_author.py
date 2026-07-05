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
                          grain_table="accounts", derives_from=["public.accounts.balance"])
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
                          grain_table="accounts", derives_from=["public.accounts.balance"])
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})
    before = db.execute("SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    draft_contract(db, feature, client)
    after = db.execute("SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert after == before + 1                        # the authoring call is audited

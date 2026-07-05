from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import (
    feature_recipe,
    leakage_check,
    recommend_features,
)
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    rows = [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),  # point-in-time
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "churned", "boolean", definition="customer churned flag"),
    ]
    build_graph(db, "bank", rows)


def test_recommend_features_grounds_out_hallucinations(db):
    _bank_graph(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "txn_count_90d", "description": "count of txns",
         "derives_from": ["public.transactions.amount"], "aggregation": "count_90d",
         "grain_table": "accounts"},
        {"name": "ghost", "description": "uses a column that doesn't exist",
         "derives_from": ["public.transactions.nonexistent"]},   # hallucinated -> dropped
    ]})})
    ideas = recommend_features(db, "predict churn", client, catalog_source="bank")
    assert len(ideas) == 1
    assert ideas[0].name == "txn_count_90d"
    assert ideas[0].derives_from == ["public.transactions.amount"]


def test_feature_recipe_pairs_llm_intent_with_deterministic_join_path(db):
    _bank_graph(db)
    client = FakeLLM(script={"overlay.feature.recipe": FakeResponse(output={
        "grain_table": "accounts", "join_table": "transactions",
        "derives_from": ["public.transactions.amount"],
        "aggregation": "sum_90d", "as_of_column": "posted_at"})})
    recipe = feature_recipe(db, "total spend per account last 90 days", client, catalog_source="bank")
    assert recipe.grain_table == "accounts"
    assert recipe.derives_from == ["public.transactions.amount"]
    # the join path is real (found deterministically), not invented by the LLM
    assert len(recipe.join_path) == 1
    step = recipe.join_path[0]
    # Traversal accounts -> transactions is the REVERSE of the stored transactions->accounts N:1,
    # so oriented to the traversal it is 1:N (one account fans out to many transactions) and the
    # step reads forward from the grain (M7).
    assert step.cardinality == "1:N"
    assert step.from_ref == "public.accounts.account_id"
    assert step.to_ref == "public.transactions.acct_id"


def test_leakage_check_flags_target_derived_column(db):
    _bank_graph(db)
    derives = ["public.accounts.churned", "public.transactions.amount"]
    client = FakeLLM(script={"overlay.feature.leakage": FakeResponse(output={"leaks": [
        {"object_ref": "public.accounts.churned", "reason": "looks like the target label"},
        {"object_ref": "public.not.used", "reason": "not in derives_from -> ignored"},
    ]})})
    warnings = leakage_check(db, derives, "public.accounts.churned", client)
    assert len(warnings) == 1
    assert warnings[0].object_ref == "public.accounts.churned"


def test_recommend_and_recipe_respect_read_scope(db):
    """M6: a PII column must NOT be fed to the LLM (or returned) without the role."""
    from featuregen.overlay.upload.canonical import CanonicalRow
    rows = [
        CanonicalRow("bank", "accounts", "balance", "numeric", definition="ledger balance"),
        CanonicalRow("bank", "accounts", "ssn", "text", sensitivity="pii", definition="customer SSN"),
    ]
    build_graph(db, "bank", rows)

    captured = {}

    class _CaptureLLM:
        def call(self, request):
            captured["columns"] = request.inputs.get("columns", [])
            from featuregen.intake.llm import LLMResult
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="", status="ok")

    # No role -> the PII column is not among the candidate columns sent to the LLM.
    recommend_features(db, "predict risk", _CaptureLLM(), catalog_source="bank")
    refs = {c["object_ref"] for c in captured["columns"]}
    assert "public.accounts.balance" in refs
    assert "public.accounts.ssn" not in refs

    # With the pii_reader role -> the PII column is available.
    recommend_features(db, "predict risk", _CaptureLLM(), catalog_source="bank", roles={"pii_reader"})
    refs2 = {c["object_ref"] for c in captured["columns"]}
    assert "public.accounts.ssn" in refs2

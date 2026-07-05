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
    assert recipe.join_path[0].cardinality == "N:1"


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

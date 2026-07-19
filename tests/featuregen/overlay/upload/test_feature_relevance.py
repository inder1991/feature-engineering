import pytest

import featuregen.overlay.upload.feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope


def test_tokenize_and_objective_source_priority():
    assert fa._tokenize("Churn, 30-day!") == {"churn", "30", "day"}
    scope = ConfirmedScope(primary="retail_churn", secondary=("deposit_growth",),
                           target_entity="Account", modelling_contexts=("ifrs9",))
    # Governed route derives tokens from the scope, NOT the (unrelated) objective string.
    gov = fa._objective_tokens("weather forecast", None, scope)
    assert {"retail", "churn", "deposit", "growth", "account", "ifrs9"} <= gov
    assert "weather" not in gov
    # Direct-assist route: objective free text + explicit entity.
    assert fa._objective_tokens("predict churn", "Account", None) == {"predict", "churn", "account"}
    # Lexical fallback: objective only.
    assert fa._objective_tokens("predict churn", None, None) == {"predict", "churn"}
    # unscoped governed scope falls through to assist/lexical.
    uns = ConfirmedScope(primary=None, unscoped=True)
    assert fa._objective_tokens("predict churn", None, uns) == {"predict", "churn"}


def _seed(db):
    rows = [
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "churn_flag", "boolean", definition="churn label"),
        CanonicalRow("bank", "accounts", "region", "text", definition="branch region"),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")


def test_mandatory_grain_always_selected_and_scoring_order(db):
    _seed(db)
    cols = fa._candidate_columns(db, "bank", roles=())
    selected, _ctx, dropped = fa.select_relevant_context(
        db, cols, objective="predict churn", entity=None, scope=None)
    refs = [c["object_ref"] for c in selected]
    assert dropped == 0
    assert "public.accounts.account_id" in refs                 # grain is mandatory
    # churn_flag shares the token 'churn' with the objective -> ranked above region.
    assert refs.index("public.accounts.churn_flag") < refs.index("public.accounts.region")


def test_byte_budget_drops_lowest_scored_and_counts(db, monkeypatch):
    _seed(db)
    cols = fa._candidate_columns(db, "bank", roles=())
    # Budget large enough for mandatory + the single highest-scored optional, not the rest.
    mand = fa.select_relevant_context(db, [c for c in cols
                                           if c["object_ref"] == "public.accounts.account_id"],
                                      objective="predict churn", entity=None, scope=None)[0]
    one_sel, one_ctx, _ = fa.select_relevant_context(
        db, [c for c in cols if c["object_ref"] in
             ("public.accounts.account_id", "public.accounts.churn_flag")],
        objective="predict churn", entity=None, scope=None)
    # RF-I5: the budget must INCLUDE the per-table table_context grain block the selection
    # always assembles for the mandatory grain column — not an empty context.
    budget = fa._assembled_bytes(one_sel, one_ctx)
    monkeypatch.setattr(fa, "FEATURE_CONTEXT_BYTE_BUDGET", budget)
    selected, _ctx, dropped = fa.select_relevant_context(
        db, cols, objective="predict churn", entity=None, scope=None)
    refs = [c["object_ref"] for c in selected]
    assert "public.accounts.account_id" in refs and "public.accounts.churn_flag" in refs
    assert "public.accounts.region" not in refs
    assert dropped == 1
    assert len(mand) == 1  # sanity: the mandatory-only assembly had exactly the grain column


def test_overflow_raises_context_too_large_not_chunk(db, monkeypatch):
    _seed(db)
    cols = fa._candidate_columns(db, "bank", roles=())
    monkeypatch.setattr(fa, "FEATURE_CONTEXT_BYTE_BUDGET", 5)  # smaller than mandatory alone
    with pytest.raises(fa.ContextTooLarge):
        fa.select_relevant_context(db, cols, objective="predict churn", entity=None, scope=None)
    assert fa.RejectCode.CONTEXT_TOO_LARGE == "CONTEXT_TOO_LARGE"

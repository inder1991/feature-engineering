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


def _capture_client(captured):
    from featuregen.intake.llm import LLMResult

    class _CaptureLLM:
        def call(self, request):
            captured.append(dict(request.inputs.get("catalog_metadata", {})))
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="",
                             status="ok")

    return _CaptureLLM()


def test_flag_off_menu_byte_identical(db, monkeypatch):
    _seed(db)
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    captured: list = []
    fa.recommend_features(db, "predict churn", _capture_client(captured), catalog_source="bank",
                          budget=1, critic=False)
    meta = captured[0]
    assert "table_context" not in meta                 # no context block flag-off
    assert all(set(m.keys()) == {"object_ref", "table", "column", "concept", "domain"}
               for m in meta["columns"])               # thin projection only


def test_flag_on_menu_enriched_with_context_and_relevance(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    captured: list = []
    fa.recommend_features(db, "predict churn", _capture_client(captured), catalog_source="bank",
                          budget=1, critic=False)
    meta = captured[0]
    assert "table_context" in meta
    amount = next(m for m in meta["columns"] if m["object_ref"] == "public.accounts.churn_flag")
    assert amount["additivity"]["authority"] in ("governed", "hint")  # wrapped fact


def test_flag_on_overflow_surfaces_context_too_large(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setattr(fa, "FEATURE_CONTEXT_BYTE_BUDGET", 5)
    report = fa.recommend_features_report(db, "predict churn", _capture_client([]),
                                          catalog_source="bank", budget=1, critic=False)
    assert report.ideas == []
    assert any(r["code"] == fa.RejectCode.CONTEXT_TOO_LARGE for r in report.rejections)

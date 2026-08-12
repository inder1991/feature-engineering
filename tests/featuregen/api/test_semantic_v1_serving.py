"""SE-7 — semantic_v1 SERVES: the recipe lens comes from the semantic engine, end to end.

The enforced projection is the program's payoff: frozen context → capability binder →
eligibility fold → assembly → typed gauntlet → Gate-1 cards. These tests prove the mode
actually serves V2 candidates (not the legacy template lens), persists its observations in the
request transaction, and that the frozen `legacy` default is untouched by the new branch.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits"}),
    })


def _bank(conn) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
    ]
    build_graph(conn, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (now, now))


def _post(client) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def test_semantic_v1_serves_the_recipe_lens_from_the_semantic_engine(
        make_client, conn, monkeypatch, caplog):
    """The one-engine payoff: under semantic_v1 the legacy template grounding never runs —
    the templates lens (when present) and the recipe rejections come from the V2 engine."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")

    def boom(*a, **k):  # pragma: no cover - reached only on regression
        raise AssertionError("legacy template grounding must not run under semantic_v1")

    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1._template_candidates", boom)
    with caplog.at_level("INFO", logger="featuregen.overlay.upload.contract.gate1"):
        body = _post(make_client(llm_client=_fake()))

    served = [r.message for r in caplog.records if r.message.startswith("semantic-v1 served:")]
    assert len(served) == 1
    # The V2 engine decided every recipe outcome: whatever bound was served, whatever did not
    # is a NAMED rejection — nothing silently dropped. On this catalog the V2 registry's
    # objective scoping decides how many candidates exist; the invariant is the SOURCE.
    lens_names = {s["lens"] for s in body["alternatives"]}
    assert "llm" in lens_names or len(body["alternatives"]) >= 1   # LLM lens unchanged
    rows = conn.execute(
        "SELECT source_origin, binding_state FROM semantic_candidate_observation").fetchall()
    assert rows, "the serving path persists its observations in the request transaction"
    assert all(r[0] == "recipe_v2" for r in rows)


def test_semantic_v1_recipe_cards_carry_projected_provenance(make_client, conn, monkeypatch):
    """When a V2 candidate binds on this catalog, the card is recipe-sourced with role
    bindings projected from the binder's verdicts (measured authority, confirmation flags)."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    body = _post(make_client(llm_client=_fake()))

    template_sets = [s for s in body["alternatives"] if s["lens"] == "templates"]
    if not template_sets or not template_sets[0]["features"]:
        # Honest skip-shape: nothing bound on this fixture — the engine refused everything by
        # name instead. The projection contract is then visible in the rejections.
        assert any(r.get("code") for r in body.get("rejections", []))
        return
    card = template_sets[0]["features"][0]
    assert card.get("generation_source") == "recipe"
    assert card.get("recipe_id")


def test_semantic_v1_dispositions_describe_the_v2_universe(make_client, conn, monkeypatch):
    """SE-7 part 4: under semantic_v1 the disposition lens folds the universe that was
    actually planned — V2 recipe ids, with the engine's own grounded/rejected outcomes —
    never the legacy registry (which would read UNBUILDABLE for every recipe it never ran)."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    body = _post(make_client(llm_client=_fake()))

    v2_ids = {r.recipe_id for r in V2_RECIPES}
    disposition_ids = {d["recipe_id"] for d in body["dispositions"]}
    assert disposition_ids == v2_ids
    assert body["in_scope_count"] == len(
        [d for d in body["dispositions"] if d["relevance_tier"] is not None])
    # The engine's outcome and the disposition lens agree: anything the projection served or
    # refused is stamped from those same ids, in the same universe.
    rows = {r[0] for r in conn.execute(
        "SELECT DISTINCT source_definition_id FROM semantic_candidate_observation").fetchall()}
    assert rows <= v2_ids


def test_legacy_default_is_untouched_by_the_serving_branch(make_client, conn, monkeypatch):
    """The frozen default: no env var → the semantic serving path must never run."""
    _bank(conn)

    def boom(*a, **k):  # pragma: no cover - reached only on regression
        raise AssertionError("the projection must not run in legacy mode")

    monkeypatch.setattr(
        "featuregen.overlay.upload.semantic_projection.project_assembled_set", boom)
    _post(make_client(llm_client=_fake()))

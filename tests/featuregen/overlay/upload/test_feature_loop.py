"""The validated generate-validate-refine loop for recommend_features."""
from datetime import datetime, timedelta, timezone

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import recommend_features
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def _bank(db):
    rows = [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "churned", "boolean"),   # the target label
    ]
    build_graph(db, "bank", rows)


def _fresh_watermark(db, source, now):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, now, now))


def test_loop_rejects_leaky_and_unsafe_keeps_good(db):
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "leaky", "derives_from": ["public.accounts.churned"]},                 # leaks target
        {"name": "unsafe", "derives_from": ["public.accounts.balance"],
         "aggregation": "sum_all_time"},                                                # unsafe SUM
        {"name": "good", "derives_from": ["public.accounts.balance"],
         "aggregation": "avg_90d"},                                                     # fine
    ]})})
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW)
    names = {f.name for f in out}
    assert names == {"good"}                    # leaky + unsafe were rejected


class _SeqLLM:
    """Returns responses in CALL order regardless of inputs (FakeLLM keys its counter on the input
    hash, which the loop deliberately changes each round via `avoid`)."""
    def __init__(self, responses):
        self._responses, self._i = responses, 0

    def call(self, request):
        from featuregen.intake.llm import LLMResult
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return LLMResult(output=r, self_reported_scores={}, call_ref="", status="ok")


def test_loop_refines_across_rounds(db):
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    # round 1: only a leaky idea; round 2: a good one -> the loop must continue and find it.
    client = _SeqLLM([
        {"features": [{"name": "leaky", "derives_from": ["public.accounts.churned"]}]},
        {"features": [{"name": "good", "derives_from": ["public.accounts.balance"],
                       "aggregation": "avg_90d"}]},
    ])
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW, target=1, budget=3)
    assert [f.name for f in out] == ["good"]


def test_loop_rejects_stale_source(db):
    _bank(db)
    # watermark is 3 days old -> beyond the 24h freshness window.
    _fresh_watermark(db, "bank", NOW - timedelta(days=3))
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "good", "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW)
    assert out == []                            # the only candidate's source is stale


def test_cross_domain_gather_spans_catalogs(db):
    """With an entity anchor, the loop gathers candidates from EVERY catalog holding that entity."""
    build_graph(db, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric")])
    build_graph(db, "cards", [
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer"),
        CanonicalRow("cards", "card_accounts", "spend", "numeric")])
    _fresh_watermark(db, "deposits", NOW)
    _fresh_watermark(db, "cards", NOW)

    captured = {}

    class _Capture:
        def call(self, request):
            captured["refs"] = {c["object_ref"] for c in request.inputs["columns"]}
            from featuregen.intake.llm import LLMResult
            # propose a CROSS-DOMAIN feature: balance (deposits) + spend (cards)
            return LLMResult(output={"features": [{"name": "cross", "aggregation": "avg_90d",
                "derives_from": ["public.accounts.balance", "public.card_accounts.spend"]}]},
                self_reported_scores={}, call_ref="", status="ok")

    out = recommend_features(db, "predict churn", _Capture(), entity="Customer", now=NOW, target=1)
    # the menu spanned both catalogs...
    assert "public.accounts.balance" in captured["refs"]
    assert "public.card_accounts.spend" in captured["refs"]
    # ...and a cross-domain feature was accepted (both sources fresh, no leak/unsafe).
    assert [f.name for f in out] == ["cross"]

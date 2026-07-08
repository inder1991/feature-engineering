"""Phase 2 — Gate #1 bridge: considered-set from the loop + recorded human choice."""
from datetime import UTC, datetime

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    Gate1Error,
    build_considered_set,
    confirm_gate1,
)
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (NOW, NOW))


def _client():
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def test_considered_set_has_anchor_alternatives_and_advisory(db):
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition="90-day average balance per customer", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert cs.anchor is not None and cs.anchor.name == "avg_balance_90d"    # definition -> anchor
    assert any(f.name == "avg_balance_90d" for s in cs.alternatives for f in s.features)
    assert cs.recommendation is not None and cs.recommendation.recommended_lens == "monetary"
    assert "backtest" in cs.recommendation.caveat                           # advisory, caveated


def test_considered_set_carries_gauntlet_rejections(db):
    # The considered set surfaces WHAT the gauntlet threw out and why (the Gate-#3 transparency the
    # Workbench renders) — a leaky candidate (derives from the target) is rejected, not silently dropped.
    _bank(db)
    client = FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"},
            {"name": "reads_the_answer", "derives_from": ["public.accounts.churned"],
             "aggregation": "max"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits"}),
        "overlay.feature.critique_candidates": FakeResponse(output={"issues": []}),
    })
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, client, catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert isinstance(cs.rejections, list)
    assert any(r.get("code") == "LEAKAGE" for r in cs.rejections)   # the leaky candidate is surfaced


def test_considered_set_threads_the_objective(db):
    # bug_003: the prediction goal was required-but-ignored. It now enriches the generation prompt
    # (redacted with the same discipline as the hypothesis) and still yields a valid, governable set.
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned",
                              objective="predict 90-day retail churn", now=NOW)
    assert cs.alternatives


def test_considered_set_threads_feedback(db):
    # A whole-round feedback re-runs the considered set under the human's instruction and still produces
    # a valid, governable set (its own intent + persisted snapshot) — this is what makes post-feedback
    # candidates governable (I2b), lifting the stale-intent guard.
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned",
                              feedback="focus on behavioral signals", now=NOW)
    assert cs.intent_id == intent.intent_id
    assert cs.alternatives            # feedback round still yields a validated, governable set


def test_hypothesis_only_has_no_anchor(db):
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert cs.anchor is None                                               # hypothesis-only -> no anchor
    assert cs.alternatives                                                 # but alternatives generated


def test_confirm_gate1_records_choice_and_rejects_out_of_set(db):
    _bank(db)
    intent = submit_intent(hypothesis="churn from balance drop",
                           definition="90-day average balance", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    ref = confirm_gate1(db, cs, chosen_source="anchor", chosen_option_id="avg_balance_90d",
                        actor="ds1", why="best fit for the hypothesis")
    assert ref == "avg_balance_90d"
    row = db.execute("SELECT chosen_source, chosen_option_id, why FROM contract_gate1_choice "
                     "WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    assert row == ("anchor", "avg_balance_90d", "best fit for the hypothesis")

    with pytest.raises(Gate1Error):        # a choice not in the considered set is rejected
        confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="ghost", actor="ds1")
    with pytest.raises(Gate1Error):        # 'anchor' source but not the anchor
        confirm_gate1(db, cs, chosen_source="anchor", chosen_option_id="not_the_anchor", actor="ds1")


def test_confirm_gate1_validates_the_alternative_source(db):
    # M2: an 'alternative' choice that is actually the anchor's name must be rejected
    from featuregen.overlay.upload.contract.gate1 import ConsideredSet
    from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet
    _bank(db)
    anchor = FeatureIdea("anchor_feat", "", ["public.accounts.balance"], "avg_90d", "accounts")
    alt = FeatureSet("monetary",
                     [FeatureIdea("alt_feat", "", ["public.accounts.balance"], "avg_90d", "accounts")])
    cs = ConsideredSet("intent-x", anchor, [alt], None)
    with pytest.raises(Gate1Error):
        confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="anchor_feat", actor="ds1")
    assert confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="alt_feat",
                         actor="ds1") == "alt_feat"


def test_intent_is_persisted_at_gate1(db):
    # M6: the mandatory hypothesis is durably recorded when the flow reaches Gate #1
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition="90-day average balance per customer", actor="ds1")
    build_considered_set(db, intent, _client(), catalog_source="bank",
                         target_ref="public.accounts.churned", now=NOW)
    row = db.execute("SELECT hypothesis, definition, intake_mode FROM contract_intent "
                     "WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    assert row == ("customers churn when their balance drops",
                   "90-day average balance per customer", "definition")

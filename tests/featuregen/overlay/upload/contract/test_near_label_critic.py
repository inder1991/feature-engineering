"""TASK 3 — the near-label leakage critic: flag-only verdicts, origin-blind, incapable of clearing.

The control the registry specified and nothing built: 23 templates carry ``near_label=True`` and the
``target_ref`` veto cannot compare a 90-day-inactivity FEATURE against a 90-day-inactivity LABEL.
The critic answers that one fenced question in a closed vocabulary {no_finding | too_close |
abstain} with — deliberately — no token that reads as "cleared". Its inputs are the intake build's
SIGNED window (data, migration 1059) and the already-redacted hypothesis; no signed window means
every verdict abstains at zero model cost. Verdicts are content-addressed (the Task 2b seam) and
ledger-bounded; every failure shape degrades to an honest abstain, never a removal or a 5xx.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import build_considered_set, persist_intent
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.contract.intake_ticket import record_target_reading
from featuregen.overlay.upload.contract.near_label_critic import (
    NEAR_LABEL_TASK,
    annotate_near_label,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_batch import CallLedger
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)

_DORMANCY = FeatureIdea(
    name="dormancy_days", description="days since the customer's last transaction",
    derives_from=["public.accounts.event_ts"], aggregation="recency", grain_table="accounts",
    window="90d")
_BALANCE = FeatureIdea(
    name="avg_balance_90d", description="average balance over 90 days",
    derives_from=["public.accounts.balance"], aggregation="avg_90d", grain_table="accounts")

_HYPOTHESIS = "customers churn when activity stops; churn = 90 days of inactivity"


def _critic(verdict: str = "too_close", rationale: str = "≈ the 90-day inactivity label") -> FakeLLM:
    return FakeLLM(script={NEAR_LABEL_TASK: FakeResponse(
        output={"verdict": verdict, "rationale": rationale})})


class _MustNotBeCalled:
    def call(self, *a, **k):  # pragma: no cover
        raise AssertionError("this path must never dispatch the model")


def test_no_signed_window_means_every_verdict_abstains_at_zero_cost(db):
    out = annotate_near_label(db, _MustNotBeCalled(), ideas=[_DORMANCY, _BALANCE],
                              redacted_hypothesis=_HYPOTHESIS, label_window_days=None)
    assert [i.near_label_verdict for i in out] == ["abstain", "abstain"]
    assert "sign a target window" in out[0].near_label_rationale


def test_a_signed_window_yields_a_verdict_and_a_rerun_replays(db):
    out = annotate_near_label(db, _critic(), ideas=[_DORMANCY],
                              redacted_hypothesis=_HYPOTHESIS, label_window_days=90)
    assert out[0].near_label_verdict == "too_close"
    assert out[0].near_label_rationale == "≈ the 90-day inactivity label"
    # content-addressed: the same candidate + label re-annotates with ZERO model calls
    again = annotate_near_label(db, _MustNotBeCalled(), ideas=[_DORMANCY],
                                redacted_hypothesis=_HYPOTHESIS, label_window_days=90)
    assert again[0].near_label_verdict == "too_close"


def test_the_inputs_are_not_mutated_and_a_different_window_is_a_different_question(db):
    annotate_near_label(db, _critic(), ideas=[_DORMANCY],
                        redacted_hypothesis=_HYPOTHESIS, label_window_days=90)
    assert _DORMANCY.near_label_verdict is None, "annotation returns new objects"
    # 30-day label vs the same candidate = a different cache key → a fresh adjudication
    fresh = annotate_near_label(db, _critic(verdict="no_finding", rationale="different windows"),
                                ideas=[_DORMANCY], redacted_hypothesis=_HYPOTHESIS,
                                label_window_days=30)
    assert fresh[0].near_label_verdict == "no_finding"


def test_every_failure_shape_degrades_to_an_honest_abstain(db):
    # no client
    no_client = annotate_near_label(db, None, ideas=[_DORMANCY],
                                    redacted_hypothesis=_HYPOTHESIS, label_window_days=90)
    assert no_client[0].near_label_verdict == "abstain"
    # ledger exhausted: the second candidate abstains rather than dispatching unboundedly
    ledger = CallLedger(max_provider_calls=1)
    capped = annotate_near_label(db, _critic(), ideas=[_BALANCE, _DORMANCY],
                                 redacted_hypothesis=_HYPOTHESIS, label_window_days=90,
                                 call_ledger=ledger)
    assert capped[0].near_label_verdict == "too_close"
    assert capped[1].near_label_verdict == "abstain"
    assert "ceiling" in capped[1].near_label_rationale
    # an off-vocabulary answer is never trusted
    bad = annotate_near_label(db, _critic(verdict="cleared"), ideas=[_BALANCE],
                              redacted_hypothesis="a different question entirely",
                              label_window_days=90)
    assert bad[0].near_label_verdict == "abstain"
    assert "no usable verdict" in bad[0].near_label_rationale


# ── builder integration: flag-gated, origin-blind, round-trips through the snapshot ──────────────

def _churn_catalog(db):
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
    ]
    build_graph(db, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (NOW, NOW))


def _gen_client():
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "days_since_last_txn", "derives_from": ["public.accounts.event_ts"],
             "aggregation": "recency_90d",
             "description": "days since the customer's last transaction"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "temporal", "reasoning": "recency fits the inactivity hypothesis"}),
        NEAR_LABEL_TASK: FakeResponse(
            output={"verdict": "too_close", "rationale": "≈ the label window"}),
    })


def _signed_intent(db):
    """The intake path's deduped intent with a HUMAN-signed 90-day window, plus the ROUND's own
    fresh intent for the same (hypothesis, mode, actor) — proving the critic finds the signed
    reading by identity, not by the round's intent_id."""
    intake_intent = submit_intent(hypothesis=_HYPOTHESIS, actor="ds1")
    persist_intent(db, intake_intent)
    assert record_target_reading(
        db, intent_id=intake_intent.intent_id, provenance="human_confirmed",
        target_ref="public.accounts.churned", target_window_days=90,
        confirmed_by="user:ds1")
    return submit_intent(hypothesis=_HYPOTHESIS, actor="ds1")


def test_the_critic_annotates_unconditionally(db):
    """Pre-live simplification (2026-08-11): FEATUREGEN_NEAR_LABEL_CRITIC retired — every
    surviving candidate carries a verdict without any env (a signed window exists here, so
    verdicts are real; without one, every verdict abstains at zero model cost)."""
    _churn_catalog(db)
    intent = _signed_intent(db)
    cs = build_considered_set(db, intent, _gen_client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    ideas = [f for s in cs.alternatives for f in s.features]
    assert ideas and all(f.near_label_verdict is not None for f in ideas)


def test_flag_on_annotates_origin_blind_and_the_snapshot_round_trips(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_NEAR_LABEL_CRITIC", "1")
    _churn_catalog(db)
    intent = _signed_intent(db)
    cs = build_considered_set(db, intent, _gen_client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    ideas = [f for s in cs.alternatives for f in s.features]
    assert ideas
    # ORIGIN-BLIND: the LLM-proposed near-label twin and the template candidates alike carry
    # verdicts — the exact gap the owner's decision closed (same feature, same leakage, one flag).
    llm_idea = next(f for f in ideas if f.name == "days_since_last_txn")
    assert llm_idea.near_label_verdict == "too_close"
    assert all(f.near_label_verdict is not None for f in ideas)
    # FLAG-ONLY: nothing was removed relative to the flag-off build — annotation, never removal.
    # (Compare against a fresh flag-off build of the same catalog + hypothesis.)
    monkeypatch.delenv("FEATUREGEN_NEAR_LABEL_CRITIC")
    baseline = build_considered_set(db, submit_intent(hypothesis=_HYPOTHESIS, actor="ds1"),
                                    _gen_client(), catalog_source="bank",
                                    target_ref="public.accounts.churned", now=NOW)
    assert ({f.name for s in cs.alternatives for f in s.features}
            == {f.name for s in baseline.alternatives for f in s.features})
    # the persisted public snapshot carries the verdict for the Gate-1 reload
    row = db.execute("SELECT considered FROM contract_considered WHERE intent_id = %s",
                     (cs.intent_id,)).fetchone()
    snap_ideas = [f for s in row[0]["alternatives"] for f in s["features"]]
    assert any(f.get("near_label_verdict") == "too_close" for f in snap_ideas)


def test_flag_on_without_a_signed_reading_abstains_everywhere(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_NEAR_LABEL_CRITIC", "1")
    _churn_catalog(db)
    intent = submit_intent(hypothesis=_HYPOTHESIS, actor="ds1")   # nobody signed anything
    cs = build_considered_set(db, intent, _gen_client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    ideas = [f for s in cs.alternatives for f in s.features]
    assert ideas and all(f.near_label_verdict == "abstain" for f in ideas)


def test_a_definition_mode_round_still_finds_the_signed_reading(db, monkeypatch):
    """Review fix: the intake route always mints hypothesis-mode intents, while a
    definition-carrying considered-set request runs in `definition` mode — same hypothesis, same
    person, same signature. A mode-filtered lookup silently lost it on exactly that path."""
    monkeypatch.setenv("FEATUREGEN_NEAR_LABEL_CRITIC", "1")
    _churn_catalog(db)
    _signed_intent(db)   # signs under hypothesis mode, exactly as /contract/intake does
    definition_round = submit_intent(hypothesis=_HYPOTHESIS,
                                     definition="days since last activity per customer",
                                     actor="ds1")
    cs = build_considered_set(db, definition_round, _gen_client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    ideas = [f for s in cs.alternatives for f in s.features]
    assert ideas
    assert any(f.near_label_verdict == "too_close" for f in ideas), \
        "the signed 90-day window reached the critic despite the round's definition mode"

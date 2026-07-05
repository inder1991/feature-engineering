"""Human-in-the-loop additions to feature-assist: rejection transparency (reports), human
feedback threading into every generation round, and the per-candidate refine step."""
from datetime import UTC, datetime

from featuregen.intake.llm import FakeLLM, FakeResponse, LLMResult
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import (
    RejectCode,
    recommend_feature_sets_report,
    recommend_features,
    recommend_features_report,
    refine_idea,
)
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean"),   # the target label
    ])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        ("bank", NOW, NOW))


def _result(out: dict) -> LLMResult:
    return LLMResult(output=out, self_reported_scores={}, call_ref="", status="ok")


# ---- rejection transparency -------------------------------------------------------------------


def test_report_exposes_structured_rejections_and_same_ideas(db):
    _bank(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "leaky", "derives_from": ["public.accounts.churned"], "aggregation": "latest"},
        {"name": "good", "derives_from": ["public.accounts.balance"], "aggregation": "latest"},
    ]})})
    report = recommend_features_report(db, "predict churn", client, catalog_source="bank",
                                       target_ref="public.accounts.churned", now=NOW,
                                       budget=3, critic=False)
    assert [f.name for f in report.ideas] == ["good"]
    # 3 rounds each re-rejected the same candidate; the report collapses the identical repeats.
    assert report.rejections == [
        {"name": "leaky", "reason": "leaks target", "code": RejectCode.LEAKAGE}]


def test_recommend_features_return_type_is_unchanged(db):
    _bank(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "good", "derives_from": ["public.accounts.balance"], "aggregation": "latest"}]})})
    out = recommend_features(db, "predict churn", client, catalog_source="bank", now=NOW,
                             critic=False)
    assert isinstance(out, list) and [f.name for f in out] == ["good"]


# ---- human feedback threading -----------------------------------------------------------------


class _CaptureGen:
    """Captures every generator round's catalog_metadata; critique flags once so a fix pass runs."""

    def __init__(self):
        self.metadata: list[dict] = []
        self._crit = 0

    def call(self, request):
        if request.task == "overlay.feature.recommend":
            self.metadata.append(request.inputs["catalog_metadata"])
            return _result({"features": [{
                "name": f"f{len(self.metadata)}",
                "derives_from": ["public.accounts.balance"], "aggregation": "latest"}]})
        if request.task == "overlay.feature.critique_candidates":
            self._crit += 1
            if self._crit == 1:
                cands = request.inputs["catalog_metadata"]["candidates"]
                return _result({"issues": [{"name": c["name"], "issue": "weak"} for c in cands]})
            return _result({"issues": []})
        return _result({})


def test_feedback_reaches_every_generation_round_including_fix_pass(db):
    _bank(db)
    client = _CaptureGen()
    recommend_features(db, "predict churn", client, catalog_source="bank", now=NOW,
                       target=1, budget=2, feedback="more behavioral signals")
    assert len(client.metadata) == 2                       # one generation round + one fix pass
    assert all(m["feedback"] == "more behavioral signals" for m in client.metadata)
    assert "avoid" in client.metadata[0]                   # machine feedback still rides alongside
    assert "fix" in client.metadata[1]                     # the critic-fix pass carries it too


def test_absent_feedback_leaves_round_inputs_unchanged(db):
    _bank(db)
    client = _CaptureGen()
    recommend_features(db, "predict churn", client, catalog_source="bank", now=NOW,
                       target=1, budget=1, critic=False)
    assert client.metadata and all("feedback" not in m for m in client.metadata)


# ---- per-candidate refine ---------------------------------------------------------------------


def test_refine_idea_carries_instruction_as_fix_hint_and_validates_revision(db):
    _bank(db)
    captured = {}

    class _Client:
        def call(self, request):
            captured["metadata"] = request.inputs["catalog_metadata"]
            return _result({"features": [{
                "name": "avg_balance_30d", "description": "30 day average balance",
                "derives_from": ["public.accounts.balance"], "aggregation": "avg_30d",
                "grain_table": "accounts", "rationale": "a shorter window reacts faster"}]})

    idea = {"name": "avg_balance_90d", "description": "90 day average balance",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d",
            "grain_table": "accounts"}
    revised, rejection = refine_idea(db, idea, "use a 30 day window", _Client(),
                                     catalog_source="bank", now=NOW)
    assert rejection is None
    assert revised is not None
    assert revised.name == "avg_balance_30d"
    assert revised.aggregation == "avg_30d"
    assert revised.derives_pairs == (("bank", "public.accounts.balance"),)
    assert revised.verification == "DESIGN-CHECKED"        # still only design-checked, still a proposal
    assert captured["metadata"]["fix"] == [{
        "name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
        "aggregation": "avg_90d", "issue": "use a 30 day window"}]


def test_refine_rejects_a_revision_that_leaks_the_target(db):
    _bank(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "rev", "derives_from": ["public.accounts.churned"], "aggregation": "latest"}]})})
    revised, rejection = refine_idea(
        db, {"name": "orig", "derives_from": ["public.accounts.balance"]}, "make it sharper",
        client, catalog_source="bank", target_ref="public.accounts.churned", now=NOW)
    assert revised is None
    assert rejection == {"name": "rev", "reason": "leaks target", "code": RejectCode.LEAKAGE}


def test_refine_with_empty_model_output_returns_no_revision(db):
    _bank(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": []})})
    revised, rejection = refine_idea(db, {"name": "orig"}, "tighten the window", client,
                                     catalog_source="bank", now=NOW)
    assert revised is None
    assert rejection is not None and rejection["code"] == RejectCode.NO_REVISION


def test_refine_egress_guard_blocks_pii_instruction(db):
    # M6: the reviewer's instruction is user text — a PII-bearing instruction is blocked before
    # dispatch, and the caller sees an honest NO_REVISION, never a leak.
    _bank(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "rev", "derives_from": ["public.accounts.balance"], "aggregation": "latest"}]})})
    revised, rejection = refine_idea(db, {"name": "orig"}, "email joe@bank.com the result", client,
                                     catalog_source="bank", now=NOW)
    assert revised is None
    assert rejection is not None and rejection["code"] == RejectCode.NO_REVISION


# ---- multi-set report -------------------------------------------------------------------------


def test_sets_report_aggregates_and_dedupes_rejections_across_lenses(db):
    _bank(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "leaky", "derives_from": ["public.accounts.churned"], "aggregation": "latest"},
        {"name": "good", "derives_from": ["public.accounts.balance"], "aggregation": "latest"},
    ]})})
    report = recommend_feature_sets_report(db, "predict churn", client, catalog_source="bank",
                                           target_ref="public.accounts.churned", now=NOW,
                                           lenses=("behavioral", "monetary"), per_set=1)
    assert {s.lens for s in report.sets} == {"behavioral", "monetary"}
    assert all([f.name for f in s.features] == ["good"] for s in report.sets)
    # both lens loops rejected the same candidate; the report shows it once
    assert report.rejections == [
        {"name": "leaky", "reason": "leaks target", "code": RejectCode.LEAKAGE}]

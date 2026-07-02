import pytest

from featuregen.intake.candidates import (
    CANDIDATES_OUTPUT_SCHEMA_ID,
    CANDIDATES_PROMPT_ID,
    STUB_GENERATOR_VERSION,
    StubCandidateGenerator,
)
from featuregen.intake.llm import LLMResult
from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor


@pytest.fixture(autouse=True)
def _redactor():
    """The stub now redacts the draft's free-text into the reserved `redacted_intent` (§9.4) before
    building the request, so it resolves the R10 IntentRedactor seam — register the default here."""
    register_intent_redactor(DefaultIntentRedactor())


class _ScriptedLLM:
    """A minimal LLMClient test double (structurally an LLMClient) that returns a scripted output
    and counts calls — so the 'exactly one LLM pass' invariant is directly assertable."""

    def __init__(self, output, *, status="ok", call_ref="llmc_stub_1"):
        self.output = output
        self.status = status
        self.call_ref = call_ref
        self.calls = 0
        self.last_request = None

    def call(self, request):
        self.calls += 1
        self.last_request = request
        return LLMResult(
            output=self.output, self_reported_scores={}, call_ref=self.call_ref, status=self.status
        )


# The §3.2 / Appendix-B hypothesis example: abrupt spending-category shift → credit risk.
_THREE = {
    "candidates": [
        {"definition_text": "count of distinct MCCs, last 30d minus prior 30d",
         "rationale": "category churn precedes financial distress",
         "calculation_method": {"kind": "rolling_aggregate", "aggregation": "distinct_count",
                                "window": "30d", "filter": {"concept": "merchant_category_code"}}},
        {"definition_text": "share of spend in top-1 category vs 3-month average",
         "rationale": "concentration shift signals stress",
         "calculation_method": {"kind": "ratio", "numerator": "top_category_spend",
                                "denominator": "total_spend", "window": "30d"}},
        {"definition_text": "JS divergence of this month's category-spend vs trailing 6-month",
         "rationale": "whole-distribution shift is a richer signal",
         "calculation_method": {"kind": "distribution_divergence", "measure": "jensen_shannon",
                                "window": "30d", "baseline_window": "180d"}},
    ]
}
_DRAFT = {"intake_mode": "hypothesis", "proposed_feature_name": "abrupt_category_shift",
          "target": "higher credit risk", "feature_semantics": {}}
_CATALOG = {"concepts": ["merchant_category_code", "total_spend", "top_category_spend"]}
_DOMAIN = {"allowed_concepts": ["merchant_category_code"]}


def test_single_pass_yields_three_scored_candidates():
    llm = _ScriptedLLM(_THREE)
    cands = StubCandidateGenerator(llm).generate(_DRAFT, _CATALOG, _DOMAIN)
    assert llm.calls == 1  # deliberately ONE LLM pass (§7.2)
    assert len(cands) == 3
    assert [c.calculation_method["chosen"]["kind"] for c in cands] == [
        "rolling_aggregate", "ratio", "distribution_divergence"
    ]
    for c in cands:
        assert c.candidate_id.startswith("cand_")
        assert c.rationale  # surfaced at Gate #1 (§8.1)
        assert c.calculation_method["method_version"] == 1
        assert c.provenance["llm_call_refs"] == ["llmc_stub_1"]
        assert c.provenance["generator_version"] == STUB_GENERATOR_VERSION
        assert "heuristic_rank" in c.signals  # cheap model-free signals attached
    # the one pass is the registered, versioned generate_candidates call
    assert llm.last_request.task == "generate_candidates"
    assert llm.last_request.prompt_id == CANDIDATES_PROMPT_ID
    assert llm.last_request.output_schema_id == CANDIDATES_OUTPUT_SCHEMA_ID


def test_request_inputs_are_reserved_llm_safe_not_ad_hoc_draft_keys():
    # §9.4 egress reconciliation: the request the stub hands to the (recording) client is the RESERVED,
    # LLM-safe shape (build_llm_inputs) — never the old ad-hoc draft keys — so it passes assert_llm_safe
    # and no un-redacted draft free-text ever reaches the LLM.
    llm = _ScriptedLLM(_THREE)
    StubCandidateGenerator(llm).generate(_DRAFT, _CATALOG, _DOMAIN)
    inputs = llm.last_request.inputs
    assert set(inputs) >= {
        "redacted_intent", "catalog_metadata", "raw_input_classification",
        "redaction_version", "input_redaction",
    }
    # the ad-hoc free-text draft keys are GONE (they were the egress hazard); the free-text is folded
    # into the single redacted_intent, and only catalog NAMES ride catalog_metadata.
    assert "target" not in inputs and "proposed_feature_name" not in inputs and "entity" not in inputs
    assert "abrupt_category_shift" in inputs["redacted_intent"]
    assert "higher credit risk" in inputs["redacted_intent"]
    assert inputs["catalog_metadata"] == {
        "allowed_concepts": ["merchant_category_code", "top_category_spend", "total_spend"]
    }
    assert inputs["raw_input_classification"] == "clean"


def test_unscanned_draft_fails_closed_no_llm_pass():
    # An `unscanned` intent is un-redactable → fail closed BEFORE the pass: no LLM call, no candidates.
    llm = _ScriptedLLM(_THREE)
    draft = {**_DRAFT, "raw_input_classification": "unscanned"}
    assert StubCandidateGenerator(llm).generate(draft, _CATALOG, _DOMAIN) == []
    assert llm.calls == 0  # the LLM is never reached on the fail-closed redaction path


def test_clamps_to_at_most_three_candidates():
    many = {"candidates": _THREE["candidates"] + [
        {"definition_text": "extra", "rationale": "x",
         "calculation_method": {"kind": "point_snapshot", "field": "balance"}}]}
    cands = StubCandidateGenerator(_ScriptedLLM(many)).generate(_DRAFT, _CATALOG, None)
    assert len(cands) == 3  # 1..3 (§3.2)


def test_failed_into_clarification_yields_no_candidates():
    llm = _ScriptedLLM(_THREE, status="failed_into_clarification")
    assert StubCandidateGenerator(llm).generate(_DRAFT, _CATALOG, None) == []
    assert llm.calls == 1  # still exactly one pass; it just failed closed


def test_structurally_invalid_variant_is_skipped_never_fabricated():
    bad = {"candidates": [
        {"definition_text": "good", "rationale": "r",
         "calculation_method": {"kind": "ratio", "numerator": "a", "denominator": "b"}},
        {"definition_text": "unknown kind", "rationale": "r",
         "calculation_method": {"kind": "neural_net"}},          # not a closed kind → dropped
        {"definition_text": "no method", "rationale": "r"},         # missing method → dropped
    ]}
    cands = StubCandidateGenerator(_ScriptedLLM(bad)).generate(_DRAFT, _CATALOG, None)
    assert len(cands) == 1
    assert cands[0].calculation_method["chosen"]["kind"] == "ratio"


def test_bare_variant_is_wrapped_into_the_tagged_shape():
    one = {"candidates": [
        {"definition_text": "declined auth count 90d", "rationale": "faithful",
         "calculation_method": {"kind": "rolling_aggregate", "aggregation": "count",
                                "window": "90d", "filter": {"concept": "declined_auth"}}}]}
    cands = StubCandidateGenerator(_ScriptedLLM(one)).generate(_DRAFT, {"concepts": ["declined_auth"]}, None)
    m = cands[0].calculation_method
    assert set(m) >= {"method_version", "chosen", "considered"}  # §4.2 tagged shape
    assert m["considered"] == [m["chosen"]]
    assert cands[0].signals["references_known_concept"] is True

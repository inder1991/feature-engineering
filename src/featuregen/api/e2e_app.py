"""E2E-ONLY app factory — the real app over a SCRIPTED model client.

Used exclusively by the Playwright harness's webServer (`uvicorn --factory
featuregen.api.e2e_app:create_e2e_app`). The production factory's D5 rule is untouched: it
still NEVER falls back to a fake (no key ⟹ assist honestly 503s). This module exists so the
browser journey can exercise the REAL serving path — real routes, real Postgres, real
activation policy — with deterministic model output, no provider spend, and no key on disk.

Everything except the model client is the production wiring.
"""
from __future__ import annotations

from featuregen.api.app import create_app
from featuregen.intake.llm import PROVIDER_REFUSAL, FakeLLM, FakeResponse, LLMResult


class _TolerantFakeLLM(FakeLLM):
    """FakeLLM that folds UNSCRIPTED tasks to a provider refusal instead of raising.

    The journey scripts exactly the generation-path tasks; everything else the app may
    dispatch (upload enrichment batches, summaries, …) folds to the refusal shape every
    caller already handles fail-soft — ingest proceeds un-enriched, exactly as it does in
    production with no provider configured. Test-only, like everything in this module."""

    def call(self, request):
        try:
            return super().call(request)
        except KeyError:
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_REFUSAL, cost_metadata={})

_CHURN = "customer.relationship_attrition.churn"

_INTENTS = {"intents": [{
    "display_name": "Days since last activity (model)",
    "business_definition": "Days elapsed since the customer's most recent event.",
    "primary_objective": _CHURN,
    "computation_kind": "deterministic_formula",
    "operation_class": "recency",
    "output_grain_entity": "customer",
    "source_grain": "transaction",
    "output": {
        "output_id": "recency_model", "display_label": "Days since last activity",
        "output_type": "numeric", "additivity": "non_additive",
        "unit_kind": "duration_days",
        "null_input_policy": "null timestamps are excluded and counted",
        "empty_population_policy": "null with populated flag",
    },
    "operands": [
        {"role": "who", "concept": "customer_id", "operand_class": "entity_key"},
        {"role": "when", "concept": "event_timestamp", "operand_class": "event_timestamp"},
    ],
    "temporal": {"anchor_kind": "event", "window_basis": "event time",
                 "window_unit": "days", "cutoff_inclusivity": "inclusive"},
    "rationale": "recency is the strongest dormancy precursor",
}]}


def create_e2e_app():
    client = _TolerantFakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary pressure fits churn"}),
        "overlay.feature.intents": FakeResponse(output=_INTENTS),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Count of complaint events per customer in the window."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
        # The recognizer + intake tasks the journey's screens touch — the REAL task keys and
        # payload shapes their own route tests script.
        "use_case_recognition": FakeResponse(output={
            "status": "classified",
            "candidates": [{
                "use_case_id": _CHURN, "relationship": "primary", "confidence": "high",
                "evidence_spans": ["churn"],
                "rationale": "the hypothesis is about customers leaving"}],
            "modelling_contexts": [], "target_entity": "customer",
            "ambiguity_note": None}),
        "overlay.contract.intake_ticket": FakeResponse(output={
            "target_ref": "public.accounts.churned", "target_window_days": 90,
            "target_type": "binary_classification", "business_domain": [],
            "confidence": "high", "runner_up_refs": []}),
    })
    return create_app(llm_client=client)

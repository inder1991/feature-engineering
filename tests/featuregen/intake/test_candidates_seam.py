import dataclasses

import pytest

from featuregen.intake.candidates import (
    Candidate,
    CandidateGenerator,
    candidate_signals,
    current_candidate_generator,
    register_candidate_generator,
)


def _method(chosen: dict) -> dict:
    return {"method_version": 1, "chosen": chosen, "considered": [chosen]}


def test_candidate_is_frozen_with_the_seam_fields():
    c = Candidate(
        candidate_id="cand_1",
        definition_text="count of distinct MCCs, last 30d minus prior 30d",
        rationale="category churn precedes financial distress",
        calculation_method=_method({"kind": "ratio", "numerator": "a", "denominator": "b"}),
        signals={},
        provenance={},
    )
    assert c.candidate_id == "cand_1"
    assert dataclasses.is_dataclass(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.definition_text = "mutated"  # write-once seam object


def test_signals_are_cheap_model_free_only_no_predictive_power():
    method = _method(
        {"kind": "rolling_aggregate", "aggregation": "count", "window": "90d",
         "filter": {"concept": "declined_auth"}}
    )
    s = candidate_signals(method, "count of declined auths",
                          known_concepts={"declined_auth"}, sibling_methods=[])
    assert s["references_known_concept"] is True
    assert s["window_sane"] is True
    assert s["duplicate_of_sibling"] is False
    assert 0.0 <= s["heuristic_rank"] <= 1.0
    assert s["scored_by"] == "cheap_model_free_heuristic"
    # §7.3 boundary: NO measured-predictive-power keys may ever appear
    keys = {k.lower() for k in s}
    for banned in ("iv", "woe", "auc", "information_value", "gini", "ks", "overfitting"):
        assert banned not in keys


def test_unknown_concept_and_insane_window_lower_signals():
    method = _method(
        {"kind": "rolling_aggregate", "aggregation": "count", "window": "9999d",
         "filter": {"concept": "mystery_concept"}}
    )
    s = candidate_signals(method, "", known_concepts={"declined_auth"}, sibling_methods=[])
    assert s["references_known_concept"] is False
    assert s["window_sane"] is False  # 9999d > 3y ceiling


def test_duplicate_of_a_sibling_is_flagged():
    chosen = {"kind": "distribution_divergence", "measure": "jensen_shannon",
              "window": "30d", "baseline_window": "180d"}
    method = _method(chosen)
    s = candidate_signals(method, "JS divergence of category spend",
                          known_concepts=set(), sibling_methods=[_method(chosen)])
    assert s["duplicate_of_sibling"] is True


def test_candidategenerator_is_a_runtime_checkable_protocol():
    class _G:
        def generate(self, draft, catalog_metadata, domain_context=None):
            return []

    assert isinstance(_G(), CandidateGenerator)  # structural conformance = the stable seam
    assert not isinstance(object(), CandidateGenerator)


def test_candidate_generator_di_seam_registers_and_resolves():
    # R10 — P6 OWNS the module-global register/current CandidateGenerator seam (fail-closed if unset).
    class _G:
        def generate(self, draft, catalog_metadata, domain_context=None):
            return []

    g = _G()
    register_candidate_generator(g)
    assert current_candidate_generator() is g  # last-writer-wins, mirrors register_catalog_adapter

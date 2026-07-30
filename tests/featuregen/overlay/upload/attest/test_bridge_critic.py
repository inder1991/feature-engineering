from __future__ import annotations

from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _assessment,
    _endpoint,
)

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.attest.bridge_critic import (
    BRIDGE_CRITIC_TASK,
    NamespaceRecommendation,
    critique_identifier_link,
)
from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    NamespaceVerdict,
    PopulationRelation,
)
from featuregen.overlay.upload.structured_results import load_structured_result


def _llm_assessment(**changes):
    left = _endpoint(
        "cib", "customers", authority=ConceptAuthority.LLM)
    right = _endpoint(
        "ftr", "transactions", authority=ConceptAuthority.LLM)
    return _assessment(left, right, **changes)


def _client(
    *,
    namespace: str = "same",
    population: str = "left_subset",
) -> FakeLLM:
    return FakeLLM(script={
        BRIDGE_CRITIC_TASK: FakeResponse(output={
            "namespace_recommendation": namespace,
            "population_hypothesis": population,
            "namespace_reason_codes": ["same customer identifier wording"],
            "population_reason_codes": ["transactions may cover active customers"],
        }),
    })


def test_llm_semantics_are_separate_advisory_outputs_and_replayable(db) -> None:
    original = _llm_assessment(namespace_verdict=NamespaceVerdict.UNKNOWN)
    result = critique_identifier_link(db, _client(), original)
    assert result.namespace_recommendation is NamespaceRecommendation.SAME
    assert result.population_hypothesis is PopulationRelation.LEFT_SUBSET
    assert result.assessment.namespace_verdict is NamespaceVerdict.POSSIBLE
    assert result.assessment.governed_population_relation is PopulationRelation.UNKNOWN
    assert result.assessment.population_hypothesis == "left_subset"
    assert result.assessment.strongest_evidence_label == "llm_only"
    assert result.structured_result_id is not None
    stored = load_structured_result(db, result.structured_result_id)
    assert stored is not None
    assert stored.output["population_hypothesis"] == "left_subset"
    provenance = db.execute(
        "SELECT producer_kind,producer_ref,authority_json "
        "FROM structured_result_provenance WHERE structured_result_id=%s",
        (result.structured_result_id,),
    ).fetchone()
    assert provenance[0] == "llm_call"
    assert provenance[1] == result.llm_call_ref
    assert provenance[2]["authority"] == "llm_advisory"
    assert provenance[2]["left_concept_authority"] == "llm"


def test_llm_different_recommendation_does_not_mint_governed_rejection(db) -> None:
    original = _llm_assessment(namespace_verdict=NamespaceVerdict.POSSIBLE)
    result = critique_identifier_link(
        db,
        _client(namespace="different", population="disjoint"),
        original,
    )
    assert result.namespace_recommendation is NamespaceRecommendation.DIFFERENT
    assert result.assessment.namespace_verdict is NamespaceVerdict.POSSIBLE
    assert result.assessment.hard_conflicts == ()
    assert "llm_recommends_different_namespace" in (
        result.assessment.explanation_codes)


def test_deterministic_hard_conflict_short_circuits_before_llm(db) -> None:
    original = _llm_assessment(
        hard_conflicts=("identifier_value_to_description_text",))
    result = critique_identifier_link(db, FakeLLM(), original)
    assert result.skipped_reason == "deterministic_hard_conflict"
    assert result.llm_call_ref is None
    assert db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id='bridge-critic'"
    ).fetchone()[0] == 0


def test_equivalent_validated_outputs_share_content_addressed_result(db) -> None:
    original = _llm_assessment(namespace_verdict=NamespaceVerdict.UNKNOWN)
    first = critique_identifier_link(db, _client(), original)
    # No script on the second client: a provider call would raise. Exact input replays the
    # validated structured result instead.
    second = critique_identifier_link(db, FakeLLM(), original)
    assert first.structured_result_id == second.structured_result_id
    assert db.execute(
        "SELECT count(*) FROM structured_result WHERE structured_result_id=%s",
        (first.structured_result_id,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id='bridge-critic'"
    ).fetchone()[0] == 1

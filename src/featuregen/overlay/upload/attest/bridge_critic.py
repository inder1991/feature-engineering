"""Audited LLM semantic critic for identifier-link candidates.

The model answers two advisory questions: identifier namespace and likely population relationship.
It is never asked for cardinality, uniqueness, fan-out, or execution eligibility. Deterministic hard
conflicts short-circuit before provider egress.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    NamespaceVerdict,
    PopulationRelation,
)
from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)

if TYPE_CHECKING:
    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.intake.llm import LLMClient

BRIDGE_CRITIC_VERSION = 1
BRIDGE_CRITIC_TASK = "overlay.bridge.identifier_critique"
BRIDGE_CRITIC_PROMPT_ID = "bridge_identifier_critique_v1"
BRIDGE_CRITIC_SCHEMA_ID = "bridge_identifier_critique"


class NamespaceRecommendation(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BridgeCriticResultV1:
    assessment: IdentifierLinkAssessmentV1
    namespace_recommendation: NamespaceRecommendation | None
    population_hypothesis: PopulationRelation | None
    structured_result_id: str | None
    llm_call_ref: str | None
    skipped_reason: str | None = None


def _endpoint_payload(endpoint: IdentifierEndpointV1) -> dict[str, object]:
    return {
        "logical_table_ref": endpoint.logical_table_ref,
        "members": [
            {
                "logical_column_ref": member.logical_column_ref,
                "data_type_family": member.data_type_family,
                "type_basis": member.type_basis.value,
                "key_member_role": member.key_member_role.value,
            }
            for member in endpoint.members
        ],
        "entity_id": endpoint.entity_id,
        "concept": endpoint.concept,
        "concept_authority": endpoint.concept_authority.value,
        "tuple_key_role": endpoint.tuple_key_role.value,
    }


def _critic_input(assessment: IdentifierLinkAssessmentV1) -> dict[str, object]:
    return {
        "candidate_id": assessment.candidate_id,
        "candidate_revision_id": assessment.candidate_revision_id,
        "left": _endpoint_payload(assessment.left_endpoint),
        "right": _endpoint_payload(assessment.right_endpoint),
        "deterministic_namespace_verdict": assessment.namespace_verdict.value,
        "governed_population_relation":
            assessment.governed_population_relation.value,
        "evidence": [
            ref.identity_payload() for ref in assessment.evidence_refs
        ],
        "explanation_codes": list(assessment.explanation_codes),
    }


def _reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({
        str(item).strip().lower()
        for item in value
        if isinstance(item, str) and item.strip()
    }))


def critique_identifier_link(
    conn,
    client: LLMClient,
    assessment: IdentifierLinkAssessmentV1,
    *,
    actor: IdentityEnvelope | None = None,
) -> BridgeCriticResultV1:
    """Return a new assessment carrying honestly-labelled, replayable LLM evidence."""
    if assessment.hard_conflicts or assessment.namespace_verdict is NamespaceVerdict.DIFFERENT:
        return BridgeCriticResultV1(
            assessment,
            None,
            None,
            None,
            None,
            "deterministic_hard_conflict",
        )
    metadata = _critic_input(assessment)
    input_hash = canonical_hash({
        "critic_version": BRIDGE_CRITIC_VERSION,
        "input": metadata,
    })
    stored = find_structured_result(
        conn,
        result_type="bridge_identifier_critique",
        result_version=BRIDGE_CRITIC_VERSION,
        input_content_hash=input_hash,
    )
    llm_call_ref: str | None
    if stored is None:
        call = drive_audited_structured_call(
            conn,
            client,
            task=BRIDGE_CRITIC_TASK,
            prompt_id=BRIDGE_CRITIC_PROMPT_ID,
            schema_id=BRIDGE_CRITIC_SCHEMA_ID,
            catalog_metadata=metadata,
            instruction=(
                "Assess whether the two governed metadata endpoints likely use the same identifier "
                "namespace. Separately hypothesize their population relationship. Do not infer "
                "uniqueness, cardinality, fan-out, or execution safety."
            ),
            actor=actor,
            run_id="bridge-critic",
            record_egress_block=True,
        )
        if call.output is None or call.llm_call_ref is None:
            return BridgeCriticResultV1(
                assessment,
                None,
                None,
                None,
                call.llm_call_ref,
                "llm_critic_unavailable",
            )
        llm_call_ref = call.llm_call_ref
        stored = record_structured_result(
            conn,
            result_type="bridge_identifier_critique",
            result_version=BRIDGE_CRITIC_VERSION,
            input_content_hash=input_hash,
            output=call.output,
            producer_kind="llm_call",
            producer_ref=call.llm_call_ref,
            authority={
                "authority": "llm_advisory",
                "candidate_revision_id": assessment.candidate_revision_id,
                "input_evidence_ids": [
                    ref.evidence_id for ref in assessment.evidence_refs
                ],
                "left_concept_authority":
                    assessment.left_endpoint.concept_authority.value,
                "right_concept_authority":
                    assessment.right_endpoint.concept_authority.value,
            },
        )
    else:
        provenance = conn.execute(
            "SELECT producer_ref FROM structured_result_provenance "
            "WHERE structured_result_id=%s AND producer_kind='llm_call' "
            "ORDER BY recorded_at,producer_ref LIMIT 1",
            (stored.structured_result_id,),
        ).fetchone()
        llm_call_ref = None if provenance is None else provenance[0]
    output = dict(stored.output)
    try:
        namespace = NamespaceRecommendation(output["namespace_recommendation"])
        population = PopulationRelation(output["population_hypothesis"])
    except (KeyError, TypeError, ValueError):
        # The registered schema should make this unreachable; retain an explicit fail-closed seam
        # for fake/custom clients and future schema adapters.
        return BridgeCriticResultV1(
            assessment,
            None,
            None,
            None,
            llm_call_ref,
            "llm_critic_invalid_result",
        )
    llm_evidence = EvidenceRefV1(
        evidence_id=stored.structured_result_id,
        kind=EvidenceKind.LLM_RECOMMENDATION,
        producer=f"bridge_critic:{BRIDGE_CRITIC_VERSION}",
        content_hash=stored.output_content_hash,
    )
    evidence = tuple(sorted(
        {
            ref.evidence_id: ref
            for ref in (*assessment.evidence_refs, llm_evidence)
        }.values(),
        key=lambda ref: ref.evidence_id,
    ))
    namespace_codes = _reason_codes(output.get("namespace_reason_codes"))
    population_codes = _reason_codes(output.get("population_reason_codes"))
    explanations = list(assessment.explanation_codes)
    explanations.extend(f"llm_namespace:{code}" for code in namespace_codes)
    explanations.extend(f"llm_population:{code}" for code in population_codes)
    if namespace is NamespaceRecommendation.DIFFERENT:
        explanations.append("llm_recommends_different_namespace")
    elif namespace is NamespaceRecommendation.SAME:
        explanations.append("llm_recommends_same_namespace")

    # A model may make UNKNOWN/POSSIBLE worth probing, but it cannot mint governed SAME/DIFFERENT.
    deterministic_namespace = assessment.namespace_verdict
    if (
        deterministic_namespace is NamespaceVerdict.UNKNOWN
        and namespace in {NamespaceRecommendation.SAME, NamespaceRecommendation.POSSIBLE}
    ):
        deterministic_namespace = NamespaceVerdict.POSSIBLE
    updated = replace(
        assessment,
        namespace_verdict=deterministic_namespace,
        population_hypothesis=population.value,
        evidence_refs=evidence,
        explanation_codes=tuple(explanations),
        assessment_version=f"{assessment.assessment_version}+critic-v{BRIDGE_CRITIC_VERSION}",
    )
    return BridgeCriticResultV1(
        updated,
        namespace,
        population,
        stored.structured_result_id,
        llm_call_ref,
    )

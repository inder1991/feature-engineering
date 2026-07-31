"""Evidence-first governance read model for directional bridge realizations.

Identifier links and join realizations are deliberately separate records:

* a link is the symmetric semantic claim that two tuples identify the same namespace;
* a realization is one directional, predicate-scoped and binding-pinned way to join them.

Human review annotates either record.  It never substitutes for deterministic execution safety.
This module makes those independent axes explicit on the API wire.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from featuregen.contracts import DbConn
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.identity import EntityBridgeRef
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
)
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    AsOfIntervalRequirementV1,
    CardinalityBasis,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.bridge_store import (
    CurrentBridgeRealizationV1,
    load_current_bridge_realizations,
    load_current_candidate_assessments,
    revalidate_bridge_realization,
)
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import allowed_classes, visibility_predicate
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

_CONTRADICTION_PRIORITY = {
    "endpoint_not_found": 0,
    "different_governed_entity": 10,
    "different_governed_identifier_namespace": 20,
    "incompatible_representation_role": 30,
    "identifier_value_to_description_text": 40,
    "incompatible_type_family": 50,
    "representation_untestable": 60,
}


def _strongest_contradiction(conflicts: tuple[str, ...]) -> str | None:
    """Pick by an explicit severity policy, never by the contract's alphabetical storage order."""
    if not conflicts:
        return None
    return min(
        conflicts,
        key=lambda conflict: (_CONTRADICTION_PRIORITY.get(conflict, 100), conflict),
    )


def _endpoint_view(endpoint: IdentifierEndpointV1) -> dict[str, Any]:
    return {
        "logical_table_ref": endpoint.logical_table_ref,
        "members": [
            {
                "logical_column_ref": member.logical_column_ref,
                "data_type_family": member.data_type_family,
                "type_basis": member.type_basis.value,
                "key_member_role": member.key_member_role.value,
                "physical_column_id": member.physical_column_id,
            }
            for member in endpoint.members
        ],
        "entity_id": endpoint.entity_id,
        "concept": endpoint.concept,
        "concept_authority": endpoint.concept_authority.value,
        "tuple_key_role": endpoint.tuple_key_role.value,
        "physical_table_id": endpoint.physical_table_id,
        "binding_revision_id": endpoint.binding_revision_id,
    }


def _assessment_view(assessment: IdentifierLinkAssessmentV1 | None) -> dict[str, Any] | None:
    if assessment is None:
        return None
    return {
        "candidate_id": assessment.candidate_id,
        "candidate_revision_id": assessment.candidate_revision_id,
        "assessment_version": assessment.assessment_version,
        "namespace_verdict": assessment.namespace_verdict.value,
        "governed_population_relation": assessment.governed_population_relation.value,
        "population_hypothesis": assessment.population_hypothesis,
        "left_endpoint": _endpoint_view(assessment.left_endpoint),
        "right_endpoint": _endpoint_view(assessment.right_endpoint),
        "strongest_evidence": assessment.strongest_evidence_label,
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "kind": evidence.kind.value,
                "producer": evidence.producer,
                "observed_at": (
                    evidence.observed_at.isoformat() if evidence.observed_at else None
                ),
            }
            for evidence in assessment.evidence_refs
        ],
        "proposal_reasons": list(assessment.explanation_codes),
        "strongest_contradiction": _strongest_contradiction(assessment.hard_conflicts),
        "hard_conflicts": list(assessment.hard_conflicts),
    }


def current_assessment_views_by_bridge(conn: DbConn) -> dict[str, dict[str, Any]]:
    """Current typed assessment views keyed by their governed symmetric link."""
    return {
        assessment.bridge_fact_key: view
        for assessment in load_current_candidate_assessments(conn)
        if assessment.bridge_fact_key is not None
        if (view := _assessment_view(assessment)) is not None
    }


def _predicate_view(predicate: object) -> dict[str, Any]:
    if isinstance(predicate, FixedValueReferencePredicateV1):
        return {
            "kind": predicate.kind,
            "predicate_id": predicate.predicate_id,
            "logical_column_ref": predicate.logical_column_ref,
            "value_ref": predicate.value_ref,
        }
    if isinstance(predicate, AsOfIntervalRequirementV1):
        return {
            "kind": predicate.kind,
            "predicate_id": predicate.predicate_id,
            "effective_from_ref": predicate.effective_from_ref,
            "effective_to_ref": predicate.effective_to_ref,
            "as_of_value_ref": predicate.as_of_value_ref,
        }
    if isinstance(predicate, AdditionalKeyRequirementV1):
        return {
            "kind": predicate.kind,
            "from_logical_column_ref": predicate.from_logical_column_ref,
            "to_logical_column_ref": predicate.to_logical_column_ref,
            "reason_code": predicate.reason_code,
        }
    raise TypeError(f"unsupported bridge predicate {type(predicate).__name__}")


def _cardinality_label(realization: CurrentBridgeRealizationV1) -> str:
    revision = realization.revision
    value = revision.cardinality.value
    if value is None:
        return "Unknown — profile required"
    if value in {Cardinality.ONE_TO_MANY, Cardinality.MANY_TO_MANY}:
        return "N:N risk"
    if value is Cardinality.ONE_TO_ONE:
        return "1:1 — exact unique keys"
    if revision.predicates:
        if any(
            ref.endswith(".business_dt")
            for predicate in revision.predicates
            for ref in (
                getattr(predicate, "logical_column_ref", ""),
                getattr(predicate, "from_logical_column_ref", ""),
                getattr(predicate, "to_logical_column_ref", ""),
            )
        ):
            return "N:1 when business_dt matches"
        return "N:1 — predicate scoped"
    if revision.cardinality_basis is CardinalityBasis.GOVERNED_KEY:
        return "N:1 — governed complete key"
    return "N:1 — exact profile"


def _missing_requirements(realization: CurrentBridgeRealizationV1) -> list[dict[str, str]]:
    return [
        {
            "from_logical_column_ref": predicate.from_logical_column_ref,
            "to_logical_column_ref": predicate.to_logical_column_ref,
            "reason_code": predicate.reason_code,
        }
        for predicate in realization.revision.predicates
        if isinstance(predicate, AdditionalKeyRequirementV1)
    ]


def _observation_metrics(conn: DbConn, evidence_ids: list[str]) -> list[dict[str, Any]]:
    if not evidence_ids:
        return []
    rows = conn.execute(
        "SELECT r.observation_revision_id, r.observation_json, "
        "       EXISTS (SELECT 1 FROM relationship_observation_current c "
        "               WHERE c.observation_revision_id=r.observation_revision_id) AS is_current "
        "FROM relationship_observation_revision r "
        "WHERE r.observation_revision_id = ANY(%s) "
        "ORDER BY r.observed_at DESC",
        (evidence_ids,),
    ).fetchall()
    metrics: list[dict[str, Any]] = []
    for observation_id, payload, is_current in rows:
        left = payload.get("left") or {}
        right = payload.get("right") or {}
        metrics.append({
            "observation_revision_id": observation_id,
            "is_current": bool(is_current),
            "method": payload.get("method"),
            "row_coverage": payload.get("row_coverage"),
            "complete": payload.get("complete"),
            "left_row_count": left.get("row_count"),
            "right_row_count": right.get("row_count"),
            "left_distinct_tuple_count": left.get("distinct_tuple_count"),
            "right_distinct_tuple_count": right.get("distinct_tuple_count"),
            "right_duplicate_row_count": right.get("duplicate_row_count"),
            "matched_left_distinct": payload.get("matched_left_distinct"),
            "unmatched_left_distinct": payload.get("unmatched_left_distinct"),
            "joined_row_count": payload.get("joined_row_count"),
            "max_right_matches_per_left_row": payload.get(
                "max_right_matches_per_left_row"),
            "observed_at": payload.get("observed_at"),
        })
    return metrics


def _endpoint_hidden(
    conn: DbConn,
    endpoint: IdentifierEndpointV1,
    allowed: list[str],
) -> bool:
    for member in endpoint.members:
        source, _schema, table, column = parse_ref(member.logical_column_ref)
        row = conn.execute(
            f"SELECT ({visibility_predicate()}) FROM graph_node "
            "WHERE catalog_source=%s AND object_ref=%s AND kind='column'",
            (allowed, source, f"public.{table}.{column}"),
        ).fetchone()
        if row is not None and not row[0]:
            return True
    return False


def realization_governance_view(
    conn: DbConn,
    realization: CurrentBridgeRealizationV1,
    *,
    assessment: dict[str, Any] | None,
) -> dict[str, Any]:
    revision = realization.revision
    scope = revision.applicability_scope
    purpose = scope.purposes[0]
    validation = revalidate_bridge_realization(
        conn,
        realization,
        purpose=purpose,
        environment=scope.environment,
        execution_tier=scope.execution_tier,
    )
    exact_ids = [
        evidence.evidence_id
        for evidence in revision.evidence_refs
        if evidence.kind.value == "exact_profile"
    ]
    evidence_fresh = not any(
        reason in {
            "exact_relationship_evidence_missing",
            "exact_relationship_evidence_not_current",
            "dependency_snapshot_mismatch",
        }
        for reason in validation.reason_codes
    )
    return {
        "realization_id": revision.realization_id,
        "realization_revision_id": revision.realization_revision_id,
        "bridge_fact_key": revision.bridge_fact_key,
        "direction": {
            "from": revision.from_endpoint.logical_table_ref,
            "to": revision.to_endpoint.logical_table_ref,
        },
        "from_endpoint": _endpoint_view(revision.from_endpoint),
        "to_endpoint": _endpoint_view(revision.to_endpoint),
        "column_pairs": [
            {
                "from_logical_column_ref": pair.from_logical_column_ref,
                "to_logical_column_ref": pair.to_logical_column_ref,
            }
            for pair in revision.column_pairs
        ],
        "cardinality": (
            revision.cardinality.value.value if revision.cardinality.value else "unknown"
        ),
        "cardinality_label": _cardinality_label(realization),
        "cardinality_basis": revision.cardinality_basis.value,
        "predicates": [_predicate_view(predicate) for predicate in revision.predicates],
        "missing_requirements": _missing_requirements(realization),
        "applicability_scope": {
            "scope_id": scope.scope_id,
            "execution_tier": scope.execution_tier.value,
            "purposes": list(scope.purposes),
            "environment": scope.environment,
            "partition_scope_ref": scope.partition_scope_ref,
        },
        "dependency_snapshot_id": revision.dependency_snapshot_id,
        "safety_status": realization.current.safety_status.value,
        "review_status": realization.current.review_status.value,
        "lifecycle": realization.current.lifecycle.value,
        "pointer_version": realization.current.pointer_version,
        "execution_eligible": validation.executable,
        "execution_reason_codes": list(validation.reason_codes),
        "evidence_fresh": evidence_fresh,
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "kind": evidence.kind.value,
                "producer": evidence.producer,
                "observed_at": (
                    evidence.observed_at.isoformat() if evidence.observed_at else None
                ),
            }
            for evidence in revision.evidence_refs
        ],
        "metrics": _observation_metrics(conn, exact_ids),
        "assessment": assessment,
        "available_review_actions": ["confirm", "reject"],
        "profile_action": (
            None
            if evidence_fresh
            else {
                "state": "external_run_required",
                "label": "Run bounded profile in the data environment",
            }
        ),
        "review_controls_execution": False,
    }


def list_bridge_realization_views(
    conn: DbConn,
    *,
    source: str | None = None,
    bridge_fact_key: str | None = None,
    roles: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """List current directional realizations without widening endpoint visibility."""
    assessments = current_assessment_views_by_bridge(conn)
    allowed = allowed_classes(roles) if roles is not None else None
    wanted_source = source.strip().lower() if source else None
    views: list[dict[str, Any]] = []
    for realization in load_current_bridge_realizations(
        conn, bridge_fact_key=bridge_fact_key
    ):
        revision = realization.revision
        sources = {
            parse_ref(revision.from_endpoint.logical_table_ref)[0],
            parse_ref(revision.to_endpoint.logical_table_ref)[0],
        }
        if wanted_source is not None and wanted_source not in sources:
            continue
        if allowed is not None and (
            _endpoint_hidden(conn, revision.from_endpoint, allowed)
            or _endpoint_hidden(conn, revision.to_endpoint, allowed)
        ):
            continue
        views.append(realization_governance_view(
            conn,
            realization,
            assessment=assessments.get(revision.bridge_fact_key),
        ))
    return views


def link_authority_view(conn: DbConn, ref: EntityBridgeRef) -> dict[str, Any]:
    """The actual authority configured for the symmetric link route."""
    authority = resolve_authority(
        conn, current_catalog_adapter(), ref, "entity_bridge"
    )
    return {
        "role": authority.role,
        "gate": authority.gate,
        "subjects": list(authority.subjects),
        "dual": authority.dual,
        "governance_queue": authority.governance_queue,
        "confirmation_count": 2 if authority.dual else 1,
    }

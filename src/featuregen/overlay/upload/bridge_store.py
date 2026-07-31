"""Immutable identifier-link assessment and directional-realization persistence.

The generic overlay stream owns link lifecycle.  This store owns typed assessment/realization
revision history and CAS-protected current pointers; the legacy candidate ledger and edge table are
compatibility projections only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
)
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    KeyMemberRole,
    LinkAvailability,
    LinkReviewStatus,
    NamespaceVerdict,
    PopulationRelation,
    TupleKeyRole,
    TypeBasis,
    candidate_identity_payload,
    read_overlay_identifier_link_state,
)
from featuregen.overlay.upload.bridge_realization import (
    AdditionalKeyRequirementV1,
    AsOfIntervalRequirementV1,
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
    eligible_for_production,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality


class BridgeStoreConflict(RuntimeError):
    """A current pointer advanced after the caller took its dependency snapshot."""


class BridgeStoreCorruption(RuntimeError):
    """A stable identity was already stored with different immutable content."""


@dataclass(frozen=True, slots=True)
class CandidateCurrentPointerV1:
    candidate_id: str
    candidate_revision_id: str
    pointer_version: int
    lifecycle: str = "active"


@dataclass(frozen=True, slots=True)
class BridgeDependencyRefV1:
    kind: str
    key: str
    revision: str

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.key.strip() or not self.revision.strip():
            raise ValueError("bridge dependency kind, key and revision must not be blank")


def bridge_dependency_snapshot_id(
    dependencies: tuple[BridgeDependencyRefV1, ...],
) -> str:
    """Content identity of the exact dependency set a realization was admitted against."""
    payload = [
        {
            "kind": dependency.kind,
            "key": dependency.key,
            "revision": dependency.revision,
        }
        for dependency in sorted(
            dependencies,
            key=lambda item: (item.kind, item.key, item.revision),
        )
    ]
    return "brds_" + canonical_hash(payload)


@dataclass(frozen=True, slots=True)
class CurrentBridgeRealizationV1:
    """One typed realization revision joined to its mutable current pointer and dependencies."""

    revision: BridgeJoinRealizationRevisionV1
    current: BridgeRealizationCurrentV1
    dependencies: tuple[BridgeDependencyRefV1, ...]


@dataclass(frozen=True, slots=True)
class BridgeExecutionAssessmentV1:
    """The result of re-reading every load-bearing execution dependency.

    ``executable`` deliberately does not inspect human review.  Review is an accountability
    annotation; execution is decided by deterministic safety, current evidence, lifecycle, scope
    and physical binding revisions.
    """

    realization: CurrentBridgeRealizationV1
    executable: bool
    reason_codes: tuple[str, ...]


_EXECUTION_BLOCKING_CARDINALITIES = frozenset(
    {Cardinality.ONE_TO_MANY, Cardinality.MANY_TO_MANY}
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _physical_identity(payload: dict[str, Any] | None) -> PhysicalObjectIdentityV1 | None:
    return None if payload is None else PhysicalObjectIdentityV1(**payload)


def _physical_binding(payload: dict[str, Any] | None) -> PhysicalDatasetBindingV1 | None:
    if payload is None:
        return None
    return PhysicalDatasetBindingV1(
        binding_id=payload["binding_id"],
        catalog_logical_ref=payload["catalog_logical_ref"],
        connection_id=payload["connection_id"],
        identity=PhysicalObjectIdentityV1(**payload["identity"]),
        partition_columns=tuple(payload.get("partition_columns") or ()),
        business_time_column=payload.get("business_time_column"),
        purposes=tuple(payload.get("purposes") or ()),
    )


def _evidence_ref(payload: dict[str, Any]) -> EvidenceRefV1:
    observed = payload.get("observed_at")
    return EvidenceRefV1(
        evidence_id=payload["evidence_id"],
        kind=EvidenceKind(payload["kind"]),
        producer=payload["producer"],
        content_hash=payload.get("content_hash"),
        observed_at=datetime.fromisoformat(observed) if observed else None,
    )


def _endpoint(payload: dict[str, Any]) -> IdentifierEndpointV1:
    members = tuple(
        IdentifierColumnMemberV1(
            logical_column_ref=member["logical_column_ref"],
            data_type_family=member["data_type_family"],
            type_basis=TypeBasis(member["type_basis"]),
            key_member_role=KeyMemberRole(member["key_member_role"]),
            physical_identity=_physical_identity(member.get("physical_identity")),
        )
        for member in payload["members"]
    )
    return IdentifierEndpointV1(
        logical_table_ref=payload["logical_table_ref"],
        members=members,
        entity_id=payload.get("entity_id"),
        concept=payload.get("concept"),
        concept_authority=ConceptAuthority(payload["concept_authority"]),
        tuple_key_role=TupleKeyRole(payload["tuple_key_role"]),
        physical_binding=_physical_binding(payload.get("physical_binding")),
        binding_revision_id=payload.get("binding_revision_id"),
    )


def assessment_to_json(assessment: IdentifierLinkAssessmentV1) -> dict[str, Any]:
    return _jsonable(asdict(assessment))


def assessment_from_json(payload: dict[str, Any]) -> IdentifierLinkAssessmentV1:
    if payload.get("legacy") is True:
        raise ValueError("legacy compatibility rows are not typed assessment revisions")
    return IdentifierLinkAssessmentV1(
        left_endpoint=_endpoint(payload["left_endpoint"]),
        right_endpoint=_endpoint(payload["right_endpoint"]),
        namespace_verdict=NamespaceVerdict(payload["namespace_verdict"]),
        governed_population_relation=PopulationRelation(
            payload["governed_population_relation"]),
        assessment_version=payload["assessment_version"],
        candidate_family=payload["candidate_family"],
        bridge_fact_key=payload.get("bridge_fact_key"),
        population_hypothesis=payload.get("population_hypothesis"),
        evidence_refs=tuple(_evidence_ref(item) for item in payload.get("evidence_refs") or ()),
        hard_conflicts=tuple(payload.get("hard_conflicts") or ()),
        explanation_codes=tuple(payload.get("explanation_codes") or ()),
    )


def _predicate_from_json(payload: dict[str, Any]):
    kind = payload.get("kind")
    if kind == "fixed_value_reference":
        return FixedValueReferencePredicateV1(
            predicate_id=payload["predicate_id"],
            logical_column_ref=payload["logical_column_ref"],
            value_ref=payload["value_ref"],
        )
    if kind == "as_of_interval":
        return AsOfIntervalRequirementV1(
            predicate_id=payload["predicate_id"],
            effective_from_ref=payload["effective_from_ref"],
            effective_to_ref=payload["effective_to_ref"],
            as_of_value_ref=payload["as_of_value_ref"],
        )
    if kind == "unresolved_additional_key":
        return AdditionalKeyRequirementV1(
            from_logical_column_ref=payload["from_logical_column_ref"],
            to_logical_column_ref=payload["to_logical_column_ref"],
            reason_code=payload["reason_code"],
        )
    raise ValueError(f"unknown bridge realization predicate kind {kind!r}")


def realization_from_json(payload: dict[str, Any]) -> BridgeJoinRealizationRevisionV1:
    """Rebuild a typed revision and thereby re-check its content-addressed identities."""
    cardinality_payload = payload.get("cardinality")
    cardinality = (
        cardinality_payload.get("value")
        if isinstance(cardinality_payload, dict)
        else cardinality_payload
    )
    return BridgeJoinRealizationRevisionV1(
        bridge_fact_key=payload["bridge_fact_key"],
        from_endpoint=_endpoint(payload["from_endpoint"]),
        to_endpoint=_endpoint(payload["to_endpoint"]),
        column_pairs=tuple(
            ColumnPairV1(
                pair["from_logical_column_ref"],
                pair["to_logical_column_ref"],
            )
            for pair in payload["column_pairs"]
        ),
        predicates=tuple(
            _predicate_from_json(predicate)
            for predicate in payload.get("predicates") or ()
        ),
        applicability_scope=RealizationApplicabilityScopeV1(
            scope_id=payload["applicability_scope"]["scope_id"],
            execution_tier=ExecutionTier(
                payload["applicability_scope"]["execution_tier"]),
            purposes=tuple(payload["applicability_scope"]["purposes"]),
            environment=payload["applicability_scope"]["environment"],
            partition_scope_ref=payload["applicability_scope"].get(
                "partition_scope_ref"),
        ),
        cardinality=DirectionalCardinalityVerdictV1(
            None if cardinality in {None, "unknown"} else Cardinality(str(cardinality))
        ),
        cardinality_basis=CardinalityBasis(payload["cardinality_basis"]),
        evidence_refs=tuple(
            _evidence_ref(item) for item in payload.get("evidence_refs") or ()
        ),
        dependency_snapshot_id=payload["dependency_snapshot_id"],
        derivation_version=payload["derivation_version"],
        admission_policy_version=payload["admission_policy_version"],
    )


def _current_candidate(conn, candidate_id: str) -> CandidateCurrentPointerV1 | None:
    row = conn.execute(
        "SELECT candidate_revision_id, pointer_version, lifecycle "
        "FROM governed_candidate_current WHERE candidate_id = %s",
        (candidate_id,),
    ).fetchone()
    return (
        None
        if row is None else CandidateCurrentPointerV1(
            candidate_id, row[0], int(row[1]), str(row[2]))
    )


def load_candidate_pointer_versions(conn) -> dict[str, int]:
    """Snapshot every candidate pointer before an assessment batch starts.

    Callers pass the captured version back to :func:`record_candidate_assessment`; work that
    started against an older snapshot then cannot overwrite a newer completed assessment.
    """
    return {
        candidate_id: int(pointer_version)
        for candidate_id, pointer_version in conn.execute(
            "SELECT candidate_id, pointer_version FROM governed_candidate_current"
        ).fetchall()
    }


def record_candidate_assessment(
    conn,
    assessment: IdentifierLinkAssessmentV1,
    *,
    expected_pointer_version: int | None = None,
) -> CandidateCurrentPointerV1:
    """Append an immutable assessment and CAS-advance its logical candidate pointer."""
    identity = candidate_identity_payload(
        assessment.candidate_family,
        assessment.left_endpoint,
        assessment.right_endpoint,
    )
    assessment_json = assessment_to_json(assessment)
    conn.execute(
        "INSERT INTO governed_candidate_identity "
        "(candidate_id, candidate_family, logical_identity_json) VALUES (%s,%s,%s) "
        "ON CONFLICT (candidate_id) DO NOTHING",
        (assessment.candidate_id, assessment.candidate_family, Jsonb(identity)),
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT candidate_family, logical_identity_json "
            "FROM governed_candidate_identity WHERE candidate_id = %s",
            (assessment.candidate_id,),
        )
        stored_identity = cur.fetchone()
    if (
        stored_identity is None
        or stored_identity["candidate_family"] != assessment.candidate_family
        or stored_identity["logical_identity_json"] != identity
    ):
        raise BridgeStoreCorruption(
            f"candidate identity collision for {assessment.candidate_id}")
    conn.execute(
        "INSERT INTO governed_candidate_revision "
        "(candidate_revision_id, candidate_id, assessment_json, assessment_version) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (candidate_revision_id) DO NOTHING",
        (
            assessment.candidate_revision_id,
            assessment.candidate_id,
            Jsonb(assessment_json),
            assessment.assessment_version,
        ),
    )
    stored_revision = conn.execute(
        "SELECT assessment_json, assessment_version "
        "FROM governed_candidate_revision WHERE candidate_revision_id = %s",
        (assessment.candidate_revision_id,),
    ).fetchone()
    if (
        stored_revision is None
        or stored_revision[0] != assessment_json
        or stored_revision[1] != assessment.assessment_version
    ):
        raise BridgeStoreCorruption(
            f"candidate revision collision for {assessment.candidate_revision_id}")
    current = _current_candidate(conn, assessment.candidate_id)
    if current is not None and current.candidate_revision_id == assessment.candidate_revision_id:
        if (
            expected_pointer_version is not None
            and expected_pointer_version != current.pointer_version
        ):
            raise BridgeStoreConflict(
                f"candidate {assessment.candidate_id} pointer advanced")
        if current.lifecycle == "active":
            return current
        changed = conn.execute(
            "UPDATE governed_candidate_current "
            "SET lifecycle='active', pointer_version=pointer_version+1, updated_at=now() "
            "WHERE candidate_id=%s AND candidate_revision_id=%s "
            "AND pointer_version=%s AND lifecycle='withdrawn'",
            (
                assessment.candidate_id,
                assessment.candidate_revision_id,
                current.pointer_version,
            ),
        ).rowcount
        if changed != 1:
            raise BridgeStoreConflict(
                f"candidate {assessment.candidate_id} pointer advanced")
        return CandidateCurrentPointerV1(
            assessment.candidate_id,
            assessment.candidate_revision_id,
            current.pointer_version + 1,
            "active",
        )
    expected = (
        (0 if current is None else current.pointer_version)
        if expected_pointer_version is None
        else expected_pointer_version
    )
    if expected == 0:
        changed = conn.execute(
            "INSERT INTO governed_candidate_current "
            "(candidate_id, candidate_revision_id, pointer_version, lifecycle) "
            "VALUES (%s,%s,1,'active') "
            "ON CONFLICT (candidate_id) DO NOTHING",
            (assessment.candidate_id, assessment.candidate_revision_id),
        ).rowcount
        version = 1
    else:
        changed = conn.execute(
            "UPDATE governed_candidate_current "
            "SET candidate_revision_id = %s, pointer_version = pointer_version + 1, "
            "lifecycle='active', updated_at = now() "
            "WHERE candidate_id = %s AND pointer_version = %s",
            (assessment.candidate_revision_id, assessment.candidate_id, expected),
        ).rowcount
        version = expected + 1
    if changed != 1:
        raise BridgeStoreConflict(
            f"candidate {assessment.candidate_id} pointer advanced")
    return CandidateCurrentPointerV1(
        assessment.candidate_id, assessment.candidate_revision_id, version, "active")


def withdraw_missing_candidate_assessments(
    conn,
    retained_candidate_ids: frozenset[str],
) -> tuple[int, int]:
    """Withdraw active identifier-link candidates absent from one complete global derivation.

    The immutable identity/revision rows and generic overlay event stream stay as history.  Only the
    mutable current pointer changes, and any active directional realization is demoted immediately.
    This function must only be called after a complete derivation; callers skip it when derivation
    itself fails.
    """
    rows = conn.execute(
        "SELECT c.candidate_id, c.pointer_version, "
        "coalesce(r.assessment_json->>'bridge_fact_key', "
        "         r.assessment_json->>'fact_key') AS bridge_fact_key "
        "FROM governed_candidate_current c "
        "JOIN governed_candidate_revision r "
        "  ON r.candidate_revision_id=c.candidate_revision_id "
        "JOIN governed_candidate_identity i ON i.candidate_id=c.candidate_id "
        "WHERE i.candidate_family='identifier_link' AND c.lifecycle='active' "
        "ORDER BY c.candidate_id"
    ).fetchall()
    withdrawn = 0
    realizations = 0
    for candidate_id, pointer_version, bridge_fact_key in rows:
        if candidate_id in retained_candidate_ids:
            continue
        changed = conn.execute(
            "UPDATE governed_candidate_current "
            "SET lifecycle='withdrawn', pointer_version=pointer_version+1, updated_at=now() "
            "WHERE candidate_id=%s AND pointer_version=%s AND lifecycle='active'",
            (candidate_id, pointer_version),
        ).rowcount
        if changed != 1:
            raise BridgeStoreConflict(f"candidate {candidate_id} pointer advanced")
        withdrawn += 1
        if bridge_fact_key:
            realizations += demote_realizations_for_bridge(
                conn, str(bridge_fact_key), lifecycle="stale")
    return withdrawn, realizations


def bridge_candidate_currentness(conn, bridge_fact_key: str) -> bool | None:
    """Whether a bridge backed by a governed candidate remains current.

    ``None`` means the bridge has no candidate record (for example a directly governed legacy
    bridge), so callers preserve its generic lifecycle behavior.  ``False`` is an explicit
    automatic withdrawal and must fail closed for discovery and execution.
    """
    rows = conn.execute(
        "SELECT c.lifecycle "
        "FROM governed_candidate_current c "
        "JOIN governed_candidate_revision r "
        "  ON r.candidate_revision_id=c.candidate_revision_id "
        "WHERE coalesce(r.assessment_json->>'bridge_fact_key', "
        "               r.assessment_json->>'fact_key')=%s",
        (bridge_fact_key,),
    ).fetchall()
    if not rows:
        return None
    return any(str(lifecycle) == "active" for (lifecycle,) in rows)


def load_current_candidate_assessments(
    conn,
) -> tuple[IdentifierLinkAssessmentV1, ...]:
    """Read active modern current assessments; legacy and withdrawn revisions are history."""
    rows = conn.execute(
        "SELECT r.assessment_json "
        "FROM governed_candidate_current c "
        "JOIN governed_candidate_revision r "
        "  ON r.candidate_revision_id = c.candidate_revision_id "
        "WHERE c.lifecycle='active' "
        "ORDER BY c.candidate_id"
    ).fetchall()
    out: list[IdentifierLinkAssessmentV1] = []
    for (payload,) in rows:
        if payload.get("legacy") is True:
            continue
        out.append(assessment_from_json(payload))
    return tuple(out)


def record_realization_revision(
    conn,
    revision: BridgeJoinRealizationRevisionV1,
    current: BridgeRealizationCurrentV1,
    *,
    dependencies: tuple[BridgeDependencyRefV1, ...],
    expected_pointer_version: int = 0,
) -> None:
    """Append a realization revision/dependencies and CAS-publish its typed current pointer."""
    if current.realization_revision_id != revision.realization_revision_id:
        raise ValueError("current pointer must name the supplied realization revision")
    conn.execute(
        "INSERT INTO bridge_join_realization_revision "
        "(realization_revision_id, realization_id, bridge_fact_key, realization_json, "
        " dependency_snapshot_id) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (realization_revision_id) DO NOTHING",
        (
            revision.realization_revision_id,
            revision.realization_id,
            revision.bridge_fact_key,
            Jsonb(_jsonable(asdict(revision))),
            revision.dependency_snapshot_id,
        ),
    )
    stored_revision = conn.execute(
        "SELECT realization_id, bridge_fact_key, realization_json, dependency_snapshot_id "
        "FROM bridge_join_realization_revision WHERE realization_revision_id = %s",
        (revision.realization_revision_id,),
    ).fetchone()
    expected_revision = (
        revision.realization_id,
        revision.bridge_fact_key,
        _jsonable(asdict(revision)),
        revision.dependency_snapshot_id,
    )
    if stored_revision is None or tuple(stored_revision) != expected_revision:
        raise BridgeStoreCorruption(
            f"realization revision collision for {revision.realization_revision_id}")
    for dependency in dependencies:
        conn.execute(
            "INSERT INTO bridge_realization_dependency "
            "(realization_revision_id, dependency_kind, dependency_key, dependency_revision) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                revision.realization_revision_id,
                dependency.kind,
                dependency.key,
                dependency.revision,
            ),
        )
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO bridge_join_realization_current "
            "(realization_id, realization_revision_id, safety_status, review_status, lifecycle, "
            " pointer_version) VALUES (%s,%s,%s,%s,%s,1) "
            "ON CONFLICT (realization_id) DO NOTHING",
            (
                current.realization_id,
                current.realization_revision_id,
                current.safety_status.value,
                current.review_status.value,
                current.lifecycle.value,
            ),
        ).rowcount
    else:
        changed = conn.execute(
            "UPDATE bridge_join_realization_current SET "
            "realization_revision_id=%s, safety_status=%s, review_status=%s, lifecycle=%s, "
            "pointer_version=pointer_version+1, updated_at=now() "
            "WHERE realization_id=%s AND pointer_version=%s",
            (
                current.realization_revision_id,
                current.safety_status.value,
                current.review_status.value,
                current.lifecycle.value,
                current.realization_id,
                expected_pointer_version,
            ),
        ).rowcount
    if changed != 1:
        raise BridgeStoreConflict(
            f"realization {current.realization_id} pointer advanced")


def load_current_bridge_realizations(
    conn,
    *,
    bridge_fact_key: str | None = None,
) -> tuple[CurrentBridgeRealizationV1, ...]:
    """Load current realization pointers as typed, content-verified contracts.

    Corrupt JSON or an identity mismatch is not skipped.  A supposedly current execution contract
    that cannot reproduce the primary keys stored beside it is a store-integrity failure, and
    silently omitting it would turn corruption into an unexplained missing join.
    """
    where = "" if bridge_fact_key is None else "WHERE r.bridge_fact_key = %s"
    params: tuple[object, ...] = () if bridge_fact_key is None else (bridge_fact_key,)
    rows = conn.execute(
        "SELECT r.realization_revision_id, r.realization_id, r.realization_json, "
        "c.safety_status, c.review_status, c.lifecycle, c.pointer_version "
        "FROM bridge_join_realization_current c "
        "JOIN bridge_join_realization_revision r "
        "  ON r.realization_revision_id=c.realization_revision_id "
        f"{where} ORDER BY r.realization_id",
        params,
    ).fetchall()
    out: list[CurrentBridgeRealizationV1] = []
    for (
        stored_revision_id,
        stored_realization_id,
        payload,
        safety_status,
        review_status,
        lifecycle,
        pointer_version,
    ) in rows:
        revision = realization_from_json(payload)
        if (
            revision.realization_id != stored_realization_id
            or revision.realization_revision_id != stored_revision_id
        ):
            raise BridgeStoreCorruption(
                f"realization identity mismatch for {stored_revision_id}")
        current = BridgeRealizationCurrentV1(
            realization_id=stored_realization_id,
            realization_revision_id=stored_revision_id,
            safety_status=SafetyStatus(safety_status),
            review_status=LinkReviewStatus(review_status),
            lifecycle=RealizationLifecycle(lifecycle),
            pointer_version=int(pointer_version),
        )
        dependencies = tuple(
            BridgeDependencyRefV1(kind, key, dependency_revision)
            for kind, key, dependency_revision in conn.execute(
                "SELECT dependency_kind, dependency_key, dependency_revision "
                "FROM bridge_realization_dependency "
                "WHERE realization_revision_id=%s "
                "ORDER BY dependency_kind, dependency_key, dependency_revision",
                (stored_revision_id,),
            ).fetchall()
        )
        out.append(CurrentBridgeRealizationV1(revision, current, dependencies))
    return tuple(out)


def _binding_revision_is_stored(conn, endpoint: IdentifierEndpointV1) -> bool:
    binding = endpoint.physical_binding
    revision_id = endpoint.binding_revision_id
    if binding is None or revision_id is None:
        return False
    row = conn.execute(
        "SELECT binding_id, content_hash, catalog_logical_ref, connection_id, physical_id "
        "FROM physical_dataset_binding_revision WHERE binding_revision_id=%s",
        (revision_id,),
    ).fetchone()
    return row == (
        binding.binding_id,
        binding.content_hash,
        binding.catalog_logical_ref,
        binding.connection_id,
        binding.identity.table_id,
    )


def _current_exact_evidence_ids(
    conn,
    revision: BridgeJoinRealizationRevisionV1,
    evidence_ids: frozenset[str],
) -> frozenset[str]:
    """Current exact observations that structurally attest this realization.

    The observation necessarily names the *pre-admission* realization revision it tested.  Adding
    that observation to ``evidence_refs`` creates the admitted revision ID, so requiring the
    observation to name the admitted revision would be a content-addressing cycle.  We therefore
    bind by immutable observation ID and re-check every execution-bearing field against the final
    revision: ordered tuple, binding revisions, scope, predicates, completeness and fan-out.
    """
    if not evidence_ids:
        return frozenset()
    from featuregen.data_agent.relationship_observation import observation_from_json

    from_columns = tuple(
        pair.from_logical_column_ref.rsplit(".", 1)[-1]
        for pair in revision.column_pairs
    )
    to_columns = tuple(
        pair.to_logical_column_ref.rsplit(".", 1)[-1]
        for pair in revision.column_pairs
    )
    expected_predicate_ids = tuple(
        predicate.predicate_id
        for predicate in revision.predicates
        if isinstance(
            predicate,
            FixedValueReferencePredicateV1 | AsOfIntervalRequirementV1,
        )
    )
    current: set[str] = set()
    rows = conn.execute(
        "SELECT r.observation_revision_id, r.observation_json "
        "FROM relationship_observation_current c "
        "JOIN relationship_observation_revision r "
        "  ON r.observation_revision_id=c.observation_revision_id "
        "WHERE r.observation_revision_id = ANY(%s) "
        "  AND r.method='exact' AND r.complete=true AND r.row_coverage='full' "
        "  AND r.conflict_observed=false",
        (list(evidence_ids),),
    ).fetchall()
    for observation_id, payload in rows:
        observation = observation_from_json(payload)
        if (
            observation.scope_id == revision.applicability_scope.scope_id
            and observation.left.binding_revision_id
            == revision.from_endpoint.binding_revision_id
            and observation.right.binding_revision_id
            == revision.to_endpoint.binding_revision_id
            and observation.left.columns == from_columns
            and observation.right.columns == to_columns
            and observation.predicate_ids == expected_predicate_ids
            and observation.right.duplicate_row_count == 0
            and observation.right.null_row_count == 0
            and observation.max_right_matches_per_left_row <= 1
        ):
            current.add(str(observation_id))
    return frozenset(current)


def revalidate_bridge_realization(
    conn,
    realization: CurrentBridgeRealizationV1,
    *,
    purpose: str,
    environment: str,
    execution_tier: ExecutionTier = ExecutionTier.PRODUCTION,
) -> BridgeExecutionAssessmentV1:
    """Re-read the mutable facts that may have changed since admission.

    This is the compilation/execution boundary, not a review check.  The exact current pointer,
    deterministic safety, backing link lifecycle, physical bindings, scope and current exact
    relationship observation all have to agree.  Unknown and fan-out cardinalities fail closed.
    """
    revision = realization.revision
    current = realization.current
    reasons: list[str] = []
    if not eligible_for_production(revision, current):
        reasons.append("realization_not_deterministically_executable")
    if revision.applicability_scope.execution_tier is not execution_tier:
        reasons.append("realization_execution_tier_mismatch")
    if environment != revision.applicability_scope.environment:
        reasons.append("realization_environment_mismatch")
    if purpose not in revision.applicability_scope.purposes:
        reasons.append("realization_purpose_mismatch")

    link_state = read_overlay_identifier_link_state(conn, revision.bridge_fact_key)
    if link_state.availability is not LinkAvailability.AVAILABLE:
        reasons.append("identifier_link_not_available")
    if bridge_candidate_currentness(conn, revision.bridge_fact_key) is False:
        reasons.append("identifier_link_candidate_withdrawn")

    if not _binding_revision_is_stored(conn, revision.from_endpoint):
        reasons.append("from_binding_revision_not_stored")
    if not _binding_revision_is_stored(conn, revision.to_endpoint):
        reasons.append("to_binding_revision_not_stored")

    if not revision.cardinality.known:
        reasons.append("directional_cardinality_unknown")
    elif revision.cardinality.value in _EXECUTION_BLOCKING_CARDINALITIES:
        reasons.append("directional_cardinality_fans_out")
    if revision.has_unresolved_requirements:
        reasons.append("realization_has_unresolved_requirements")

    if revision.dependency_snapshot_id != bridge_dependency_snapshot_id(
        realization.dependencies
    ):
        reasons.append("dependency_snapshot_mismatch")

    required_binding_dependencies = {
        (
            endpoint.physical_binding.binding_id,
            endpoint.binding_revision_id,
        )
        for endpoint in (revision.from_endpoint, revision.to_endpoint)
        if endpoint.physical_binding is not None
    }
    actual_binding_dependencies = {
        (dependency.key, dependency.revision)
        for dependency in realization.dependencies
        if dependency.kind == "physical_binding"
    }
    if not required_binding_dependencies <= actual_binding_dependencies:
        reasons.append("physical_binding_dependency_missing")

    exact_evidence = {
        ref.evidence_id
        for ref in revision.evidence_refs
        if ref.kind is EvidenceKind.EXACT_PROFILE
    }
    current_exact = _current_exact_evidence_ids(
        conn, revision, frozenset(exact_evidence))
    if not exact_evidence:
        reasons.append("exact_relationship_evidence_missing")
    elif not exact_evidence & current_exact:
        reasons.append("exact_relationship_evidence_not_current")
    evidence_dependencies = {
        (dependency.key, dependency.revision)
        for dependency in realization.dependencies
        if dependency.kind == "relationship_observation"
    }
    required_evidence_dependencies = {
        (ref.evidence_id, ref.content_hash or ref.evidence_id)
        for ref in revision.evidence_refs
        if ref.kind is EvidenceKind.EXACT_PROFILE
    }
    if not required_evidence_dependencies <= evidence_dependencies:
        reasons.append("relationship_evidence_dependency_missing")

    bridge_dependencies = {
        dependency.revision
        for dependency in realization.dependencies
        if dependency.kind == "bridge_fact"
        and dependency.key == revision.bridge_fact_key
    }
    if (
        link_state.overlay_head_event_id is None
        or not bridge_dependencies
    ):
        reasons.append("bridge_dependency_missing")
    elif link_state.overlay_head_event_id not in bridge_dependencies:
        reasons.append("bridge_dependency_revision_changed")

    return BridgeExecutionAssessmentV1(
        realization=realization,
        executable=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
    )


def executable_bridge_realizations(
    conn,
    *,
    purpose: str,
    environment: str,
    bridge_fact_key: str | None = None,
) -> tuple[CurrentBridgeRealizationV1, ...]:
    """Production reader: only current, deterministically safe directional realizations."""
    assessments = (
        revalidate_bridge_realization(
            conn,
            realization,
            purpose=purpose,
            environment=environment,
            execution_tier=ExecutionTier.PRODUCTION,
        )
        for realization in load_current_bridge_realizations(
            conn, bridge_fact_key=bridge_fact_key)
    )
    return tuple(
        assessment.realization
        for assessment in assessments
        if assessment.executable
    )


def demote_realizations_for_bridge(
    conn,
    bridge_fact_key: str,
    *,
    lifecycle: str,
) -> int:
    """Immediately withdraw active directional realizations backed by one link lifecycle."""
    if lifecycle not in {"stale", "rejected"}:
        raise ValueError("bridge demotion lifecycle must be stale or rejected")
    changed = conn.execute(
        "UPDATE bridge_join_realization_current c "
        "SET lifecycle=%s, pointer_version=pointer_version+1, updated_at=now() "
        "FROM bridge_join_realization_revision r "
        "WHERE r.realization_revision_id=c.realization_revision_id "
        "AND r.bridge_fact_key=%s AND c.lifecycle='active'",
        (lifecycle, bridge_fact_key),
    )
    return changed.rowcount


def demote_realizations_for_dependency(
    conn,
    dependency_kind: str,
    dependency_key: str,
) -> int:
    """Withdraw current realizations whose load-bearing dependency changed at any revision."""
    changed = conn.execute(
        "UPDATE bridge_join_realization_current c "
        "SET lifecycle='stale', pointer_version=pointer_version+1, updated_at=now() "
        "WHERE c.lifecycle='active' AND EXISTS ("
        "  SELECT 1 FROM bridge_realization_dependency d "
        "  WHERE d.realization_revision_id=c.realization_revision_id "
        "    AND d.dependency_kind=%s AND d.dependency_key=%s"
        ")",
        (dependency_kind, dependency_key),
    )
    return changed.rowcount


def demote_realization_revision(conn, realization_revision_id: str) -> int:
    """Withdraw one exact current revision after observed duplicate/fan-out evidence."""
    changed = conn.execute(
        "UPDATE bridge_join_realization_current "
        "SET lifecycle='stale', pointer_version=pointer_version+1, updated_at=now() "
        "WHERE realization_revision_id=%s AND lifecycle='active'",
        (realization_revision_id,),
    )
    return changed.rowcount

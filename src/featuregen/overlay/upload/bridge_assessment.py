"""Versioned contracts for symmetric cross-catalog identifier links.

This module owns the boundary between the platform's flat logical namespace and a physical dataset
binding.  A link says that two logical identifier tuples may denote the same namespace; it does not
say which way a join runs or whether that join is safe.  Direction and execution safety belong to
``bridge_realization``.

Stable identity deliberately excludes review state, clocks, scores and live observations:

* ``candidate_id`` identifies a candidate family plus an unordered pair of logical endpoints;
* ``candidate_revision_id`` seals the conclusions and typed evidence of one assessment;
* availability and optional human review are a current reading of the generic overlay lifecycle.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
    record_binding_revision,
    resolve_dataset_binding,
)
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

if TYPE_CHECKING:
    from featuregen.contracts import DbConn, EventEnvelope
    from featuregen.materialize.codes import MaterializationRefused
    from featuregen.materialize.inventory import ClusterInventoryV1


CANDIDATE_FAMILY_IDENTIFIER_LINK = "identifier_link"
CANDIDATE_CONTRACT_VERSION = "1.0.0"


class BridgeContractError(ValueError):
    """A malformed declaration. This is a caller error, not a governed-state refusal."""


class TypeBasis(StrEnum):
    ATTESTED = "attested"
    DECLARED = "declared"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ConceptAuthority(StrEnum):
    SOURCE = "source"
    HUMAN = "human"
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    UNKNOWN = "unknown"


class KeyMemberRole(StrEnum):
    PRIMARY = "primary"
    PARTITION = "partition"
    DISCRIMINATOR = "discriminator"
    UNKNOWN = "unknown"


class TupleKeyRole(StrEnum):
    COMPLETE_UNIQUE_KEY = "complete_unique_key"
    COMPOSITE_MEMBER = "composite_member"
    FOREIGN_KEY = "foreign_key"
    ALTERNATE_KEY = "alternate_key"
    NON_KEY = "non_key"
    UNKNOWN = "unknown"


class NamespaceVerdict(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"


class PopulationRelation(StrEnum):
    SAME = "same"
    LEFT_SUBSET = "left_subset"
    RIGHT_SUBSET = "right_subset"
    PARTIAL_OVERLAP = "partial_overlap"
    DISJOINT = "disjoint"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    """Typed evidence kinds. The ordering is display-only, never an execution policy."""

    LLM_RECOMMENDATION = "llm_recommendation"
    APPROXIMATE_PROFILE = "approximate_profile"
    STRUCTURAL_METADATA = "structural_metadata"
    HUMAN_ATTESTATION = "human_attestation"
    SOURCE_CONSTRAINT = "source_constraint"
    GOVERNED_FACT = "governed_fact"
    EXACT_PROFILE = "exact_profile"


_EVIDENCE_DISPLAY_RANK = {
    EvidenceKind.LLM_RECOMMENDATION: 10,
    EvidenceKind.APPROXIMATE_PROFILE: 20,
    EvidenceKind.STRUCTURAL_METADATA: 30,
    EvidenceKind.HUMAN_ATTESTATION: 40,
    EvidenceKind.SOURCE_CONSTRAINT: 50,
    EvidenceKind.GOVERNED_FACT: 60,
    EvidenceKind.EXACT_PROFILE: 70,
}


@dataclass(frozen=True, slots=True)
class EvidenceRefV1:
    """A composable reference to evidence, not a flattened ``evidence_level``.

    ``observed_at`` is useful provenance but deliberately excluded from :meth:`identity_payload`.
    Re-reading the same immutable evidence tomorrow must not re-key an assessment or realization.
    """

    evidence_id: str
    kind: EvidenceKind
    producer: str
    content_hash: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise BridgeContractError("evidence_id must not be blank")
        if not self.producer.strip():
            raise BridgeContractError("evidence producer must not be blank")

    def identity_payload(self) -> dict[str, str | None]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "producer": self.producer,
            "content_hash": self.content_hash,
        }


def strongest_evidence_label(evidence_refs: Iterable[EvidenceRefV1]) -> str | None:
    """Derive a UI label without persisting a lossy single evidence level."""
    refs = tuple(evidence_refs)
    if not refs:
        return None
    if all(ref.kind is EvidenceKind.LLM_RECOMMENDATION for ref in refs):
        return "llm_only"
    return max(refs, key=lambda ref: (_EVIDENCE_DISPLAY_RANK[ref.kind], ref.kind.value)).kind.value


def _canonical_logical_ref(logical_ref: str, *, column_required: bool) -> str:
    try:
        source, schema, table, column = parse_ref(logical_ref.strip().lower())
    except ValueError as exc:
        raise BridgeContractError(str(exc)) from exc
    if column_required and column is None:
        raise BridgeContractError(f"column endpoint requires a column ref, got {logical_ref!r}")
    if not column_required and column is not None:
        raise BridgeContractError(f"table endpoint must not carry a column, got {logical_ref!r}")
    # Bridge logical identity is intentionally flat. Physical schemas belong in the binding.
    if schema != "public":
        raise BridgeContractError(
            f"bridge logical refs must use the flat public namespace, got {logical_ref!r}")
    return normalize_ref(source, schema, table, column)


@dataclass(frozen=True, slots=True)
class IdentifierColumnMemberV1:
    logical_column_ref: str
    data_type_family: str
    type_basis: TypeBasis
    key_member_role: KeyMemberRole = KeyMemberRole.UNKNOWN
    physical_identity: PhysicalObjectIdentityV1 | None = None

    def __post_init__(self) -> None:
        logical = _canonical_logical_ref(self.logical_column_ref, column_required=True)
        object.__setattr__(self, "logical_column_ref", logical)
        if not self.data_type_family.strip():
            raise BridgeContractError("data_type_family must not be blank")
        if self.physical_identity is not None:
            if self.physical_identity.object_kind != "column":
                raise BridgeContractError("a member physical_identity must address a column")
            source, _schema, _table, column = parse_ref(logical)
            if self.physical_identity.catalog_source.strip().lower() != source:
                raise BridgeContractError(
                    "logical and physical member catalog_source values must match")
            if (self.physical_identity.column or "").strip().lower() != column:
                raise BridgeContractError(
                    "logical and physical member column names must match")

    @property
    def physical_column_id(self) -> str | None:
        return None if self.physical_identity is None else self.physical_identity.physical_id

    def logical_identity_payload(self) -> dict[str, str]:
        return {"logical_column_ref": self.logical_column_ref}

    def revision_payload(self) -> dict[str, object]:
        return {
            "logical_column_ref": self.logical_column_ref,
            "physical_column_id": self.physical_column_id,
            "data_type_family": self.data_type_family.strip().lower(),
            "type_basis": self.type_basis.value,
            "key_member_role": self.key_member_role.value,
        }


def _binding_payload(
    binding: PhysicalDatasetBindingV1 | None, binding_revision_id: str | None
) -> dict[str, object] | None:
    if binding is None:
        return None
    return {
        "binding_id": binding.binding_id,
        "binding_revision_id": binding_revision_id,
        "binding_content_hash": binding.content_hash,
        "physical_table_id": binding.identity.table_id,
    }


@dataclass(frozen=True, slots=True)
class IdentifierEndpointV1:
    logical_table_ref: str
    members: tuple[IdentifierColumnMemberV1, ...]
    entity_id: str | None = None
    concept: str | None = None
    concept_authority: ConceptAuthority = ConceptAuthority.UNKNOWN
    tuple_key_role: TupleKeyRole = TupleKeyRole.UNKNOWN
    physical_binding: PhysicalDatasetBindingV1 | None = None
    binding_revision_id: str | None = None

    def __post_init__(self) -> None:
        table_ref = _canonical_logical_ref(self.logical_table_ref, column_required=False)
        object.__setattr__(self, "logical_table_ref", table_ref)
        members = tuple(self.members)
        object.__setattr__(self, "members", members)
        if not members:
            raise BridgeContractError("identifier endpoint members must not be empty")
        logical_members = [member.logical_column_ref for member in members]
        if len(set(logical_members)) != len(logical_members):
            raise BridgeContractError("identifier endpoint members must not contain duplicates")
        source, schema, table, _column = parse_ref(table_ref)
        for member in members:
            m_source, m_schema, m_table, _m_column = parse_ref(member.logical_column_ref)
            if (m_source, m_schema, m_table) != (source, schema, table):
                raise BridgeContractError(
                    f"member {member.logical_column_ref!r} is outside endpoint {table_ref!r}")
        if self.binding_revision_id is not None and self.physical_binding is None:
            raise BridgeContractError("binding_revision_id requires a physical_binding")
        if self.physical_binding is not None:
            if self.binding_revision_id != self.physical_binding.binding_revision_id:
                raise BridgeContractError(
                    "binding_revision_id must equal the resolved physical binding revision")
            physical = self.physical_binding.identity
            if physical.object_kind != "table":
                raise BridgeContractError("endpoint physical_binding must address a table")
            if physical.catalog_source.strip().lower() != source:
                raise BridgeContractError(
                    "logical endpoint and physical binding catalog_source values must match")

    @property
    def physical_table_id(self) -> str | None:
        return (
            None if self.physical_binding is None
            else self.physical_binding.identity.table_id
        )

    @property
    def executable(self) -> bool:
        return self.physical_binding is not None and bool(
            self.binding_revision_id and self.binding_revision_id.strip())

    def logical_identity_payload(self) -> dict[str, object]:
        return {
            "logical_table_ref": self.logical_table_ref,
            "members": [member.logical_identity_payload() for member in self.members],
        }

    def revision_payload(self) -> dict[str, object]:
        return {
            **self.logical_identity_payload(),
            "binding": _binding_payload(self.physical_binding, self.binding_revision_id),
            "members": [member.revision_payload() for member in self.members],
            "entity_id": self.entity_id,
            "concept": self.concept,
            "concept_authority": self.concept_authority.value,
            "tuple_key_role": self.tuple_key_role.value,
        }

    def execution_identity_payload(self) -> dict[str, object]:
        """Only the resolved address and ordered logical tuple—not assessment conclusions."""
        if not self.executable:
            raise BridgeContractError(
                f"endpoint {self.logical_table_ref!r} has no resolved binding revision")
        return {
            **self.logical_identity_payload(),
            "binding": _binding_payload(self.physical_binding, self.binding_revision_id),
        }

    def with_tuple_key_role(self, role: TupleKeyRole) -> IdentifierEndpointV1:
        """Return a reclassified endpoint without fabricating per-member key semantics."""
        return replace(self, tuple_key_role=role)


def resolve_and_record_endpoint_binding(
    conn: DbConn,
    inventory: ClusterInventoryV1,
    endpoint: IdentifierEndpointV1,
    *,
    connection_id: str,
    purposes: tuple[str, ...],
    business_time_column: str | None = None,
    recorded_by: str | None = None,
) -> IdentifierEndpointV1 | MaterializationRefused:
    """Turn a flat logical endpoint into an executable, revision-pinned physical endpoint.

    Resolution delegates to the materialization input owner. The inventory must also attest every
    tuple member as a physical data or partition column; a table-level address alone cannot make a
    relationship probe executable.
    """
    from featuregen.materialize.codes import (
        CompilationRefusalCode,
        MaterializationRefused,
    )

    binding = resolve_dataset_binding(
        conn,
        inventory,
        logical_table_ref=endpoint.logical_table_ref,
        # NO bespoke `binding_id`. This path used to name its stream
        # `identifier-endpoint:<env>:<ref>` while the selection path named the SAME table's stream
        # `derived-<catalog>-<table>` — two binding streams for one physical table, so an
        # observation recorded through one was invisible to a reader holding the other (Release C
        # Task 11 scope 0; the argument is at `physical.derived_binding_id`). The resolver's shared
        # default is now the one name.
        connection_id=connection_id,
        business_time_column=business_time_column,
        purposes=purposes,
    )
    if isinstance(binding, MaterializationRefused):
        return binding
    layout = inventory.layout_for(binding.identity.schema, binding.identity.table)
    # resolve_dataset_binding derived a PhysicalInputRequirement, so this is defensive against a
    # contradictory inventory implementation rather than a second resolution path.
    if layout is None:
        return MaterializationRefused(
            CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
            f"no captured layout for {binding.identity.table_id}")
    available_columns = {
        name.strip().lower() for name, _physical_type in layout.columns}
    available_columns.update(
        name.strip().lower() for name, _physical_type in (layout.partition_columns or ()))
    member_columns: list[str] = []
    for member in endpoint.members:
        _source, _schema, _table, column = parse_ref(member.logical_column_ref)
        assert column is not None  # IdentifierColumnMemberV1 enforces this at construction
        if column not in available_columns:
            return MaterializationRefused(
                CompilationRefusalCode.UNACCOUNTED_LOGICAL_REF,
                f"{member.logical_column_ref} is absent from captured physical layout "
                f"{binding.identity.table_id}")
        member_columns.append(column)
    bound_members = tuple(
        replace(member, physical_identity=binding.column(column))
        for member, column in zip(endpoint.members, member_columns, strict=True)
    )
    bound = replace(
        endpoint,
        members=bound_members,
        physical_binding=binding,
        binding_revision_id=binding.binding_revision_id,
    )
    record_binding_revision(conn, binding, recorded_by=recorded_by)
    return bound


def _logical_endpoint_sort_key(endpoint: IdentifierEndpointV1) -> tuple[str, tuple[str, ...]]:
    return (
        endpoint.logical_table_ref,
        tuple(member.logical_column_ref for member in endpoint.members),
    )


def candidate_identity_payload(
    candidate_family: str,
    left_endpoint: IdentifierEndpointV1,
    right_endpoint: IdentifierEndpointV1,
) -> dict[str, object]:
    if not candidate_family.strip():
        raise BridgeContractError("candidate_family must not be blank")
    ordered = sorted((left_endpoint, right_endpoint), key=_logical_endpoint_sort_key)
    endpoints = [endpoint.logical_identity_payload() for endpoint in ordered]
    if endpoints[0] == endpoints[1]:
        raise BridgeContractError("an identifier link requires two distinct logical endpoints")
    return {
        "contract_version": CANDIDATE_CONTRACT_VERSION,
        "candidate_family": candidate_family.strip().lower(),
        "endpoints": endpoints,
    }


def candidate_id_for(
    candidate_family: str,
    left_endpoint: IdentifierEndpointV1,
    right_endpoint: IdentifierEndpointV1,
) -> str:
    """Stable legacy-compatible-width id over only the candidate's logical family and pair."""
    return canonical_hash(
        candidate_identity_payload(candidate_family, left_endpoint, right_endpoint))[:16]


@dataclass(frozen=True, slots=True)
class IdentifierLinkAssessmentV1:
    left_endpoint: IdentifierEndpointV1
    right_endpoint: IdentifierEndpointV1
    namespace_verdict: NamespaceVerdict
    governed_population_relation: PopulationRelation
    assessment_version: str
    candidate_family: str = CANDIDATE_FAMILY_IDENTIFIER_LINK
    bridge_fact_key: str | None = None
    population_hypothesis: str | None = None
    evidence_refs: tuple[EvidenceRefV1, ...] = ()
    hard_conflicts: tuple[str, ...] = ()
    explanation_codes: tuple[str, ...] = ()
    candidate_id: str = field(init=False)
    candidate_revision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.assessment_version.strip():
            raise BridgeContractError("assessment_version must not be blank")
        left, right = sorted(
            (self.left_endpoint, self.right_endpoint), key=_logical_endpoint_sort_key)
        object.__setattr__(self, "left_endpoint", left)
        object.__setattr__(self, "right_endpoint", right)
        evidence = tuple(sorted(
            self.evidence_refs, key=lambda ref: canonical_hash(ref.identity_payload())))
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "hard_conflicts", tuple(sorted(set(self.hard_conflicts))))
        object.__setattr__(
            self, "explanation_codes", tuple(sorted(set(self.explanation_codes))))
        candidate_id = candidate_id_for(self.candidate_family, left, right)
        object.__setattr__(self, "candidate_id", candidate_id)
        revision_payload = {
            "candidate_id": candidate_id,
            "bridge_fact_key": self.bridge_fact_key,
            "left_endpoint": left.revision_payload(),
            "right_endpoint": right.revision_payload(),
            "namespace_verdict": self.namespace_verdict.value,
            "governed_population_relation": self.governed_population_relation.value,
            "population_hypothesis": self.population_hypothesis,
            "evidence_refs": [ref.identity_payload() for ref in evidence],
            "hard_conflicts": list(self.hard_conflicts),
            "explanation_codes": list(self.explanation_codes),
            "assessment_version": self.assessment_version,
        }
        object.__setattr__(
            self, "candidate_revision_id", canonical_hash(revision_payload))

    @property
    def strongest_evidence_label(self) -> str | None:
        return strongest_evidence_label(self.evidence_refs)


class LinkAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class LinkReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    HUMAN_VERIFIED = "human_verified"


class LinkUnavailableReason(StrEnum):
    REJECTED = "rejected"
    REVERIFY = "reverify"
    STALE = "stale"
    SUPERSEDED = "superseded"
    UNREADABLE = "unreadable"


class FoldedLinkStatus(StrEnum):
    DRAFT = "DRAFT"
    PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVERIFY = "REVERIFY"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class OverlayIdentifierLinkStateV1:
    availability: LinkAvailability
    folded_status: FoldedLinkStatus | None
    unavailable_reason: LinkUnavailableReason | None
    review_status: LinkReviewStatus
    overlay_head_event_id: str | None
    governed_value: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class IdentifierLinkAvailabilityV1:
    bridge_fact_key: str
    candidate_revision_id: str
    availability: LinkAvailability
    folded_status: FoldedLinkStatus | None
    unavailable_reason: LinkUnavailableReason | None
    review_status: LinkReviewStatus
    overlay_head_event_id: str | None


_AVAILABLE_FOLDS = frozenset({
    FoldedLinkStatus.DRAFT,
    FoldedLinkStatus.PARTIALLY_CONFIRMED,
    FoldedLinkStatus.VERIFIED,
})
_UNAVAILABLE_REASON = {
    FoldedLinkStatus.REJECTED: LinkUnavailableReason.REJECTED,
    FoldedLinkStatus.REVERIFY: LinkUnavailableReason.REVERIFY,
    FoldedLinkStatus.STALE: LinkUnavailableReason.STALE,
}


def _human_reviewed(stream: Sequence[EventEnvelope], confirmed_event_id: str | None) -> bool:
    if confirmed_event_id is None:
        return False
    confirmed = next((event for event in stream if event.event_id == confirmed_event_id), None)
    if confirmed is None:
        return False
    # Source-declared VERIFIED is operationally available but nobody endorsed it. The actor is
    # checked as well as the payload so a legacy service auto-confirm is never labelled human.
    return (
        "authority_basis" not in confirmed.payload
        and confirmed.actor.actor_kind == "human"
        and bool(confirmed.payload.get("confirmers"))
    )


def link_state_from_stream(
    stream: Sequence[EventEnvelope],
) -> OverlayIdentifierLinkStateV1:
    """Map one PRE-LOADED overlay stream to availability/review — the pure half of
    :func:`read_overlay_identifier_link_state`, split out so A4's batched snapshot reader (one
    ``= ANY`` events read for a whole considered set) reuses the SAME availability authority
    instead of minting a second spelling of the fold. Behavior is byte-identical to the inline
    logic this replaces: an empty or unfoldable stream fails closed as UNREADABLE."""
    from featuregen.overlay.state import fold_overlay_state

    try:
        stream = tuple(stream)
        if not stream:
            raise LookupError("missing overlay fact stream")
        folded = fold_overlay_state(stream)
    except Exception:  # noqa: BLE001 - an unreadable lifecycle must fail closed
        return OverlayIdentifierLinkStateV1(
            LinkAvailability.UNAVAILABLE, None, LinkUnavailableReason.UNREADABLE,
            LinkReviewStatus.UNREVIEWED, None, {})
    try:
        if folded.status is None:
            raise ValueError("overlay link has no folded status")
        status = FoldedLinkStatus(folded.status)
    except (TypeError, ValueError):
        return OverlayIdentifierLinkStateV1(
            LinkAvailability.UNAVAILABLE, None, LinkUnavailableReason.UNREADABLE,
            LinkReviewStatus.UNREVIEWED, stream[-1].event_id, {})
    available = status in _AVAILABLE_FOLDS
    reason = None if available else _UNAVAILABLE_REASON.get(
        status, LinkUnavailableReason.UNREADABLE)
    reviewed = (
        LinkReviewStatus.HUMAN_VERIFIED
        if status is FoldedLinkStatus.VERIFIED
        and _human_reviewed(stream, folded.confirmed_event_id)
        else LinkReviewStatus.UNREVIEWED
    )
    value = folded.value if isinstance(folded.value, Mapping) else {}
    return OverlayIdentifierLinkStateV1(
        LinkAvailability.AVAILABLE if available else LinkAvailability.UNAVAILABLE,
        status, reason, reviewed, stream[-1].event_id, value)


def read_overlay_identifier_link_state(
    conn: DbConn, bridge_fact_key: str | None
) -> OverlayIdentifierLinkStateV1:
    """Fold the authoritative stream once and map availability/review for every consumer."""
    if not bridge_fact_key:
        return OverlayIdentifierLinkStateV1(
            LinkAvailability.UNAVAILABLE, None, LinkUnavailableReason.UNREADABLE,
            LinkReviewStatus.UNREVIEWED, None, {})
    from featuregen.overlay import store

    try:
        stream = tuple(store.load_fact(conn, bridge_fact_key))
    except Exception:  # noqa: BLE001 - an unreadable lifecycle must fail closed
        return OverlayIdentifierLinkStateV1(
            LinkAvailability.UNAVAILABLE, None, LinkUnavailableReason.UNREADABLE,
            LinkReviewStatus.UNREVIEWED, None, {})
    return link_state_from_stream(stream)


def read_identifier_link_availability(
    conn: DbConn,
    *,
    bridge_fact_key: str,
    candidate_revision_id: str,
) -> IdentifierLinkAvailabilityV1:
    state = read_overlay_identifier_link_state(conn, bridge_fact_key)
    return IdentifierLinkAvailabilityV1(
        bridge_fact_key=bridge_fact_key,
        candidate_revision_id=candidate_revision_id,
        availability=state.availability,
        folded_status=state.folded_status,
        unavailable_reason=state.unavailable_reason,
        review_status=state.review_status,
        overlay_head_event_id=state.overlay_head_event_id,
    )


@dataclass(frozen=True, slots=True)
class AvailableIdentifierLinkV1:
    assessment: IdentifierLinkAssessmentV1
    availability: IdentifierLinkAvailabilityV1
    ranking_strength: int = 0

    def __post_init__(self) -> None:
        if self.availability.availability is not LinkAvailability.AVAILABLE:
            raise BridgeContractError("AvailableIdentifierLinkV1 requires an available lifecycle")
        if self.availability.candidate_revision_id != self.assessment.candidate_revision_id:
            raise BridgeContractError("availability and assessment revisions do not match")


def _flat_object_ref(logical_column_ref: str) -> str:
    _source, schema, table, column = parse_ref(logical_column_ref)
    if column is None:
        raise BridgeContractError(
            f"identifier member is not a column: {logical_column_ref!r}")
    return f"{schema}.{table}.{column}"


def _endpoint_from_legacy_link(
    source: str,
    object_ref: str,
    *,
    entity_id: str,
    data_type_family: str,
    type_basis: str,
    is_grain: bool,
) -> IdentifierEndpointV1:
    parts = object_ref.strip().lower().split(".")
    if len(parts) != 3:
        raise BridgeContractError(f"legacy bridge endpoint is not schema.table.column: {object_ref}")
    schema, table, column = parts
    member = IdentifierColumnMemberV1(
        normalize_ref(source, schema, table, column),
        data_type_family or "unknown",
        TypeBasis(type_basis) if type_basis in TypeBasis._value2member_map_ else TypeBasis.UNKNOWN,
        # Legacy flat is_grain is advisory and cannot reconstruct complete tuple keyness.
        KeyMemberRole.UNKNOWN,
    )
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, schema, table),
        members=(member,),
        entity_id=entity_id,
        tuple_key_role=TupleKeyRole.UNKNOWN,
    )


def available_identifier_links(
    conn: DbConn, *, object_ref: str | None = None
) -> tuple[AvailableIdentifierLinkV1, ...]:
    """Read modern current assessments, then legacy-only candidates through one lifecycle fold."""
    from featuregen.overlay.upload.bridge_store import load_current_candidate_assessments
    from featuregen.overlay.upload.cross_catalog_links import cross_catalog_links

    legacy_links = cross_catalog_links(conn, object_ref=object_ref)
    ranking_by_fact_key = {
        link.fact_key: link.strength for link in legacy_links if link.fact_key is not None}
    out: list[AvailableIdentifierLinkV1] = []
    modern_fact_keys: set[str] = set()
    for assessment in load_current_candidate_assessments(conn):
        if assessment.bridge_fact_key is None:
            continue
        if object_ref is not None:
            wanted = object_ref.strip().lower()
            endpoint_refs = {
                _flat_object_ref(member.logical_column_ref)
                for endpoint in (assessment.left_endpoint, assessment.right_endpoint)
                for member in endpoint.members
            }
            if wanted not in endpoint_refs:
                continue
        availability = read_identifier_link_availability(
            conn,
            bridge_fact_key=assessment.bridge_fact_key,
            candidate_revision_id=assessment.candidate_revision_id,
        )
        if availability.availability is LinkAvailability.AVAILABLE:
            out.append(AvailableIdentifierLinkV1(
                assessment,
                availability,
                ranking_by_fact_key.get(assessment.bridge_fact_key, 0),
            ))
            modern_fact_keys.add(assessment.bridge_fact_key)

    for link in legacy_links:
        if link.fact_key is None:
            continue
        if link.fact_key in modern_fact_keys:
            continue
        left = _endpoint_from_legacy_link(
            link.left_catalog_source, link.left_object_ref, entity_id=link.entity_id,
            data_type_family=link.data_type_family, type_basis=link.type_basis,
            is_grain=link.left_is_grain)
        right = _endpoint_from_legacy_link(
            link.right_catalog_source, link.right_object_ref, entity_id=link.entity_id,
            data_type_family=link.data_type_family, type_basis=link.type_basis,
            is_grain=link.right_is_grain)
        assessment = IdentifierLinkAssessmentV1(
            left_endpoint=left,
            right_endpoint=right,
            namespace_verdict=NamespaceVerdict.POSSIBLE,
            governed_population_relation=PopulationRelation.UNKNOWN,
            assessment_version=CANDIDATE_CONTRACT_VERSION,
            bridge_fact_key=link.fact_key,
            explanation_codes=("legacy_candidate_union",),
        )
        availability = read_identifier_link_availability(
            conn, bridge_fact_key=link.fact_key,
            candidate_revision_id=assessment.candidate_revision_id)
        if availability.availability is LinkAvailability.AVAILABLE:
            out.append(AvailableIdentifierLinkV1(assessment, availability, link.strength))
    return tuple(sorted(
        out,
        key=lambda link: (
            -link.ranking_strength,
            link.assessment.candidate_id,
        ),
    ))

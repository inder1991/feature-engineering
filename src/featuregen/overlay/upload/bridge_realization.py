"""Immutable directional join realizations for symmetric identifier links.

An identifier link is unordered.  A realization is not: it says exactly which physical binding is
read on each side, how tuple members pair, which non-equality predicates apply, and what cardinality
has actually been established in that direction.  Human review is deliberately absent from both
stable identities and execution-safety derivation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    EvidenceRefV1,
    IdentifierEndpointV1,
    LinkReviewStatus,
    strongest_evidence_label,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

REALIZATION_CONTRACT_VERSION = "2.0.0"


@dataclass(frozen=True, slots=True)
class DirectionalCardinalityVerdictV1:
    """Required wrapper adding UNKNOWN without changing the settled taxonomy vocabulary."""

    value: Cardinality | None

    @classmethod
    def unknown(cls) -> DirectionalCardinalityVerdictV1:
        return cls(None)

    @property
    def known(self) -> bool:
        return self.value is not None

    def identity_payload(self) -> str:
        return "unknown" if self.value is None else self.value.value

    def inverted(self) -> DirectionalCardinalityVerdictV1:
        inverse = {
            Cardinality.ONE_TO_ONE: Cardinality.ONE_TO_ONE,
            Cardinality.ONE_TO_MANY: Cardinality.MANY_TO_ONE,
            Cardinality.MANY_TO_ONE: Cardinality.ONE_TO_MANY,
            Cardinality.MANY_TO_MANY: Cardinality.MANY_TO_MANY,
        }
        return (
            DirectionalCardinalityVerdictV1.unknown()
            if self.value is None
            else DirectionalCardinalityVerdictV1(inverse[self.value])
        )


class CardinalityBasis(StrEnum):
    GOVERNED_KEY = "governed_key"
    SOURCE_CONSTRAINT = "source_constraint"
    EXACT_PROFILE = "exact_profile"
    APPROXIMATE_PROFILE = "approximate_profile"
    METADATA_INFERENCE = "metadata_inference"
    NONE = "none"


class SafetyStatus(StrEnum):
    UNASSESSED = "unassessed"
    DETERMINISTICALLY_VALIDATED = "deterministically_validated"
    UNSAFE = "unsafe"


class RealizationLifecycle(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ExecutionTier(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


def _column_ref(value: str) -> str:
    try:
        source, schema, table, column = parse_ref(value.strip().lower())
    except ValueError as exc:
        raise BridgeContractError(str(exc)) from exc
    if column is None:
        raise BridgeContractError(f"realization mapping requires a column ref, got {value!r}")
    if schema != "public":
        raise BridgeContractError(
            f"realization logical refs must stay in the flat public namespace, got {value!r}")
    return normalize_ref(source, schema, table, column)


@dataclass(frozen=True, slots=True)
class ColumnPairV1:
    from_logical_column_ref: str
    to_logical_column_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "from_logical_column_ref", _column_ref(self.from_logical_column_ref))
        object.__setattr__(
            self, "to_logical_column_ref", _column_ref(self.to_logical_column_ref))

    def identity_payload(self) -> list[str]:
        return [self.from_logical_column_ref, self.to_logical_column_ref]


@dataclass(frozen=True, slots=True)
class FixedValueReferencePredicateV1:
    """A partition/snapshot column must equal a named, externally resolved value reference."""

    predicate_id: str
    logical_column_ref: str
    value_ref: str
    kind: str = field(default="fixed_value_reference", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_column_ref", _column_ref(self.logical_column_ref))
        if not self.predicate_id.strip() or not self.value_ref.strip():
            raise BridgeContractError(
                "fixed predicate predicate_id and value_ref must not be blank")

    def identity_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "predicate_id": self.predicate_id,
            "logical_column_ref": self.logical_column_ref,
            "value_ref": self.value_ref,
        }


@dataclass(frozen=True, slots=True)
class AsOfIntervalRequirementV1:
    """Half-open ``[effective_from, effective_to)`` validity at a named as-of value."""

    predicate_id: str
    effective_from_ref: str
    effective_to_ref: str
    as_of_value_ref: str
    kind: str = field(default="as_of_interval", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_from_ref", _column_ref(self.effective_from_ref))
        object.__setattr__(self, "effective_to_ref", _column_ref(self.effective_to_ref))
        if self.effective_from_ref == self.effective_to_ref:
            raise BridgeContractError("as-of interval bounds must be different columns")
        if not self.predicate_id.strip() or not self.as_of_value_ref.strip():
            raise BridgeContractError(
                "as-of predicate predicate_id and as_of_value_ref must not be blank")

    def identity_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "predicate_id": self.predicate_id,
            "effective_from_ref": self.effective_from_ref,
            "effective_to_ref": self.effective_to_ref,
            "as_of_value_ref": self.as_of_value_ref,
            "interval": "[from,to)",
        }


@dataclass(frozen=True, slots=True)
class AdditionalKeyRequirementV1:
    """A known missing equality member. It prevents execution until promoted into ``column_pairs``."""

    from_logical_column_ref: str
    to_logical_column_ref: str
    reason_code: str
    kind: str = field(default="unresolved_additional_key", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "from_logical_column_ref", _column_ref(self.from_logical_column_ref))
        object.__setattr__(
            self, "to_logical_column_ref", _column_ref(self.to_logical_column_ref))
        if not self.reason_code.strip():
            raise BridgeContractError("additional-key reason_code must not be blank")

    def identity_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "from_logical_column_ref": self.from_logical_column_ref,
            "to_logical_column_ref": self.to_logical_column_ref,
            "reason_code": self.reason_code,
        }


StructuredPredicateV1: TypeAlias = (
    FixedValueReferencePredicateV1
    | AsOfIntervalRequirementV1
    | AdditionalKeyRequirementV1
)
_STRUCTURED_PREDICATE_TYPES = (
    FixedValueReferencePredicateV1,
    AsOfIntervalRequirementV1,
    AdditionalKeyRequirementV1,
)


@dataclass(frozen=True, slots=True)
class RealizationApplicabilityScopeV1:
    scope_id: str
    execution_tier: ExecutionTier
    purposes: tuple[str, ...]
    environment: str
    partition_scope_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.scope_id.strip():
            raise BridgeContractError("realization scope_id must not be blank")
        if not self.environment.strip():
            raise BridgeContractError("realization environment must not be blank")
        purposes = tuple(sorted(set(purpose.strip() for purpose in self.purposes if purpose.strip())))
        if not purposes:
            raise BridgeContractError("realization scope requires at least one purpose")
        object.__setattr__(self, "purposes", purposes)

    def identity_payload(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "execution_tier": self.execution_tier.value,
            "purposes": list(self.purposes),
            "environment": self.environment,
            "partition_scope_ref": self.partition_scope_ref,
        }


def _endpoint_execution_payload(endpoint: IdentifierEndpointV1) -> dict[str, object]:
    if not endpoint.executable:
        raise BridgeContractError(
            f"realization endpoint {endpoint.logical_table_ref!r} has no resolved binding revision")
    return endpoint.execution_identity_payload()


@dataclass(frozen=True, slots=True)
class BridgeJoinRealizationRevisionV1:
    bridge_fact_key: str
    from_endpoint: IdentifierEndpointV1
    to_endpoint: IdentifierEndpointV1
    column_pairs: tuple[ColumnPairV1, ...]
    predicates: tuple[StructuredPredicateV1, ...]
    applicability_scope: RealizationApplicabilityScopeV1
    cardinality: DirectionalCardinalityVerdictV1
    cardinality_basis: CardinalityBasis
    evidence_refs: tuple[EvidenceRefV1, ...]
    dependency_snapshot_id: str
    derivation_version: str
    admission_policy_version: str
    realization_id: str = field(init=False)
    realization_revision_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "bridge_fact_key", "dependency_snapshot_id", "derivation_version",
            "admission_policy_version",
        ):
            if not getattr(self, name).strip():
                raise BridgeContractError(f"{name} must not be blank")
        if self.from_endpoint.logical_identity_payload() == self.to_endpoint.logical_identity_payload():
            raise BridgeContractError("directional realization endpoints must be distinct")
        from_payload = _endpoint_execution_payload(self.from_endpoint)
        to_payload = _endpoint_execution_payload(self.to_endpoint)

        pairs = tuple(self.column_pairs)
        object.__setattr__(self, "column_pairs", pairs)
        if not pairs:
            raise BridgeContractError("directional realization column_pairs must not be empty")
        if not all(isinstance(pair, ColumnPairV1) for pair in pairs):
            raise BridgeContractError("column_pairs accepts only ColumnPairV1 values")
        from_refs = [pair.from_logical_column_ref for pair in pairs]
        to_refs = [pair.to_logical_column_ref for pair in pairs]
        if len(set(from_refs)) != len(from_refs) or len(set(to_refs)) != len(to_refs):
            raise BridgeContractError("column_pairs must not repeat either tuple member")
        from_members = {
            member.logical_column_ref for member in self.from_endpoint.members}
        to_members = {member.logical_column_ref for member in self.to_endpoint.members}
        if not set(from_refs) <= from_members or not set(to_refs) <= to_members:
            raise BridgeContractError("column_pairs must reference their directional endpoint members")

        predicates = tuple(self.predicates)
        object.__setattr__(self, "predicates", predicates)
        if not all(isinstance(predicate, _STRUCTURED_PREDICATE_TYPES)
                   for predicate in predicates):
            raise BridgeContractError(
                "predicates accepts only structured predicate contracts; free-form SQL is forbidden")
        predicate_payloads = [predicate.identity_payload() for predicate in predicates]
        if len({canonical_hash(payload) for payload in predicate_payloads}) != len(predicates):
            raise BridgeContractError("realization predicates must not contain duplicates")
        equality_pairs = {
            (pair.from_logical_column_ref, pair.to_logical_column_ref) for pair in pairs}
        for predicate in predicates:
            if isinstance(predicate, AdditionalKeyRequirementV1):
                required = (
                    predicate.from_logical_column_ref,
                    predicate.to_logical_column_ref,
                )
                if required in equality_pairs:
                    raise BridgeContractError(
                        "an equality mapping must appear only in column_pairs, not as a predicate")

        evidence = tuple(sorted(
            self.evidence_refs, key=lambda ref: canonical_hash(ref.identity_payload())))
        object.__setattr__(self, "evidence_refs", evidence)

        identity_payload = {
            "contract_version": REALIZATION_CONTRACT_VERSION,
            "bridge_fact_key": self.bridge_fact_key,
            "from_endpoint": from_payload,
            "to_endpoint": to_payload,
            "column_pairs": [pair.identity_payload() for pair in pairs],
            "predicates": predicate_payloads,
            "applicability_scope": self.applicability_scope.identity_payload(),
        }
        realization_id = canonical_hash(identity_payload)
        object.__setattr__(self, "realization_id", realization_id)
        revision_payload = {
            "realization_id": realization_id,
            "cardinality": self.cardinality.identity_payload(),
            "cardinality_basis": self.cardinality_basis.value,
            "evidence_refs": [ref.identity_payload() for ref in evidence],
            "dependency_snapshot_id": self.dependency_snapshot_id,
            "derivation_version": self.derivation_version,
            "admission_policy_version": self.admission_policy_version,
        }
        object.__setattr__(
            self, "realization_revision_id", canonical_hash(revision_payload))

    @property
    def strongest_evidence_label(self) -> str | None:
        return strongest_evidence_label(self.evidence_refs)

    @property
    def has_unresolved_requirements(self) -> bool:
        return any(isinstance(p, AdditionalKeyRequirementV1) for p in self.predicates)


@dataclass(frozen=True, slots=True)
class BridgeRealizationCurrentV1:
    realization_id: str
    realization_revision_id: str
    safety_status: SafetyStatus
    review_status: LinkReviewStatus
    lifecycle: RealizationLifecycle
    pointer_version: int

    def __post_init__(self) -> None:
        if not self.realization_id.strip() or not self.realization_revision_id.strip():
            raise BridgeContractError("realization current pointer requires both identities")
        if self.pointer_version < 1:
            raise BridgeContractError("realization pointer_version must be positive")


def eligible_for_sandbox(
    revision: BridgeJoinRealizationRevisionV1,
    current: BridgeRealizationCurrentV1,
) -> bool:
    """Provisional use: available shape, active lifecycle, no unresolved required key."""
    return (
        current.realization_id == revision.realization_id
        and current.realization_revision_id == revision.realization_revision_id
        and current.lifecycle is RealizationLifecycle.ACTIVE
        and current.safety_status in {
            SafetyStatus.UNASSESSED,
            SafetyStatus.DETERMINISTICALLY_VALIDATED,
        }
        and not revision.has_unresolved_requirements
    )


def eligible_for_production(
    revision: BridgeJoinRealizationRevisionV1,
    current: BridgeRealizationCurrentV1,
) -> bool:
    """Production safety is deterministic; optional human review is intentionally not consulted."""
    return (
        eligible_for_sandbox(revision, current)
        and current.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    )

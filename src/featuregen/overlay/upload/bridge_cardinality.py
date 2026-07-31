"""Complete-key metadata assessment for directional bridge cardinality.

A flat ``is_grain`` flag is never a complete key.  This module compares the exact ordered tuple used
by a directional realization with the table's complete governed grain and keeps three answers
separate:

* complete and unique;
* complete but explicitly non-unique;
* unknown (including a scalar member of a composite key).

The result is metadata evidence only.  Task 7's scoped data probe is still required before production
safety can be asserted for a realization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierEndpointV1,
    TupleKeyRole,
)
from featuregen.overlay.upload.bridge_realization import (
    CardinalityBasis,
    DirectionalCardinalityVerdictV1,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
    GrainAuthorityProvenance,
)
from featuregen.overlay.upload.planner.multisource_endpoints import governed_endpoint
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

BRIDGE_CARDINALITY_METADATA_VERSION = "1.0.0"


class EndpointKeyness(StrEnum):
    COMPLETE_UNIQUE_KEY = "complete_unique_key"
    COMPLETE_NON_UNIQUE_GRAIN = "complete_non_unique_grain"
    COMPOSITE_KEY_MEMBER = "composite_key_member"
    NOT_COMPLETE_KEY = "not_complete_key"
    UNKNOWN = "unknown"


class KeyCompletionRequirementKind(StrEnum):
    ADDITIONAL_KEY_OR_SNAPSHOT_PREDICATE = "additional_key_or_snapshot_predicate"


@dataclass(frozen=True, slots=True)
class CompleteKeyV1:
    catalog: str
    logical_table_ref: str
    ordered_column_refs: tuple[str, ...]
    is_unique: bool
    authority_provenance: GrainAuthorityProvenance
    fact_key: str
    fact_revision: str
    dependency_identity: str

    @property
    def available_without_human_confirmation(self) -> bool:
        return self.authority_provenance in {
            GrainAuthorityProvenance.catalog_authoritative,
            GrainAuthorityProvenance.source_declared,
        }


@dataclass(frozen=True, slots=True)
class KeyCompletionRequirementV1:
    endpoint_side: str
    endpoint_table_ref: str
    missing_key_refs: tuple[str, ...]
    kind: KeyCompletionRequirementKind = (
        KeyCompletionRequirementKind.ADDITIONAL_KEY_OR_SNAPSHOT_PREDICATE
    )

    def __post_init__(self) -> None:
        if self.endpoint_side not in {"from", "to"}:
            raise ValueError("endpoint_side must be 'from' or 'to'")
        if not self.missing_key_refs:
            raise ValueError("a key-completion requirement needs at least one missing key")


@dataclass(frozen=True, slots=True)
class EndpointKeyAssessmentV1:
    endpoint: IdentifierEndpointV1
    keyness: EndpointKeyness
    complete_key: CompleteKeyV1 | None
    missing_key_refs: tuple[str, ...] = ()

    @property
    def unique_verdict(self) -> bool | None:
        if self.keyness is EndpointKeyness.COMPLETE_UNIQUE_KEY:
            return True
        if self.keyness in {
            EndpointKeyness.COMPLETE_NON_UNIQUE_GRAIN,
            EndpointKeyness.NOT_COMPLETE_KEY,
        }:
            return False
        return None


@dataclass(frozen=True, slots=True)
class MetadataCardinalityAssessmentV1:
    cardinality: DirectionalCardinalityVerdictV1
    cardinality_basis: CardinalityBasis
    from_key: EndpointKeyAssessmentV1
    to_key: EndpointKeyAssessmentV1
    requirements: tuple[KeyCompletionRequirementV1, ...]
    risk_codes: tuple[str, ...] = ()
    assessment_version: str = BRIDGE_CARDINALITY_METADATA_VERSION


def _logical_key_refs(endpoint: GovernedEndpointV1) -> tuple[str, ...]:
    refs: list[str] = []
    for qualified in endpoint.grain_key_refs:
        parts = qualified.strip().lower().split(".")
        if len(parts) != 3:
            raise ValueError(f"invalid governed grain column ref {qualified!r}")
        schema, table, column = parts
        refs.append(normalize_ref(endpoint.catalog, schema, table, column))
    return tuple(refs)


def _complete_key(endpoint: GovernedEndpointV1) -> CompleteKeyV1:
    return CompleteKeyV1(
        catalog=endpoint.catalog,
        logical_table_ref=normalize_ref(
            endpoint.catalog,
            endpoint.table_ref.rpartition(".")[0],
            endpoint.table_ref.rpartition(".")[2],
        ),
        ordered_column_refs=_logical_key_refs(endpoint),
        is_unique=endpoint.grain_is_unique,
        authority_provenance=endpoint.grain_authority_provenance,
        fact_key=endpoint.grain_fact_key,
        fact_revision=endpoint.grain_fact_revision,
        dependency_identity=endpoint.grain_dependency_identity,
    )


def resolve_complete_key(
    conn,
    adapter: CatalogAdapter,
    *,
    catalog: str,
    table_ref: str,
    now: datetime,
) -> CompleteKeyV1 | None:
    """Resolve a complete key/grain from catalog or current governed fact authority.

    DRAFT/advisory flat flags never reach this function's result because ``governed_endpoint`` is
    resolve-fact gated.
    """
    endpoint = governed_endpoint(
        conn, adapter, catalog=catalog, table_ref=table_ref, now=now)
    return None if endpoint is None else _complete_key(endpoint)


def assess_endpoint_keyness(
    endpoint: IdentifierEndpointV1,
    complete_key: CompleteKeyV1 | None,
) -> EndpointKeyAssessmentV1:
    """Compare exact endpoint membership with the complete governed tuple."""
    if complete_key is None:
        return EndpointKeyAssessmentV1(
            endpoint.with_tuple_key_role(TupleKeyRole.UNKNOWN),
            EndpointKeyness.UNKNOWN,
            None,
        )
    member_refs = tuple(member.logical_column_ref for member in endpoint.members)
    complete_refs = complete_key.ordered_column_refs
    if member_refs == complete_refs:
        if complete_key.is_unique:
            return EndpointKeyAssessmentV1(
                endpoint.with_tuple_key_role(TupleKeyRole.COMPLETE_UNIQUE_KEY),
                EndpointKeyness.COMPLETE_UNIQUE_KEY,
                complete_key,
            )
        return EndpointKeyAssessmentV1(
            endpoint.with_tuple_key_role(TupleKeyRole.NON_KEY),
            EndpointKeyness.COMPLETE_NON_UNIQUE_GRAIN,
            complete_key,
        )
    if set(member_refs) < set(complete_refs):
        missing = tuple(ref for ref in complete_refs if ref not in member_refs)
        return EndpointKeyAssessmentV1(
            endpoint.with_tuple_key_role(TupleKeyRole.COMPOSITE_MEMBER),
            EndpointKeyness.COMPOSITE_KEY_MEMBER,
            complete_key,
            missing,
        )
    return EndpointKeyAssessmentV1(
        endpoint.with_tuple_key_role(TupleKeyRole.UNKNOWN),
        EndpointKeyness.NOT_COMPLETE_KEY,
        complete_key,
    )


def infer_metadata_cardinality(
    from_endpoint: IdentifierEndpointV1,
    to_endpoint: IdentifierEndpointV1,
    *,
    from_complete_key: CompleteKeyV1 | None,
    to_complete_key: CompleteKeyV1 | None,
) -> MetadataCardinalityAssessmentV1:
    """Apply the directional unique/non-unique truth table, failing closed on partial keys."""
    from_key = assess_endpoint_keyness(from_endpoint, from_complete_key)
    to_key = assess_endpoint_keyness(to_endpoint, to_complete_key)
    requirements: list[KeyCompletionRequirementV1] = []
    for side, assessed in (("from", from_key), ("to", to_key)):
        if assessed.keyness is EndpointKeyness.COMPOSITE_KEY_MEMBER:
            requirements.append(KeyCompletionRequirementV1(
                endpoint_side=side,
                endpoint_table_ref=assessed.endpoint.logical_table_ref,
                missing_key_refs=assessed.missing_key_refs,
            ))

    from_unique = from_key.unique_verdict
    to_unique = to_key.unique_verdict
    if requirements or from_unique is None or to_unique is None:
        return MetadataCardinalityAssessmentV1(
            DirectionalCardinalityVerdictV1.unknown(),
            CardinalityBasis.NONE,
            from_key,
            to_key,
            tuple(requirements),
        )

    truth_table = {
        (False, True): Cardinality.MANY_TO_ONE,
        (True, False): Cardinality.ONE_TO_MANY,
        (True, True): Cardinality.ONE_TO_ONE,
        (False, False): Cardinality.MANY_TO_MANY,
    }
    cardinality = truth_table[(from_unique, to_unique)]
    risk_codes = (
        ("many_to_many_fanout_risk",)
        if cardinality is Cardinality.MANY_TO_MANY
        else ()
    )
    return MetadataCardinalityAssessmentV1(
        DirectionalCardinalityVerdictV1(cardinality),
        CardinalityBasis.GOVERNED_KEY,
        from_key,
        to_key,
        (),
        risk_codes,
    )

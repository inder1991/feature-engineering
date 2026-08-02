"""Deterministic grounding for cross-catalog identifier-link candidates.

Concept, entity, identifier namespace, population, representation and descriptive metadata are
different claims.  This module keeps them different.  In particular, two columns classified for
the same business entity are only a *possible* namespace match unless governed namespace evidence
says more.

The planner's concept-authority resolver remains fail-closed and unchanged.  Bridge discovery uses
that resolver for source/human authority, then separately preserves a selected LLM recommendation
as an honestly labelled advisory input.  It never upgrades an LLM recommendation into a planner
binding.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import FieldEvidence, read_active_field_evidence

# The representation ruleset (roles, type families, exact-token helpers) is EXTRACTED to
# attest/representation.py so bridge grounding and the enrichment concept critic share ONE ruleset
# (ingestion-richness Task 2). Re-imported here — behavior and this module's public API are
# unchanged; private aliases keep every original call site byte-identical.
from featuregen.overlay.upload.attest.representation import (  # noqa: F401 — re-exported API
    RepresentationRole,
    _dedupe_evidence,
    _evidence_kind,
    _evidence_ref,
    _tokens,
    resolve_type_family,
    type_family,
)
from featuregen.overlay.upload.attest.representation import (
    observed_format as _observed_format,
)
from featuregen.overlay.upload.attest.representation import (
    representation_role as _representation_role,
)
from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    KeyMemberRole,
    NamespaceVerdict,
    PopulationRelation,
    TupleKeyRole,
    TypeBasis,
)
from featuregen.overlay.upload.concepts import concept as lookup_concept
from featuregen.overlay.upload.governed_grain import GovernedGrain, read_governed_grain
from featuregen.overlay.upload.identifier_scope import resolve_identifier_issuer
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

BRIDGE_GROUNDING_VERSION = "1.0.0"

#: Explanation code for a SAME-NAMESPACE pair whose endpoints are classified for different
#: entities (the cust_num(customer) x counter_party_cif_id(counterparty) case). Namespace is the
#: only axis that gates join candidacy; entity corroborates and displays, so within one namespace
#: this is a display note, never a suppression.
ENTITY_DISAGREEMENT = "entity_disagreement"

_METADATA_FIELDS = (
    "definition",
    "names",
    "terms",
    "synonyms",
    "domain",
    "taxonomy",
)
_AUTHORITATIVE_PAIRS = (
    (EvidenceProducer.HUMAN.value, AssertionStrength.CONFIRMED.value),
    (EvidenceProducer.SOURCE.value, AssertionStrength.ATTESTED.value),
)


class EvidencePresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"


class MetadataAgreement(StrEnum):
    CORROBORATES = "corroborates"
    DIVERGES = "diverges"
    ONE_SIDED = "one_sided"
    ABSENT = "absent"


class RepresentationCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BridgeConceptGroundingV1:
    concept: str | None
    authority: ConceptAuthority
    provenance_label: str
    evidence_refs: tuple[EvidenceRefV1, ...]
    authoritative: bool


@dataclass(frozen=True, slots=True)
class MetadataFacetV1:
    field_name: str
    presence: EvidencePresence
    canonical_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataComparisonV1:
    field_name: str
    agreement: MetadataAgreement


@dataclass(frozen=True, slots=True)
class BridgeEndpointGroundingV1:
    logical_column_ref: str
    logical_table_ref: str
    exists: bool
    column_name: str
    concept: BridgeConceptGroundingV1
    governed_entity_id: str | None
    advisory_entity_id: str | None
    explicit_namespace: str | None
    governed_population: str | None
    representation_role: RepresentationRole
    data_type_family: str
    type_basis: TypeBasis
    is_grain: bool
    key_member_role: KeyMemberRole
    tuple_key_role: TupleKeyRole
    observed_format: str | None
    metadata_facets: tuple[MetadataFacetV1, ...]
    evidence_refs: tuple[EvidenceRefV1, ...]
    #: The identifier ISSUER axis (semantic Task 2): the endpoint's issuing scope resolved
    #: through `identifier_scope.resolve_identifier_issuer` (global scheme registry, else the
    #: catalog's declared semantic scope). `(None, "unresolved")` is the honest default — a
    #: missing issuer never looks configured. Defaulted so pre-existing positional constructor
    #: sites stay byte-identical.
    issuer_scope: str | None = None
    issuer_basis: str = "unresolved"

    @property
    def entity_id(self) -> str | None:
        return self.governed_entity_id or self.advisory_entity_id

    def facet(self, field_name: str) -> MetadataFacetV1:
        return next(
            (
                facet
                for facet in self.metadata_facets
                if facet.field_name == field_name
            ),
            MetadataFacetV1(field_name, EvidencePresence.ABSENT),
        )

    def to_identifier_endpoint(self) -> IdentifierEndpointV1:
        if not self.exists:
            raise ValueError(f"cannot build an endpoint for missing column {self.logical_column_ref}")
        return IdentifierEndpointV1(
            logical_table_ref=self.logical_table_ref,
            members=(
                IdentifierColumnMemberV1(
                    logical_column_ref=self.logical_column_ref,
                    data_type_family=self.data_type_family,
                    type_basis=self.type_basis,
                    key_member_role=self.key_member_role,
                ),
            ),
            entity_id=self.entity_id,
            concept=self.concept.concept,
            concept_authority=self.concept.authority,
            tuple_key_role=self.tuple_key_role,
        )


@dataclass(frozen=True, slots=True)
class IdentifierLinkGroundingV1:
    left: BridgeEndpointGroundingV1
    right: BridgeEndpointGroundingV1
    namespace_verdict: NamespaceVerdict
    governed_population_relation: PopulationRelation
    population_hypothesis: str | None
    representation_compatibility: RepresentationCompatibility
    metadata_comparisons: tuple[MetadataComparisonV1, ...]
    evidence_refs: tuple[EvidenceRefV1, ...]
    hard_conflicts: tuple[str, ...]
    explanation_codes: tuple[str, ...]

    def to_assessment(self, *, bridge_fact_key: str | None = None) -> IdentifierLinkAssessmentV1:
        return IdentifierLinkAssessmentV1(
            left_endpoint=self.left.to_identifier_endpoint(),
            right_endpoint=self.right.to_identifier_endpoint(),
            namespace_verdict=self.namespace_verdict,
            governed_population_relation=self.governed_population_relation,
            assessment_version=BRIDGE_GROUNDING_VERSION,
            bridge_fact_key=bridge_fact_key,
            population_hypothesis=self.population_hypothesis,
            evidence_refs=self.evidence_refs,
            hard_conflicts=self.hard_conflicts,
            explanation_codes=self.explanation_codes,
        )


def _selected_evidence_ids(conn: DbConn, decision_id: str | None) -> frozenset[str]:
    if not decision_id:
        return frozenset()
    row = conn.execute(
        "SELECT selected_evidence_ids FROM field_decision_event "
        "WHERE decision_event_id = %s "
        "AND event_type NOT IN ('rejected', 'staled', 'superseded')",
        (decision_id,),
    ).fetchone()
    if row is None:
        return frozenset()
    selected = row[0]
    if not isinstance(selected, list):
        return frozenset()
    return frozenset(str(item) for item in selected)


def _string_value(evidence: FieldEvidence) -> str:
    if isinstance(evidence.proposed_value, str):
        return evidence.proposed_value.strip()
    return json.dumps(evidence.proposed_value, sort_keys=True, separators=(",", ":"))


def _concept_grounding(
    conn: DbConn,
    *,
    logical_ref: str,
    display_concept: str | None,
    concept_decision_id: str | None,
) -> BridgeConceptGroundingV1:
    # Lazy by design: ``planner.plan`` imports bridge discovery only for its derivation-version
    # constant. Importing B's authority module at module load also imports the multi-source contract
    # vocabulary and violates the existing single-source import-neutrality proof. The resolver is
    # reused when grounding actually runs, without widening the ordinary planner's import graph.
    from featuregen.overlay.upload.planner.b_concept_authority import (
        ConceptAuthority as PlannerConceptAuthority,
    )
    from featuregen.overlay.upload.planner.b_concept_authority import (
        PlannerConceptBinding,
        resolve_planner_concept_binding,
    )

    authoritative = resolve_planner_concept_binding(conn, logical_ref)
    active = read_active_field_evidence(conn, logical_ref, "concept")
    if isinstance(authoritative, PlannerConceptBinding):
        refs = [
            _evidence_ref(item)
            for item in active
            if item.evidence_id in authoritative.evidence_ids
        ]
        authority = (
            ConceptAuthority.HUMAN
            if authoritative.authority is PlannerConceptAuthority.human_confirmed
            else ConceptAuthority.SOURCE
        )
        return BridgeConceptGroundingV1(
            concept=authoritative.authoritative_concept,
            authority=authority,
            provenance_label=authoritative.authority.value,
            evidence_refs=_dedupe_evidence(refs),
            authoritative=True,
        )

    concept_name = (display_concept or "").strip().lower() or None
    selected_ids = _selected_evidence_ids(conn, concept_decision_id)
    matching = [
        item
        for item in active
        if (not selected_ids or item.evidence_id in selected_ids)
        and concept_name is not None
        and _string_value(item).strip().lower() == concept_name
    ]
    producers = {item.producer for item in matching}
    if EvidenceProducer.LLM.value in producers:
        authority = ConceptAuthority.LLM
        label = "llm_only"
    elif producers & {
        EvidenceProducer.PARSER.value,
        EvidenceProducer.TAXONOMY.value,
    }:
        authority = ConceptAuthority.DETERMINISTIC
        label = "deterministic"
    else:
        # Source/human rows rejected by the authoritative resolver stay non-authoritative.  Their
        # mere presence must never be relabelled as accepted authority.
        authority = ConceptAuthority.UNKNOWN
        label = "unknown"
    return BridgeConceptGroundingV1(
        concept=concept_name,
        authority=authority,
        provenance_label=label,
        evidence_refs=_dedupe_evidence([_evidence_ref(item) for item in matching]),
        authoritative=False,
    )


def _governed_scalar(
    conn: DbConn, logical_ref: str, field_name: str
) -> tuple[str | None, tuple[EvidenceRefV1, ...]]:
    active = read_active_field_evidence(conn, logical_ref, field_name)
    for producer, strength in _AUTHORITATIVE_PAIRS:
        tier = [
            item
            for item in active
            if item.producer == producer and item.strength == strength
        ]
        if not tier:
            continue
        values = {_string_value(item).strip().lower() for item in tier if _string_value(item)}
        if len(values) != 1:
            return None, _dedupe_evidence([_evidence_ref(item) for item in tier])
        return next(iter(values)), _dedupe_evidence([_evidence_ref(item) for item in tier])
    return None, ()


def _metadata_facets(
    conn: DbConn,
    *,
    logical_ref: str,
    column_name: str,
    definition: str | None,
    domain: str | None,
    semantic_terms: str | None,
) -> tuple[tuple[MetadataFacetV1, ...], tuple[EvidenceRefV1, ...]]:
    evidence_by_field = {
        field_name: read_active_field_evidence(conn, logical_ref, field_name)
        for field_name in (
            "definition",
            "business_term",
            "semantic_terms",
            "domain",
            "bian_path",
            "fibo_path",
        )
    }

    def evidence_values(field_name: str) -> tuple[object, ...]:
        return tuple(
            item.proposed_value
            for item in evidence_by_field[field_name]
            if item.proposed_value not in (None, "")
        )

    values = {
        "definition": (definition, *evidence_values("definition")),
        "names": (column_name,),
        "terms": evidence_values("business_term"),
        "synonyms": (
            semantic_terms,
            *evidence_values("semantic_terms"),
        ),
        "domain": (domain, *evidence_values("domain")),
        "taxonomy": (
            *evidence_values("bian_path"),
            *evidence_values("fibo_path"),
        ),
    }
    facets: list[MetadataFacetV1] = []
    for field_name in _METADATA_FIELDS:
        tokens = tuple(sorted(_tokens(*values[field_name])))
        facets.append(
            MetadataFacetV1(
                field_name=field_name,
                presence=(
                    EvidencePresence.PRESENT if tokens else EvidencePresence.ABSENT
                ),
                canonical_tokens=tokens,
            )
        )
    refs = _dedupe_evidence([
        _evidence_ref(item)
        for rows in evidence_by_field.values()
        for item in rows
    ])
    return tuple(facets), refs


def ground_bridge_endpoint(
    conn: DbConn, logical_column_ref: str
) -> BridgeEndpointGroundingV1:
    """Read and ground one current flat logical column endpoint."""
    source, schema, table, column = parse_ref(logical_column_ref)
    canonical_ref = normalize_ref(source, schema, table, column)
    table_ref = normalize_ref(source, schema, table)
    if schema != "public" or column is None:
        return BridgeEndpointGroundingV1(
            canonical_ref,
            table_ref,
            False,
            column or "",
            BridgeConceptGroundingV1(
                None, ConceptAuthority.UNKNOWN, "unknown", (), False
            ),
            None,
            None,
            None,
            None,
            RepresentationRole.UNKNOWN,
            "other",
            TypeBasis.UNKNOWN,
            False,
            KeyMemberRole.UNKNOWN,
            TupleKeyRole.UNKNOWN,
            None,
            tuple(
                MetadataFacetV1(name, EvidencePresence.ABSENT)
                for name in _METADATA_FIELDS
            ),
            (),
        )
    row = conn.execute(
        "SELECT column_name, data_type, declared_type, concept, is_grain, definition, domain, "
        "       semantic_terms, entity, concept_decision_id "
        "FROM graph_node "
        "WHERE lower(catalog_source) = %s AND lower(object_ref) = %s AND kind = 'column'",
        (source, f"public.{table}.{column}"),
    ).fetchone()
    if row is None:
        return BridgeEndpointGroundingV1(
            canonical_ref,
            table_ref,
            False,
            column,
            BridgeConceptGroundingV1(
                None, ConceptAuthority.UNKNOWN, "unknown", (), False
            ),
            None,
            None,
            None,
            None,
            RepresentationRole.UNKNOWN,
            "other",
            TypeBasis.UNKNOWN,
            False,
            KeyMemberRole.UNKNOWN,
            TupleKeyRole.UNKNOWN,
            None,
            tuple(
                MetadataFacetV1(name, EvidencePresence.ABSENT)
                for name in _METADATA_FIELDS
            ),
            (),
        )

    (
        graph_column,
        data_type,
        declared_type,
        display_concept,
        is_grain,
        definition,
        domain,
        semantic_terms,
        display_entity,
        concept_decision_id,
    ) = row
    concept_grounding = _concept_grounding(
        conn,
        logical_ref=canonical_ref,
        display_concept=display_concept,
        concept_decision_id=concept_decision_id,
    )
    registered = (
        lookup_concept(concept_grounding.concept)
        if concept_grounding.concept
        else None
    )
    explicit_entity, entity_evidence = _governed_scalar(conn, canonical_ref, "entity")
    # The RAW registry entity_link, deliberately NOT the display seam (D12.1-revised): this value
    # flows into advisory_entity_id -> _entity_pick -> fact_key, so routing it through
    # `display_entity` would re-key governed bridge facts (resurrecting REJECTED decoys under
    # fresh keys and duplicating VERIFIED links). The `customer` correction is read-time only.
    concept_entity = registered.entity_link if registered is not None else None
    governed_entity = explicit_entity
    if (
        governed_entity is None
        and concept_grounding.authority in {ConceptAuthority.SOURCE, ConceptAuthority.HUMAN}
    ):
        governed_entity = concept_entity
    advisory_entity = (
        governed_entity
        or (str(display_entity).strip().lower() if display_entity else None)
        or concept_entity
    )
    namespace, namespace_evidence = _governed_scalar(
        conn, canonical_ref, "identifier_namespace"
    )
    population, population_evidence = _governed_scalar(
        conn, canonical_ref, "identifier_population"
    )
    family, basis = resolve_type_family(data_type, declared_type)
    observed_format, format_evidence = _observed_format(conn, canonical_ref)
    facets, metadata_evidence = _metadata_facets(
        conn,
        logical_ref=canonical_ref,
        column_name=graph_column,
        definition=definition,
        domain=domain,
        semantic_terms=semantic_terms,
    )
    governed_grain = read_governed_grain(
        conn, source, table, now=datetime.now(UTC), schema=schema)
    if isinstance(governed_grain, GovernedGrain) and column in governed_grain.columns:
        tuple_key_role = (
            TupleKeyRole.COMPLETE_UNIQUE_KEY
            if len(governed_grain.columns) == 1
            else TupleKeyRole.COMPOSITE_MEMBER
        )
        key_member_role = (
            KeyMemberRole.PRIMARY
            if len(governed_grain.columns) == 1
            else KeyMemberRole.UNKNOWN
        )
        grain_evidence = (
            ()
            if governed_grain.fact_event_id is None
            else (
                EvidenceRefV1(
                    evidence_id=governed_grain.fact_event_id,
                    kind=EvidenceKind.GOVERNED_FACT,
                    producer="overlay_grain",
                ),
            )
        )
    else:
        tuple_key_role = TupleKeyRole.UNKNOWN
        key_member_role = KeyMemberRole.UNKNOWN
        grain_evidence = ()
    issuer_scope, issuer_basis = resolve_identifier_issuer(
        conn, source, concept_grounding.concept)
    refs = _dedupe_evidence(
        [
            *concept_grounding.evidence_refs,
            *entity_evidence,
            *namespace_evidence,
            *population_evidence,
            *format_evidence,
            *metadata_evidence,
            *grain_evidence,
        ]
    )
    return BridgeEndpointGroundingV1(
        logical_column_ref=canonical_ref,
        logical_table_ref=table_ref,
        exists=True,
        column_name=graph_column,
        concept=concept_grounding,
        governed_entity_id=governed_entity,
        advisory_entity_id=advisory_entity,
        explicit_namespace=namespace,
        governed_population=population,
        representation_role=_representation_role(
            column_name=graph_column,
            definition=definition,
            concept_name=concept_grounding.concept,
            observed_format=observed_format,
            data_type_family=family,
        ),
        data_type_family=family,
        type_basis=basis,
        is_grain=bool(is_grain),
        # ``graph_node.is_grain`` is a flat upload projection. It may identify a possible member,
        # but cannot prove completeness or uniqueness of the table's ordered key. Task 5 performs
        # that classification from the complete governed grain fact.
        key_member_role=key_member_role,
        tuple_key_role=tuple_key_role,
        observed_format=observed_format,
        metadata_facets=facets,
        evidence_refs=refs,
        issuer_scope=issuer_scope,
        issuer_basis=issuer_basis,
    )


def _compare_metadata(
    left: BridgeEndpointGroundingV1, right: BridgeEndpointGroundingV1
) -> tuple[MetadataComparisonV1, ...]:
    comparisons: list[MetadataComparisonV1] = []
    for field_name in _METADATA_FIELDS:
        left_facet = left.facet(field_name)
        right_facet = right.facet(field_name)
        if (
            left_facet.presence is EvidencePresence.ABSENT
            and right_facet.presence is EvidencePresence.ABSENT
        ):
            agreement = MetadataAgreement.ABSENT
        elif (
            left_facet.presence is EvidencePresence.ABSENT
            or right_facet.presence is EvidencePresence.ABSENT
        ):
            agreement = MetadataAgreement.ONE_SIDED
        elif set(left_facet.canonical_tokens) & set(right_facet.canonical_tokens):
            agreement = MetadataAgreement.CORROBORATES
        else:
            agreement = MetadataAgreement.DIVERGES
        comparisons.append(MetadataComparisonV1(field_name, agreement))
    return tuple(comparisons)


def _population_hypothesis(
    left: BridgeEndpointGroundingV1, right: BridgeEndpointGroundingV1
) -> str | None:
    left_tokens = _tokens(
        left.logical_column_ref,
        *(facet.canonical_tokens for facet in left.metadata_facets),
    )
    right_tokens = _tokens(
        right.logical_column_ref,
        *(facet.canonical_tokens for facet in right.metadata_facets),
    )
    left_cardholder = "cardholder" in left_tokens
    right_cardholder = "cardholder" in right_tokens
    if left_cardholder and not right_cardholder:
        return PopulationRelation.LEFT_SUBSET.value
    if right_cardholder and not left_cardholder:
        return PopulationRelation.RIGHT_SUBSET.value
    return None


def assess_grounded_identifier_link(
    left: BridgeEndpointGroundingV1,
    right: BridgeEndpointGroundingV1,
) -> IdentifierLinkGroundingV1:
    """Assess two already-grounded endpoints without repeating their catalog/evidence reads."""
    left, right = sorted((left, right), key=lambda endpoint: endpoint.logical_column_ref)

    hard_conflicts: list[str] = []
    explanations: list[str] = []
    if not left.exists or not right.exists:
        hard_conflicts.append("endpoint_not_found")

    if (
        left.governed_entity_id
        and right.governed_entity_id
        and left.governed_entity_id != right.governed_entity_id
    ):
        # The three-axis model: within ONE identifier namespace, an entity mismatch is a display
        # note (a counterparty may be our customer — same cif registry). Across namespaces, or
        # where no namespace is declared, differing governed entities stay a hard conflict.
        left_registered = lookup_concept(left.concept.concept) if left.concept.concept else None
        right_registered = lookup_concept(right.concept.concept) if right.concept.concept else None
        if (
            left_registered is not None
            and right_registered is not None
            and left_registered.namespace is not None
            and left_registered.namespace == right_registered.namespace
        ):
            explanations.append(ENTITY_DISAGREEMENT)
        else:
            hard_conflicts.append("different_governed_entity")

    if left.explicit_namespace and right.explicit_namespace:
        if left.explicit_namespace == right.explicit_namespace:
            namespace = NamespaceVerdict.SAME
            explanations.append("same_governed_identifier_namespace")
        else:
            namespace = NamespaceVerdict.DIFFERENT
            hard_conflicts.append("different_governed_identifier_namespace")
    elif left.entity_id and left.entity_id == right.entity_id:
        namespace = NamespaceVerdict.POSSIBLE
        explanations.append("same_entity_namespace_unproven")
    else:
        namespace = NamespaceVerdict.UNKNOWN

    # ── the ISSUER fold (semantic Task 2): a scheme names a value space only WITHIN an issuer.
    # Applied to SAME-SCHEME pairs (governed namespace facts equal, or both registry concepts
    # declaring one scheme). The truth table (plan Task 2): same known issuer -> normal candidate;
    # different known issuers -> refuse (two banks' registries are disjoint worlds — equal values
    # mean nothing); either unresolved -> ADVISORY candidate, flagged, never an equality proof
    # (rule 5) — and never a hard conflict, so AI-proposed stays usable. This lives HERE so no
    # grounded path can bypass it; `identifier_scope` only produces the axis. Party role is not
    # consulted anywhere in this function — roles explain links, they never gate pairing.
    left_scheme_concept = lookup_concept(left.concept.concept) if left.concept.concept else None
    right_scheme_concept = (
        lookup_concept(right.concept.concept) if right.concept.concept else None)
    same_scheme = bool(
        (left.explicit_namespace and left.explicit_namespace == right.explicit_namespace)
        or (
            left_scheme_concept is not None
            and right_scheme_concept is not None
            and left_scheme_concept.namespace is not None
            and left_scheme_concept.namespace == right_scheme_concept.namespace
        )
    )
    if same_scheme:
        if left.issuer_scope and right.issuer_scope:
            if left.issuer_scope == right.issuer_scope:
                explanations.append("same_identifier_issuer_scope")
            else:
                namespace = NamespaceVerdict.DIFFERENT
                hard_conflicts.append("different_identifier_issuer_scope")
        else:
            explanations.append("issuer_unresolved")
            if namespace is NamespaceVerdict.SAME:
                # A governed same-scheme FACT pair without issuer context is still unproven
                # equality: demote to POSSIBLE — a missing issuer never appears as verified.
                namespace = NamespaceVerdict.POSSIBLE

    non_identifier_roles = {
        RepresentationRole.HUMAN_LABEL,
        RepresentationRole.DESCRIPTION_TEXT,
        RepresentationRole.FREE_TEXT,
    }
    if (
        left.representation_role in non_identifier_roles
        or right.representation_role in non_identifier_roles
    ):
        representation = RepresentationCompatibility.INCOMPATIBLE
        hard_conflicts.append("incompatible_representation_role")
    elif (
        left.representation_role is RepresentationRole.IDENTIFIER_VALUE
        and right.representation_role is RepresentationRole.IDENTIFIER_VALUE
    ):
        representation = RepresentationCompatibility.COMPATIBLE
    else:
        representation = RepresentationCompatibility.UNKNOWN

    if (
        left.data_type_family != right.data_type_family
        and "other" not in {left.data_type_family, right.data_type_family}
    ):
        hard_conflicts.append("incompatible_type_family")
        representation = RepresentationCompatibility.INCOMPATIBLE
    if "other" in {left.data_type_family, right.data_type_family}:
        hard_conflicts.append("representation_untestable")

    if left.governed_population and right.governed_population:
        population_relation = (
            PopulationRelation.SAME
            if left.governed_population == right.governed_population
            else PopulationRelation.UNKNOWN
        )
    else:
        population_relation = PopulationRelation.UNKNOWN
    hypothesis = _population_hypothesis(left, right)
    if hypothesis:
        explanations.append("advisory_population_hypothesis")

    labels = {left.concept.provenance_label, right.concept.provenance_label}
    if "llm_only" in labels:
        explanations.append("llm_only")

    return IdentifierLinkGroundingV1(
        left=left,
        right=right,
        namespace_verdict=namespace,
        governed_population_relation=population_relation,
        population_hypothesis=hypothesis,
        representation_compatibility=representation,
        metadata_comparisons=_compare_metadata(left, right),
        evidence_refs=_dedupe_evidence(
            [*left.evidence_refs, *right.evidence_refs]
        ),
        hard_conflicts=tuple(sorted(set(hard_conflicts))),
        explanation_codes=tuple(sorted(set(explanations))),
    )


def ground_identifier_link(
    conn: DbConn, left_ref: str, right_ref: str
) -> IdentifierLinkGroundingV1:
    """Ground a pair without turning advisory similarity into governed truth."""
    return assess_grounded_identifier_link(
        ground_bridge_endpoint(conn, left_ref),
        ground_bridge_endpoint(conn, right_ref),
    )

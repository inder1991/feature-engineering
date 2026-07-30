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
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import FieldEvidence, read_active_field_evidence
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
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

BRIDGE_GROUNDING_VERSION = "1.0.0"

_WORD_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TYPE_PARAMETER = re.compile(r"\s*\([^)]*\)\s*$")

_TYPE_FAMILY = {
    "integer": "integer",
    "int": "integer",
    "int4": "integer",
    "int8": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "serial": "integer",
    "bigserial": "integer",
    "text": "text",
    "varchar": "text",
    "character varying": "text",
    "char": "text",
    "character": "text",
    "string": "text",
    "uuid": "uuid",
}

_IDENTIFIER_TOKENS = frozenset({
    "id", "identifier", "key", "ref", "reference", "num", "number", "no", "nbr", "code", "cd",
})
_LABEL_TOKENS = frozenset({"name", "nm", "label", "title"})
_DESCRIPTION_TOKENS = frozenset({
    "desc", "description", "narrative", "comment", "comments", "remarks",
})
_FREE_TEXT_TOKENS = frozenset({"text", "prose", "freeform", "free", "memo", "notes"})
_HUMAN_READABLE_TOKENS = frozenset({"display", "readable", "human"})

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


class RepresentationRole(StrEnum):
    IDENTIFIER_VALUE = "identifier_value"
    HUMAN_LABEL = "human_label"
    DESCRIPTION_TEXT = "description_text"
    FREE_TEXT = "free_text"
    UNKNOWN = "unknown"


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


def type_family(data_type: str | None) -> str:
    """Return the coarse equality-comparison family for a SQL type."""
    normalized = _TYPE_PARAMETER.sub("", (data_type or "").strip().lower())
    return _TYPE_FAMILY.get(normalized, "other")


def resolve_type_family(
    data_type: str | None, declared_type: str | None
) -> tuple[str, TypeBasis]:
    """Resolve attested structure first and use a glossary declaration only as fallback."""
    attested = type_family(data_type)
    if attested != "other":
        return attested, TypeBasis.ATTESTED
    return type_family(declared_type), TypeBasis.DECLARED


def _tokens(*values: object) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            tokens.update(_tokens(*value))
            continue
        if isinstance(value, dict):
            tokens.update(_tokens(*value.keys(), *value.values()))
            continue
        text = _CAMEL_BOUNDARY.sub(" ", str(value))
        tokens.update(token for token in _WORD_RE.split(text.lower()) if token)
    return tokens


def _evidence_kind(evidence: FieldEvidence) -> EvidenceKind:
    producer = EvidenceProducer(evidence.producer)
    if producer is EvidenceProducer.HUMAN:
        return EvidenceKind.HUMAN_ATTESTATION
    if producer is EvidenceProducer.SOURCE:
        return EvidenceKind.SOURCE_CONSTRAINT
    if producer is EvidenceProducer.LLM:
        return EvidenceKind.LLM_RECOMMENDATION
    if producer is EvidenceProducer.PROFILER:
        return EvidenceKind.APPROXIMATE_PROFILE
    return EvidenceKind.STRUCTURAL_METADATA


def _evidence_ref(evidence: FieldEvidence) -> EvidenceRefV1:
    observed_at = evidence.created_at if isinstance(evidence.created_at, datetime) else None
    return EvidenceRefV1(
        evidence_id=evidence.evidence_id,
        kind=_evidence_kind(evidence),
        producer=evidence.producer,
        content_hash=evidence.proposed_value_hash,
        observed_at=observed_at,
    )


def _dedupe_evidence(evidence: list[EvidenceRefV1]) -> tuple[EvidenceRefV1, ...]:
    return tuple(
        sorted(
            {item.evidence_id: item for item in evidence}.values(),
            key=lambda item: item.evidence_id,
        )
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


def _observed_format(
    conn: DbConn, logical_ref: str
) -> tuple[str | None, tuple[EvidenceRefV1, ...]]:
    for field_name in ("semantic_type", "logical_representation"):
        evidence = read_active_field_evidence(conn, logical_ref, field_name)
        values = [
            item for item in evidence if item.proposed_value not in (None, "")
        ]
        if values:
            return (
                str(values[0].proposed_value).strip().lower() or None,
                _dedupe_evidence([_evidence_ref(item) for item in values]),
            )
    return None, ()


def _representation_role(
    *,
    column_name: str,
    definition: str | None,
    concept_name: str | None,
    observed_format: str | None,
    data_type_family: str,
) -> RepresentationRole:
    name_tokens = _tokens(column_name)
    definition_tokens = _tokens(definition)
    registered = lookup_concept(concept_name) if concept_name else None
    concept_tokens = _tokens(concept_name)

    # An explicit structural name/description suffix is stronger negative evidence than a possibly
    # mistaken concept projection.  Exact word tokens avoid the classic "mandate contains date"
    # substring bug.
    if name_tokens & _DESCRIPTION_TOKENS:
        return RepresentationRole.DESCRIPTION_TEXT
    if name_tokens & _LABEL_TOKENS:
        return RepresentationRole.HUMAN_LABEL
    if name_tokens & _FREE_TEXT_TOKENS:
        return RepresentationRole.FREE_TEXT

    if registered is not None:
        if registered.group == "identifier":
            return RepresentationRole.IDENTIFIER_VALUE
        if registered.group in {"label", "categorical"} and (
            concept_tokens & (_LABEL_TOKENS | _DESCRIPTION_TOKENS)
        ):
            return RepresentationRole.HUMAN_LABEL
        if registered.group == "text":
            return RepresentationRole.FREE_TEXT

    if name_tokens & _IDENTIFIER_TOKENS:
        return RepresentationRole.IDENTIFIER_VALUE
    if observed_format in {"identifier", "numeric_string"}:
        return RepresentationRole.IDENTIFIER_VALUE
    if definition_tokens & _DESCRIPTION_TOKENS:
        return RepresentationRole.DESCRIPTION_TEXT
    if (
        definition_tokens & (_LABEL_TOKENS | _HUMAN_READABLE_TOKENS)
        and data_type_family == "text"
    ):
        return RepresentationRole.HUMAN_LABEL
    return RepresentationRole.UNKNOWN


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
    refs = _dedupe_evidence(
        [
            *concept_grounding.evidence_refs,
            *entity_evidence,
            *namespace_evidence,
            *population_evidence,
            *format_evidence,
            *metadata_evidence,
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
        key_member_role=(
            KeyMemberRole.PRIMARY if is_grain else KeyMemberRole.UNKNOWN
        ),
        tuple_key_role=(
            TupleKeyRole.COMPLETE_UNIQUE_KEY if is_grain else TupleKeyRole.UNKNOWN
        ),
        observed_format=observed_format,
        metadata_facets=facets,
        evidence_refs=refs,
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

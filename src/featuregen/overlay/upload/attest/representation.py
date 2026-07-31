"""The deterministic REPRESENTATION ruleset — what a column's name/type/format say it physically IS.

Extracted from :mod:`attest.bridge_grounding` (ingestion-richness Task 2) so that bridge grounding
and the enrichment concept critic consume ONE ruleset instead of two drifting copies. A
representation claim ("this column holds an identifier value" / "this is description prose") is a
different claim from a concept assignment, an entity link, or a namespace — this module keeps it
deterministic, token-exact, and free of any LLM involvement, so it can serve as REFUTING evidence
that no model recommendation may overturn.

Everything here is behavior-identical to the bridge_grounding originals; ``bridge_grounding``
re-imports these names, and its unchanged test suite is the proof of the move. The only genuinely
new logic is :func:`shape_conflicts` — the namespace-shape extension the concept critic consumes.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import EvidenceProducer
from featuregen.overlay.field_evidence import FieldEvidence, read_active_field_evidence
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    TypeBasis,
)
from featuregen.overlay.upload.concepts import concept as lookup_concept

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


class RepresentationRole(StrEnum):
    IDENTIFIER_VALUE = "identifier_value"
    HUMAN_LABEL = "human_label"
    DESCRIPTION_TEXT = "description_text"
    FREE_TEXT = "free_text"
    UNKNOWN = "unknown"


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


def observed_format(
    conn: DbConn, logical_ref: str
) -> tuple[str | None, tuple[EvidenceRefV1, ...]]:
    """The governed observed format claim for one column, with the evidence that carries it."""
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


def representation_role(
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

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


# ── The NEW namespace-shape extension (ingestion-richness Task 2) ────────────────────────────────
#
# ``shape_conflicts`` answers ONE refute-oriented question deterministically: does this column's
# own shape (name tokens, declared type, definition tokens) CONTRADICT the identifier concept it
# was assigned? It exists because the live conflation defects were all shape-visible — a BIC-shaped
# column classified into the CIF namespace, a description classified as a branch identifier, an
# amount classified as a counterparty id — and a deterministic contradiction must be able to refute
# without (and before) any LLM. Codes are CLOSED; exact word tokens only (the ``_tokens`` splitter
# — never substrings, so "mandate" cannot fire "date" nor "grate" fire "rate").

#: Closed conflict vocabulary — everything this module can ever emit. Consumers (the concept
#: critic, stage details, decision-trail reasons) may rely on this set being exhaustive.
SHAPE_CONFLICT_CODES = frozenset({
    "identifier_namespace_mismatch",
    "name_or_description_not_identifier",
    "measure_not_identifier",
})

# Measure-name word tokens. Deliberately the four the remediation plan closes on — a wider net
# (e.g. "balance", "pct") buys recall at precision's expense, and a deterministic refuter must
# never wrongly kill a correct assignment.
_MEASURE_NAME_TOKENS = frozenset({"amt", "amount", "rate", "bal"})

# Declared SQL types that are inherently FRACTIONAL. A double/float/real column can never hold an
# identifier. Integer/decimal/numeric types are deliberately EXCLUDED: customer and account numbers
# are routinely declared integral (NUMBER/bigint), so treating them as measure evidence would refute
# correct assignments — the false positive a deterministic refuter must not have.
_FRACTIONAL_TYPES = frozenset({
    "double", "double precision", "float", "float4", "float8", "real",
})

# Shape claims -> the registry namespace each claim identifies. A claim fires on exact word tokens
# in the column NAME or DEFINITION: the token "bic" (or the glossary's own "8/11 alphanumeric"
# shape wording) claims the SWIFT BIC namespace; "uuid"/"uetr" claim the SWIFT gpi UETR namespace.
# These are the only shapes with an unambiguous single-namespace meaning today; scheme codes, CIFs
# and serials are shape-anonymous and stay the critic's (LLM) question.
_BIC_NAMESPACE = "swift_bic"
_UETR_NAMESPACE = "swift_uetr"


def _fractional_declared_type(declared_type: str | None) -> bool:
    normalized = _TYPE_PARAMETER.sub("", (declared_type or "").strip().lower())
    return normalized in _FRACTIONAL_TYPES


def _claimed_namespaces(tokens: set[str]) -> set[str]:
    """The identifier namespaces the column's own name/definition wording claims."""
    claimed: set[str] = set()
    if "bic" in tokens or ("alphanumeric" in tokens and tokens & {"8", "11"}):
        claimed.add(_BIC_NAMESPACE)
    if tokens & {"uuid", "uetr"}:
        claimed.add(_UETR_NAMESPACE)
    return claimed


def shape_conflicts(
    column_name: str,
    declared_type: str | None,
    definition: str | None,
    concept: str | None,
) -> tuple[str, ...]:
    """Deterministic contradictions between a column's shape and its proposed IDENTIFIER concept.

    Returns a sorted tuple of closed codes (subset of :data:`SHAPE_CONFLICT_CODES`); ``()`` for a
    clean assignment AND for any non-identifier concept — this extension corroborates identifier
    assignments only, so measures/labels/temporals pass through untouched.

    * ``identifier_namespace_mismatch`` — the name/definition claims a specific namespace shape
      (BIC / UUID-UETR) but the concept's registry namespace is a different value space.
    * ``name_or_description_not_identifier`` — :func:`representation_role` reads the column as
      description/label/free text. The role is computed WITHOUT the proposed concept: the concept
      under refutation must not be allowed to vouch for itself (a registered identifier concept
      would otherwise short-circuit the definition-token evidence).
    * ``measure_not_identifier`` — a measure-name word token, or an inherently fractional declared
      type, on a column claimed to be an identifier.
    """
    registered = lookup_concept((concept or "").strip().lower())
    if registered is None or registered.group != "identifier":
        return ()
    tokens = _tokens(column_name) | _tokens(definition)
    conflicts: set[str] = set()

    claimed = _claimed_namespaces(tokens)
    if claimed and registered.namespace not in claimed:
        conflicts.add("identifier_namespace_mismatch")

    role = representation_role(
        column_name=column_name,
        definition=definition,
        concept_name=None,             # never let the disputed concept vouch for itself
        observed_format=None,
        data_type_family=type_family(declared_type),
    )
    if role in {
        RepresentationRole.DESCRIPTION_TEXT,
        RepresentationRole.HUMAN_LABEL,
        RepresentationRole.FREE_TEXT,
    }:
        conflicts.add("name_or_description_not_identifier")

    if _tokens(column_name) & _MEASURE_NAME_TOKENS or _fractional_declared_type(declared_type):
        conflicts.add("measure_not_identifier")
    return tuple(sorted(conflicts))

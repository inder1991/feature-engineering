"""Phase-3B.2B — cross-catalog entity-bridge candidate discovery.

A bridge candidate links two catalog-local identifier columns that denote the SAME entity in DISTINCT
uploads (e.g. core.customer_master.customer_id <-> crm.customers.customer_id). Governed via the concept
registry (concept group='identifier' + entity_link), NEVER the free-text graph_node.entity tag. Read-only
and deterministic; a candidate becomes a governed fact only when proposed + confirmed (later tasks)."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.bridge_assessment import (
    CANDIDATE_FAMILY_IDENTIFIER_LINK,
    ConceptAuthority,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    KeyMemberRole,
    TupleKeyRole,
    TypeBasis,
    candidate_id_for,
)
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

BRIDGE_DERIVATION_VERSION = "1.0.0"

_TYPE_FAMILY = {
    "integer": "integer", "int": "integer", "int4": "integer", "int8": "integer",
    "bigint": "integer", "smallint": "integer", "serial": "integer", "bigserial": "integer",
    "text": "text", "varchar": "text", "character varying": "text", "char": "text",
    "character": "text", "string": "text",
    "uuid": "uuid",
}


#: `varchar(150)`, `timestamp(0)`, `decimal(18,2)` — how every real DDL export writes a type. The
#: parameter is a length or precision, never part of the type's FAMILY.
_TYPE_PARAMETER = re.compile(r"\s*\([^)]*\)\s*$")


def _type_family(data_type: str | None) -> str:
    """The family a declared type belongs to, ignoring any length/precision parameter.

    A `varchar(150)` and a `varchar(50)` hold the same KIND of value, so refusing to bridge them
    would be refusing on a formatting detail. An exact lookup matched only the bare `varchar`, which
    made every column of the real second source unclassifiable — the same inert outcome as the
    missing declared type, one layer further down.
    """
    normalized = _TYPE_PARAMETER.sub("", (data_type or "").strip().lower())
    return _TYPE_FAMILY.get(normalized, "other")


@dataclass(frozen=True, slots=True)
class BridgeCandidateV1:
    candidate_id: str
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef
    data_type_family: str
    left_is_grain: bool
    right_is_grain: bool
    #: How the matched family was established: ``attested`` (both sides from a structural source),
    #: ``declared`` (both from a glossary file's own answer), or ``mixed``. Advisory metadata for the
    #: human confirmer — a DECLARED type is someone's spreadsheet entry, not a read of the physical
    #: schema — and deliberately OUTSIDE ``candidate_id``, so re-deriving the same pair after a
    #: structural source attests the type keeps the SAME candidate rather than forking a second one.
    type_basis: str = "attested"
    candidate_family: str = CANDIDATE_FAMILY_IDENTIFIER_LINK


@dataclass(frozen=True, slots=True)
class _IdCol:
    catalog_source: str
    table_name: str
    column_name: str
    entity: str
    type_family: str
    is_grain: bool
    type_basis: str


def _resolve_family(data_type: str | None, declared_type: str | None) -> tuple[str, str]:
    """``(family, basis)`` for one column, attested first.

    A GLOSSARY upload attests no physical type — the FTR adapter emits ``CanonicalRow.type='unknown'``
    and carries the file's own answer in ``graph_node.declared_type`` — so reading ``data_type`` alone
    classified every glossary column as an unrecognised family and dropped it. That made a bridge
    candidate structurally impossible for a glossary-sourced catalog: measured on the deployed FTR
    catalog, 126/126 columns are ``data_type='unknown'`` while 113 carry ``declared_type='string'``,
    including the single ``customer_id`` the whole cross-catalog story hangs on.

    The attested value stays the stronger authority and decides whenever it classifies. The declared
    value is consulted ONLY when nothing was attested, and the caller records that weaker basis on the
    candidate rather than hiding it. An unclassifiable declared value is NOT a wildcard — it stays
    ``other`` and is excluded, so the family check keeps doing the job it exists for."""
    attested = _type_family(data_type)
    if attested != "other":
        return attested, "attested"
    return _type_family(declared_type), "declared"


def _identifier_columns(conn, *, roles: Iterable[str]) -> list[_IdCol]:
    rows = conn.execute(
        "SELECT catalog_source, table_name, column_name, data_type, concept, is_grain, declared_type "
        "FROM graph_node "
        "WHERE kind = 'column' AND concept IS NOT NULL "
        "AND visible_requires <@ %s "
        "ORDER BY catalog_source, object_ref",
        (allowed_sensitivities(roles),)).fetchall()
    out: list[_IdCol] = []
    for catalog_source, table_name, column_name, data_type, concept_name, is_grain, declared_type in rows:
        c = concept(concept_name)
        if c is None or c.group != "identifier" or not c.entity_link:
            continue
        family, basis = _resolve_family(data_type, declared_type)
        out.append(_IdCol(catalog_source=catalog_source, table_name=table_name, column_name=column_name,
                          entity=c.entity_link, type_family=family, is_grain=bool(is_grain),
                          type_basis=basis))
    return out


def _col_ref(col: _IdCol) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=col.catalog_source, object_kind="column", schema="public",
                            table=col.table_name, column=col.column_name)


def _candidate(entity: str, a: _IdCol, b: _IdCol) -> BridgeCandidateV1:
    left, right = sorted((a, b), key=lambda c: (c.catalog_source, c.table_name, c.column_name))

    def endpoint(col: _IdCol) -> IdentifierEndpointV1:
        member = IdentifierColumnMemberV1(
            logical_column_ref=normalize_ref(
                col.catalog_source, "public", col.table_name, col.column_name),
            data_type_family=col.type_family,
            type_basis=TypeBasis(col.type_basis),
            key_member_role=KeyMemberRole.PRIMARY if col.is_grain else KeyMemberRole.UNKNOWN,
        )
        return IdentifierEndpointV1(
            logical_table_ref=normalize_ref(col.catalog_source, "public", col.table_name),
            members=(member,),
            entity_id=entity,
            concept=f"{entity}_id",
            concept_authority=ConceptAuthority.DETERMINISTIC,
            tuple_key_role=(
                TupleKeyRole.COMPLETE_UNIQUE_KEY if col.is_grain
                else TupleKeyRole.UNKNOWN),
        )

    candidate_id = candidate_id_for(
        CANDIDATE_FAMILY_IDENTIFIER_LINK, endpoint(left), endpoint(right))
    basis = left.type_basis if left.type_basis == right.type_basis else "mixed"
    return BridgeCandidateV1(
        candidate_id=candidate_id, entity_id=entity, left_ref=_col_ref(left), right_ref=_col_ref(right),
        data_type_family=left.type_family, left_is_grain=left.is_grain, right_is_grain=right.is_grain,
        type_basis=basis)


def derive_bridge_candidates(conn, *, roles: Iterable[str] = ()) -> tuple[BridgeCandidateV1, ...]:
    """Candidate bridges from declared metadata: identifier concepts for the SAME entity_link, in DISTINCT
    catalog sources, with a COMPATIBLE type family. Deterministic (canonical unordered pair + sorted
    output). Read-only."""
    by_entity: dict[str, list[_IdCol]] = {}
    for col in _identifier_columns(conn, roles=roles):
        # `other` = neither an attested nor a declared type this platform can classify. Excluded, so
        # the family check below can never degrade into "unknown matches anything".
        if col.type_family != "other":
            by_entity.setdefault(col.entity, []).append(col)
    cands: dict[str, BridgeCandidateV1] = {}
    for entity, group in by_entity.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.catalog_source == b.catalog_source or a.type_family != b.type_family:
                    continue
                c = _candidate(entity, a, b)
                cands[c.candidate_id] = c
    return tuple(cands[k] for k in sorted(cands))

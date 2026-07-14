"""Phase-3B.2B — cross-catalog entity-bridge candidate discovery.

A bridge candidate links two catalog-local identifier columns that denote the SAME entity in DISTINCT
uploads (e.g. core.customer_master.customer_id <-> crm.customers.customer_id). Governed via the concept
registry (concept group='identifier' + entity_link), NEVER the free-text graph_node.entity tag. Read-only
and deterministic; a candidate becomes a governed fact only when proposed + confirmed (later tasks)."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.read_scope import allowed_sensitivities

BRIDGE_DERIVATION_VERSION = "1.0.0"

_TYPE_FAMILY = {
    "integer": "integer", "int": "integer", "int4": "integer", "int8": "integer",
    "bigint": "integer", "smallint": "integer", "serial": "integer", "bigserial": "integer",
    "text": "text", "varchar": "text", "character varying": "text", "char": "text",
    "character": "text", "string": "text",
    "uuid": "uuid",
}


def _type_family(data_type: str | None) -> str:
    return _TYPE_FAMILY.get((data_type or "").strip().lower(), "other")


@dataclass(frozen=True, slots=True)
class BridgeCandidateV1:
    candidate_id: str
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef
    data_type_family: str
    left_is_grain: bool
    right_is_grain: bool


@dataclass(frozen=True, slots=True)
class _IdCol:
    catalog_source: str
    table_name: str
    column_name: str
    entity: str
    type_family: str
    is_grain: bool


def _identifier_columns(conn, *, roles: Iterable[str]) -> list[_IdCol]:
    rows = conn.execute(
        "SELECT catalog_source, table_name, column_name, data_type, concept, is_grain FROM graph_node "
        "WHERE kind = 'column' AND concept IS NOT NULL "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
        "ORDER BY catalog_source, object_ref",
        (allowed_sensitivities(roles),)).fetchall()
    out: list[_IdCol] = []
    for catalog_source, table_name, column_name, data_type, concept_name, is_grain in rows:
        c = concept(concept_name)
        if c is None or c.group != "identifier" or not c.entity_link:
            continue
        out.append(_IdCol(catalog_source=catalog_source, table_name=table_name, column_name=column_name,
                          entity=c.entity_link, type_family=_type_family(data_type), is_grain=bool(is_grain)))
    return out


def _col_ref(col: _IdCol) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=col.catalog_source, object_kind="column", schema="public",
                            table=col.table_name, column=col.column_name)


def _candidate(entity: str, a: _IdCol, b: _IdCol) -> BridgeCandidateV1:
    left, right = sorted((a, b), key=lambda c: (c.catalog_source, c.table_name, c.column_name))
    material = (f"{entity}|{left.catalog_source}.{left.table_name}.{left.column_name}"
                f"|{right.catalog_source}.{right.table_name}.{right.column_name}|{BRIDGE_DERIVATION_VERSION}")
    candidate_id = hashlib.sha256(material.encode()).hexdigest()[:16]
    return BridgeCandidateV1(
        candidate_id=candidate_id, entity_id=entity, left_ref=_col_ref(left), right_ref=_col_ref(right),
        data_type_family=left.type_family, left_is_grain=left.is_grain, right_is_grain=right.is_grain)


def derive_bridge_candidates(conn, *, roles: Iterable[str] = ()) -> tuple[BridgeCandidateV1, ...]:
    """Candidate bridges from declared metadata: identifier concepts for the SAME entity_link, in DISTINCT
    catalog sources, with a COMPATIBLE type family. Deterministic (canonical unordered pair + sorted
    output). Read-only."""
    by_entity: dict[str, list[_IdCol]] = {}
    for col in _identifier_columns(conn, roles=roles):
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

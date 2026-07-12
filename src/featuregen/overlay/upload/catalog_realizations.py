"""Phase-3B.2A — derive a catalog's physical realizations of the global entity relationships from its
declared joins. Pure, deterministic, read-only over ``graph_node``/``graph_edge``. The semantic hop a
join realizes is its OBJECT-GRAIN pair (each = the entity of the table's is_grain column), NOT the
join-key entity. Behaviour-neutral: nothing consumes this until the 3B.3 planner."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.taxonomy.entity_registry import (
    GRAPH_VERSION,
    global_relationship_for,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityRelationshipDefinitionV1,
    EntityRelationshipProposalV1,
    RealizationAuthority,
    RelationshipProposalStatus,
    RelationshipStatus,
)

REALIZATION_DERIVATION_VERSION = "1.0.0"

# The upload cardinality tokens (canonical.py) -> the governed Cardinality. Unstated -> MANY_TO_ONE
# (the overwhelmingly common FK direction). N:N is not a valid upload token.
CARDINALITY_TOKENS: dict[str, Cardinality] = {
    "N:1": Cardinality.MANY_TO_ONE,
    "1:N": Cardinality.ONE_TO_MANY,
    "1:1": Cardinality.ONE_TO_ONE,
}


def cardinality_from_token(token: str | None) -> Cardinality:
    if token is None or token == "":
        return Cardinality.MANY_TO_ONE
    try:
        return CARDINALITY_TOKENS[token]
    except KeyError:
        raise ValueError(f"unknown cardinality token: {token!r}") from None


def invert_cardinality(c: Cardinality) -> Cardinality:
    """The cardinality read from the opposite direction (endpoints swapped)."""
    if c is Cardinality.MANY_TO_ONE:
        return Cardinality.ONE_TO_MANY
    if c is Cardinality.ONE_TO_MANY:
        return Cardinality.MANY_TO_ONE
    return c   # one_to_one and many_to_many are symmetric


@dataclass(frozen=True, slots=True)
class NormalizedRealization:
    """The result of orienting a declared join against a global relationship: the bound relationship id,
    the declared cardinality (inverted if the join was authored in reverse), whether it was reverse-
    authored, and whether the cardinality conflicts with the global model."""
    relationship_id: str
    declared_cardinality: Cardinality
    conflict: bool
    reversed_authoring: bool


def normalize_realization(
    *, from_object_grain: str, to_object_grain: str, declared: Cardinality,
    global_rel: EntityRelationshipDefinitionV1 | None) -> NormalizedRealization | None:
    """Orient a declared join (grains ``from -> to``, cardinality ``declared``) against ``global_rel``.
    Returns None when there is no global relationship (caller records a catalog_local_relationship +
    proposal). Otherwise binds the relationship and reports whether the join was reverse-authored (so its
    cardinality is inverted to compare) and whether the (oriented) cardinality CONFLICTS with the global
    model (fail closed — surfaced, never silently overridden)."""
    if global_rel is None:
        return None
    if (from_object_grain, to_object_grain) == (global_rel.from_entity, global_rel.to_entity):
        oriented, reversed_ = declared, False
    else:
        # reverse orientation: the join was authored to->from; invert its cardinality to compare
        oriented, reversed_ = invert_cardinality(declared), True
    return NormalizedRealization(
        relationship_id=global_rel.relationship_id, declared_cardinality=oriented,
        conflict=oriented is not global_rel.cardinality, reversed_authoring=reversed_)


def table_of(column_object_ref: str) -> str:
    """The table object_ref of a column object_ref: ``public.accounts.customer_id`` -> ``public.accounts``.
    Precondition: expects a COLUMN object_ref (``public.<table>.<column>``); a bare table ref is over-stripped."""
    return column_object_ref.rsplit(".", 1)[0]


def _entity_of_concept(concept_name: str | None) -> str | None:
    if not concept_name:
        return None
    c = concept(concept_name)
    return c.entity_link if c is not None else None


def object_grain(conn, catalog_source: str, table_object_ref: str) -> str | None:
    """The OBJECT GRAIN of a table: the ``entity_link`` of the concept of the table's ``is_grain`` column.
    ``None`` when the table has no grain column or its grain concept links no entity. This is the table's
    grain — NOT a join-key column's entity."""
    row = conn.execute(
        "SELECT concept FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
        "AND table_name = %s AND is_grain = true "
        "AND object_ref LIKE %s ORDER BY object_ref LIMIT 1",
        (catalog_source, table_object_ref.rsplit(".", 1)[-1], table_object_ref + ".%")).fetchone()
    return _entity_of_concept(row[0]) if row is not None else None


def key_entity(conn, catalog_source: str, column_object_ref: str) -> str | None:
    """The join-KEY entity of a column: its concept's ``entity_link`` (governed). ``None`` when the
    column has no concept or its concept links no entity."""
    row = conn.execute(
        "SELECT concept FROM graph_node WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (catalog_source, column_object_ref)).fetchone()
    return _entity_of_concept(row[0]) if row is not None else None


CONCEPT_REGISTRY_FOR_REALIZATION = "concepts@1"   # a version tag for the concept vocabulary


@dataclass(frozen=True, slots=True)
class CatalogRealizationResult:
    catalog_source: str
    realizations: tuple[CatalogEntityRelationshipV1, ...]        # bound to a global relationship, VALID
    conflicts: tuple[CatalogEntityRelationshipV1, ...]           # cardinality conflict (fail-closed)
    local_relationships: tuple[CatalogEntityRelationshipV1, ...]  # unmapped grain pair, intra-catalog-only
    proposals: tuple[EntityRelationshipProposalV1, ...]          # governance proposals for the unmapped
    fingerprint: str


def _catalog_schema_fingerprint(conn, catalog_source: str) -> str:
    """A deterministic hash of the catalog's schema-relevant metadata (columns + grain/entity/concept +
    join edges) — so the derivation's cache key changes iff the catalog's declared structure changes."""
    nodes = conn.execute(
        "SELECT object_ref, kind, table_name, is_grain, concept FROM graph_node "
        "WHERE catalog_source = %s ORDER BY object_ref", (catalog_source,)).fetchall()
    edges = conn.execute(
        "SELECT from_ref, to_ref, cardinality FROM graph_edge "
        "WHERE catalog_source = %s AND kind = 'joins' ORDER BY from_ref, to_ref", (catalog_source,)).fetchall()
    blob = json.dumps({"nodes": [list(n) for n in nodes], "edges": [list(e) for e in edges]},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def realization_fingerprint(conn, catalog_source: str) -> str:
    """The composite immutable key — catalog schema fingerprint + global-graph version + concept-registry
    version + derivation version — NOT the mutable catalog_source name alone."""
    parts = "|".join((_catalog_schema_fingerprint(conn, catalog_source), GRAPH_VERSION,
                      CONCEPT_REGISTRY_FOR_REALIZATION, REALIZATION_DERIVATION_VERSION))
    return hashlib.sha256(parts.encode()).hexdigest()


def _join_edges(conn, catalog_source: str) -> list[tuple[str, str, str | None]]:
    """Intra-catalog declared join edges (both endpoints in THIS catalog — a cross-source target is a
    3B.2B bridge concern, not a realization)."""
    return conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality FROM graph_edge e "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' "
        "  AND EXISTS(SELECT 1 FROM graph_node n WHERE n.catalog_source = e.catalog_source "
        "             AND n.object_ref = e.to_ref) "
        "ORDER BY e.from_ref, e.to_ref", (catalog_source,)).fetchall()


def derive_catalog_realizations(conn, catalog_source: str) -> CatalogRealizationResult:
    """Derive this catalog's physical realizations from its declared joins. Deterministic, read-only.
    Each intra-catalog join whose object-grain pair matches a global relationship becomes a bound
    realization (a cardinality contradiction -> a conflict bucket); an unmapped grain pair -> a
    catalog-local relationship + a governance proposal. Object grain = the table's is_grain column
    entity, distinct from the join-key entity."""
    realizations: list[CatalogEntityRelationshipV1] = []
    conflicts: list[CatalogEntityRelationshipV1] = []
    local: list[CatalogEntityRelationshipV1] = []
    proposals: list[EntityRelationshipProposalV1] = []

    for from_key, to_key, card_token in _join_edges(conn, catalog_source):
        from_table, to_table = table_of(from_key), table_of(to_key)
        fg, tg = object_grain(conn, catalog_source, from_table), object_grain(conn, catalog_source, to_table)
        fke, tke = key_entity(conn, catalog_source, from_key), key_entity(conn, catalog_source, to_key)
        if fg is None or tg is None or fke is None or tke is None:
            continue                                            # unresolvable grain/key -> not derivable
        declared = cardinality_from_token(card_token)
        # try forward, then reverse orientation against the global model
        norm = normalize_realization(from_object_grain=fg, to_object_grain=tg,
                                     declared=declared, global_rel=global_relationship_for(fg, tg)) \
            or normalize_realization(from_object_grain=fg, to_object_grain=tg,
                                     declared=declared, global_rel=global_relationship_for(tg, fg))
        rid = f"{catalog_source}:{from_key}->{to_key}"
        rel = CatalogEntityRelationshipV1(
            realization_id=rid, relationship_id=(norm.relationship_id if norm else ""),
            catalog_source=catalog_source,
            from_object_ref=from_table, from_object_grain=fg, to_object_ref=to_table, to_object_grain=tg,
            from_key_ref=from_key, from_key_entity=fke, to_key_ref=to_key, to_key_entity=tke,
            declared_cardinality=declared,
            reversed_authoring=(norm.reversed_authoring if norm else False),
            authority=RealizationAuthority.DECLARED_JOIN, status=RelationshipStatus.ACTIVE)
        if norm is None:
            local.append(rel)
            proposals.append(EntityRelationshipProposalV1(
                proposal_id=f"prop:{rid}", proposed_from_entity=fg, proposed_to_entity=tg,
                proposed_cardinality=declared, evidence_refs=(from_key, to_key),
                source_catalog=catalog_source, inferred_by="catalog_realization_derivation@1",
                status=RelationshipProposalStatus.PENDING))
        elif norm.conflict:
            conflicts.append(rel)
        else:
            realizations.append(rel)

    return CatalogRealizationResult(
        catalog_source=catalog_source, realizations=tuple(realizations), conflicts=tuple(conflicts),
        local_relationships=tuple(local), proposals=tuple(proposals),
        fingerprint=realization_fingerprint(conn, catalog_source))

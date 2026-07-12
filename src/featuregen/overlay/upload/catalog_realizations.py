"""Phase-3B.2A — derive a catalog's physical realizations of the global entity relationships from its
declared joins. Pure, deterministic, read-only over ``graph_node``/``graph_edge``. The semantic hop a
join realizes is its OBJECT-GRAIN pair (each = the entity of the table's is_grain column), NOT the
join-key entity. Behaviour-neutral: nothing consumes this until the 3B.3 planner."""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    EntityRelationshipDefinitionV1,
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


_SCHEMA = "public"


def table_of(column_object_ref: str) -> str:
    """The table object_ref of a column object_ref: ``public.accounts.customer_id`` -> ``public.accounts``."""
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

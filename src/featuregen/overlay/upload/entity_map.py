"""Entity Map v0 — the READ-ONLY face of the ontology (ingestion-richness Task 3D).

The ontology has an engine and consumers but no face: entities as nodes, the columns carrying each
entity grouped by catalog, and the available cross-catalog links as edges. This module renders
whatever the governed readers currently say — it grows richer as other tasks land, with zero rework.

**One availability truth.** The link population comes from
:func:`featuregen.overlay.upload.bridge_assessment.available_identifier_links` VERBATIM. This module
never re-reads the candidate ledger and never folds lifecycles itself — a second availability
interpretation is the banned defect class (a map that disagrees with governance or the planner about
which links exist is worse than no map). ``tests/.../test_entity_map.py`` guards this statically and
by byte-identity against the reader.

**Direction-specific eligibility is read, never re-derived.** Where a current directional
realization exists it is loaded through :func:`bridge_store.load_current_bridge_realizations` and
its eligibility comes from the existing :func:`bridge_realization.eligible_for_sandbox` /
:func:`bridge_realization.eligible_for_production` predicates — the same answers every other
consumer gets.

**Read scope.** Column COUNTS and SAMPLE REFS are read-scoped exactly like asset detail and search
(``COALESCE(visible_requires,'{}') <@ allowed_classes(roles)``): a caller who may not see a
restricted column sees neither the column nor its contribution to any count. The LINKS are the
un-scoped availability truth every existing consumer of ``available_identifier_links`` (search,
asset relationships, governance, feature suggestion) already serves — scoping them here would break
the one-truth byte-identity this read model exists to keep.

**No freshness window.** The map is a truth surface over the graph as stored — the same basis as
``scripts/verify_catalog_richness.sql``, whose per-catalog entity coverage the map's counts must
reconcile with. Adding a private freshness window here would be a second, invisible filter (the
defect search once had).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.bridge_assessment import (
    AvailableIdentifierLinkV1,
    IdentifierEndpointV1,
    LinkReviewStatus,
    available_identifier_links,
)
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, display_entity
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import allowed_classes
from featuregen.overlay.upload.semantic_context import composed_link_realizations
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

#: Sample column refs shown per (entity, catalog). A taste of what carries the entity, not a listing
#: — the node click deep-links to search for the full read-scoped set.
SAMPLE_LIMIT = 3

#: Link statuses — the platform's one link-status vocabulary: confirmed means a human endorsed the
#: semantics (the folded review status the READER reports), proposed means derived and usable but
#: unreviewed. A proposed link is honest output, never a failure state (standing product direction).
STATUS_CONFIRMED = "confirmed"
STATUS_PROPOSED = "proposed"


@dataclass(frozen=True, slots=True)
class EntityCatalogGroupV1:
    """One catalog's read-scoped contribution to an entity node."""

    catalog_source: str
    column_count: int
    sample_refs: tuple[str, ...]  # flat schema.table.column, read-scoped, first SAMPLE_LIMIT


@dataclass(frozen=True, slots=True)
class EntityMapNodeV1:
    """One entity present in the graph (or referenced by a link endpoint)."""

    entity_id: str
    registered: bool              # in known_entities() — the closed Concept.entity_link vocabulary
    column_count: int             # read-scoped total across catalogs
    catalogs: tuple[EntityCatalogGroupV1, ...]


@dataclass(frozen=True, slots=True)
class EntityMapEndpointV1:
    """One side of a link, flattened for display. ``namespace`` is read from the concept registry
    via the endpoint's concept where derivable — never guessed from column names."""

    catalog_source: str
    table_ref: str                # flat schema.table
    column_refs: tuple[str, ...]  # flat schema.table.column per tuple member
    entity_id: str | None
    concept: str | None
    namespace: str | None


@dataclass(frozen=True, slots=True)
class EntityMapRealizationV1:
    """One current directional realization's eligibility, read from the existing readers."""

    from_catalog_source: str
    from_table_ref: str
    to_catalog_source: str
    to_table_ref: str
    lifecycle: str
    safety_status: str
    sandbox_eligible: bool
    production_eligible: bool


@dataclass(frozen=True, slots=True)
class EntityMapLinkV1:
    """One AVAILABLE identifier link — an edge between two entity nodes."""

    candidate_id: str
    candidate_revision_id: str
    bridge_fact_key: str
    status: str                   # STATUS_CONFIRMED | STATUS_PROPOSED
    folded_status: str | None     # DRAFT / PARTIALLY_CONFIRMED / VERIFIED — extra honesty, not a gate
    strength: int                 # the reader's ranking strength, verbatim
    left: EntityMapEndpointV1
    right: EntityMapEndpointV1
    realizations: tuple[EntityMapRealizationV1, ...]


@dataclass(frozen=True, slots=True)
class EntityMapV1:
    entities: tuple[EntityMapNodeV1, ...]
    links: tuple[EntityMapLinkV1, ...]


def _endpoint_view(endpoint: IdentifierEndpointV1) -> EntityMapEndpointV1:
    source, schema, table, _column = parse_ref(endpoint.logical_table_ref)
    columns: list[str] = []
    for member in endpoint.members:
        _m_source, m_schema, m_table, m_column = parse_ref(member.logical_column_ref)
        columns.append(f"{m_schema}.{m_table}.{m_column}")
    concept = endpoint.concept
    registered = CONCEPT_REGISTRY.get(concept) if concept else None
    # The assessment's own entity conclusion wins; a concept's registry entity_link fills in only
    # when the assessment concluded none. Namespace is registry-only: no concept, no namespace.
    # The DISPLAY entity then resolves through the alias seam (semantic Task 2 / D12.1): a
    # counterparty_id endpoint displays `customer` — counterparty is a party ROLE. Stored fact
    # keys and the assessment's persisted entity_id are untouched; this is presentation.
    entity_id = display_entity(
        concept, endpoint.entity_id or (registered.entity_link if registered else None))
    return EntityMapEndpointV1(
        catalog_source=source,
        table_ref=f"{schema}.{table}",
        column_refs=tuple(columns),
        entity_id=entity_id,
        concept=concept,
        namespace=registered.namespace if registered else None,
    )


def _realization_views(conn, bridge_fact_key: str) -> tuple[EntityMapRealizationV1, ...]:
    # ONE shared composition (semantic_context.composed_link_realizations) renders here and in
    # the semantic-context bundle — the map never re-loads or re-judges eligibility itself.
    views: list[EntityMapRealizationV1] = []
    for composed in composed_link_realizations(conn, bridge_fact_key):
        revision, current = composed.revision, composed.current
        f_source, f_schema, f_table, _ = parse_ref(revision.from_endpoint.logical_table_ref)
        t_source, t_schema, t_table, _ = parse_ref(revision.to_endpoint.logical_table_ref)
        views.append(EntityMapRealizationV1(
            from_catalog_source=f_source,
            from_table_ref=f"{f_schema}.{f_table}",
            to_catalog_source=t_source,
            to_table_ref=f"{t_schema}.{t_table}",
            lifecycle=current.lifecycle.value,
            safety_status=current.safety_status.value,
            sandbox_eligible=composed.sandbox_eligible,
            production_eligible=composed.production_eligible,
        ))
    return tuple(views)


def _link_view(conn, link: AvailableIdentifierLinkV1) -> EntityMapLinkV1:
    availability = link.availability
    confirmed = availability.review_status is LinkReviewStatus.HUMAN_VERIFIED
    return EntityMapLinkV1(
        candidate_id=link.assessment.candidate_id,
        candidate_revision_id=link.assessment.candidate_revision_id,
        bridge_fact_key=availability.bridge_fact_key,
        status=STATUS_CONFIRMED if confirmed else STATUS_PROPOSED,
        folded_status=(
            availability.folded_status.value if availability.folded_status is not None else None),
        strength=link.ranking_strength,
        left=_endpoint_view(link.assessment.left_endpoint),
        right=_endpoint_view(link.assessment.right_endpoint),
        realizations=_realization_views(conn, availability.bridge_fact_key),
    )


def _entity_nodes(
    conn, roles: Iterable[str], link_entities: set[str]
) -> tuple[EntityMapNodeV1, ...]:
    allowed = allowed_classes(roles)
    counts = conn.execute(
        "SELECT entity, catalog_source, count(*) FROM graph_node "
        "WHERE kind='column' AND NULLIF(entity,'') IS NOT NULL "
        "  AND COALESCE(visible_requires,'{}') <@ %s "
        "GROUP BY entity, catalog_source ORDER BY entity, catalog_source",
        (allowed,),
    ).fetchall()
    samples = conn.execute(
        "SELECT entity, catalog_source, object_ref FROM ("
        "  SELECT entity, catalog_source, object_ref,"
        "         row_number() OVER (PARTITION BY entity, catalog_source ORDER BY object_ref) AS rn"
        "  FROM graph_node"
        "  WHERE kind='column' AND NULLIF(entity,'') IS NOT NULL"
        "    AND COALESCE(visible_requires,'{}') <@ %s"
        ") s WHERE rn <= %s ORDER BY entity, catalog_source, object_ref",
        (allowed, SAMPLE_LIMIT),
    ).fetchall()
    sample_map: dict[tuple[str, str], list[str]] = {}
    for entity, catalog, object_ref in samples:
        sample_map.setdefault((entity, catalog), []).append(object_ref)
    by_entity: dict[str, list[EntityCatalogGroupV1]] = {}
    for entity, catalog, count in counts:
        by_entity.setdefault(entity, []).append(EntityCatalogGroupV1(
            catalog_source=catalog,
            column_count=int(count),
            sample_refs=tuple(sample_map.get((entity, catalog), ())),
        ))
    # A link endpoint's entity appears as a node even when this caller sees none of its columns —
    # otherwise the edge would dangle. Count 0 with no samples: nothing hidden is named.
    for entity in sorted(link_entities - set(by_entity)):
        by_entity[entity] = []
    registry = known_entities()
    nodes = [
        EntityMapNodeV1(
            entity_id=entity,
            registered=entity in registry,
            column_count=sum(group.column_count for group in groups),
            catalogs=tuple(groups),
        )
        for entity, groups in by_entity.items()
    ]
    # Biggest first for a stable, size-meaningful default order; id breaks ties deterministically.
    nodes.sort(key=lambda node: (-node.column_count, node.entity_id))
    return tuple(nodes)


def build_entity_map(conn, *, roles: Iterable[str] = ()) -> EntityMapV1:
    """The entity map: read-scoped entity nodes + the verbatim available-link edges."""
    links = tuple(
        _link_view(conn, link) for link in available_identifier_links(conn))
    link_entities = {
        endpoint.entity_id
        for link in links
        for endpoint in (link.left, link.right)
        if endpoint.entity_id
    }
    return EntityMapV1(
        entities=_entity_nodes(conn, roles, link_entities),
        links=links,
    )

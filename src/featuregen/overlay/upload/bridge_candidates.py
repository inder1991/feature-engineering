"""Phase-3B.2B — cross-catalog entity-bridge candidate discovery.

A candidate nominates two catalog-local identifier columns for the same entity; it does not claim
that their identifier namespaces or populations are equal. Bridge-specific grounding keeps those
claims separate and suppresses hard representation/entity/namespace conflicts. Discovery is
read-only and deterministic. A proposal becomes available without human confirmation; confirmation
records review but is not a consumption gate.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.attest.bridge_grounding import (
    BridgeEndpointGroundingV1,
    assess_grounded_identifier_link,
    ground_bridge_endpoint,
    resolve_type_family,
    type_family,
)
from featuregen.overlay.upload.bridge_assessment import (
    CANDIDATE_FAMILY_IDENTIFIER_LINK,
    ConceptAuthority,
    IdentifierLinkAssessmentV1,
)
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

BRIDGE_DERIVATION_VERSION = "1.0.0"

def _type_family(data_type: str | None) -> str:
    """The family a declared type belongs to, ignoring any length/precision parameter.

    A `varchar(150)` and a `varchar(50)` hold the same KIND of value, so refusing to bridge them
    would be refusing on a formatting detail. An exact lookup matched only the bare `varchar`, which
    made every column of the real second source unclassifiable — the same inert outcome as the
    missing declared type, one layer further down.
    """
    return type_family(data_type)


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
    left_concept_authority: str = ConceptAuthority.UNKNOWN.value
    right_concept_authority: str = ConceptAuthority.UNKNOWN.value
    assessment: IdentifierLinkAssessmentV1 | None = None


@dataclass(frozen=True, slots=True)
class _IdCol:
    catalog_source: str
    table_name: str
    column_name: str
    entity: str
    type_family: str
    is_grain: bool
    type_basis: str
    concept_authority: str
    grounding: BridgeEndpointGroundingV1


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
    family, basis = resolve_type_family(data_type, declared_type)
    return family, basis.value


def _identifier_columns(conn, *, roles: Iterable[str]) -> list[_IdCol]:
    rows = conn.execute(
        "SELECT catalog_source, table_name, column_name "
        "FROM graph_node "
        "WHERE kind = 'column' AND concept IS NOT NULL "
        "AND visible_requires <@ %s "
        "ORDER BY catalog_source, object_ref",
        (allowed_sensitivities(roles),)).fetchall()
    out: list[_IdCol] = []
    for catalog_source, table_name, column_name in rows:
        logical_ref = normalize_ref(
            catalog_source, "public", table_name, column_name)
        grounding = ground_bridge_endpoint(conn, logical_ref)
        concept_name = grounding.concept.concept
        c = concept(concept_name) if concept_name is not None else None
        if c is None or c.group != "identifier" or not c.entity_link:
            continue
        out.append(_IdCol(
            catalog_source=catalog_source,
            table_name=table_name,
            column_name=column_name,
            entity=grounding.entity_id or c.entity_link,
            type_family=grounding.data_type_family,
            is_grain=grounding.is_grain,
            type_basis=grounding.type_basis.value,
            concept_authority=grounding.concept.authority.value,
            grounding=grounding,
        ))
    return out


def _col_ref(col: _IdCol) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=col.catalog_source, object_kind="column", schema="public",
                            table=col.table_name, column=col.column_name)


def _candidate(entity: str, a: _IdCol, b: _IdCol) -> BridgeCandidateV1:
    left, right = sorted((a, b), key=lambda c: (c.catalog_source, c.table_name, c.column_name))
    grounding = assess_grounded_identifier_link(left.grounding, right.grounding)
    assessment = grounding.to_assessment()
    basis = left.type_basis if left.type_basis == right.type_basis else "mixed"
    return BridgeCandidateV1(
        candidate_id=assessment.candidate_id,
        entity_id=entity,
        left_ref=_col_ref(left),
        right_ref=_col_ref(right),
        data_type_family=left.type_family, left_is_grain=left.is_grain, right_is_grain=right.is_grain,
        type_basis=basis,
        left_concept_authority=left.concept_authority,
        right_concept_authority=right.concept_authority,
        assessment=assessment,
    )


def _current_identifier_column(conn, ref: CatalogObjectRef) -> _IdCol | None:
    """Resolve one proposal endpoint against the live graph, never against caller-supplied fields."""
    if (
        ref.object_kind.strip().lower() != "column"
        or ref.schema.strip().lower() != "public"
        or not ref.column
    ):
        return None
    logical_ref = normalize_ref(
        ref.catalog_source, "public", ref.table, ref.column)
    grounding = ground_bridge_endpoint(conn, logical_ref)
    if not grounding.exists:
        return None
    concept_name = grounding.concept.concept
    registered = concept(concept_name) if concept_name is not None else None
    if registered is None or registered.group != "identifier" or not registered.entity_link:
        return None
    return _IdCol(
        catalog_source=ref.catalog_source.strip().lower(),
        table_name=ref.table.strip().lower(),
        column_name=ref.column.strip().lower(),
        entity=grounding.entity_id or registered.entity_link,
        type_family=grounding.data_type_family,
        is_grain=grounding.is_grain,
        type_basis=grounding.type_basis.value,
        concept_authority=grounding.concept.authority.value,
        grounding=grounding,
    )


def bridge_catalog_write_error(conn, ref) -> str | None:
    """State-aware bridge write gate shared by propose, confirm and direct entry.

    The structural identity gate in :mod:`overlay.identity` proves ref/value consistency. This gate
    proves that both claimed endpoints still exist as catalog columns and are still classified as
    identifiers for the claimed entity. Human approval is deliberately not required.
    """
    from featuregen.overlay.identity import EntityBridgeRef

    if not isinstance(ref, EntityBridgeRef):
        return "entity_bridge requires an EntityBridgeRef"
    current_endpoints: dict[str, _IdCol] = {}
    for side, endpoint in (("left", ref.left_ref), ("right", ref.right_ref)):
        current = _current_identifier_column(conn, endpoint)
        if current is None:
            return (
                f"entity_bridge {side} endpoint does not exist as a flat logical identifier column: "
                f"{endpoint.catalog_source}::public.{endpoint.table}.{endpoint.column or ''}"
            )
        if current.entity != ref.entity_id.strip().lower():
            return (
                f"entity_bridge {side} endpoint is classified for entity {current.entity!r}, "
                f"not the claimed {ref.entity_id!r}"
            )
        current_endpoints[side] = current
    grounding = assess_grounded_identifier_link(
        current_endpoints["left"].grounding,
        current_endpoints["right"].grounding,
    )
    if grounding.hard_conflicts:
        return (
            "entity_bridge endpoints have a hard grounding conflict: "
            + ", ".join(grounding.hard_conflicts)
        )
    return None


def bridge_candidate_write_error(conn, candidate: BridgeCandidateV1) -> str | None:
    """Stronger gate for the automatic candidate path, including live type/basis agreement."""
    from featuregen.overlay.identity import EntityBridgeRef

    ref = EntityBridgeRef(
        entity_id=candidate.entity_id,
        left_ref=candidate.left_ref,
        right_ref=candidate.right_ref,
    )
    error = bridge_catalog_write_error(conn, ref)
    if error is not None:
        return error
    left = _current_identifier_column(conn, candidate.left_ref)
    right = _current_identifier_column(conn, candidate.right_ref)
    assert left is not None and right is not None  # established by bridge_catalog_write_error
    if left.type_family != right.type_family:
        return (
            f"entity_bridge endpoint type families differ ({left.type_family} vs "
            f"{right.type_family}); an explicit normalization realization is required"
        )
    if left.type_family == "other":
        return "entity_bridge endpoint type family is unclassified"
    live_basis = left.type_basis if left.type_basis == right.type_basis else "mixed"
    if candidate.data_type_family != left.type_family or candidate.type_basis != live_basis:
        return (
            "entity_bridge candidate type evidence is stale: "
            f"candidate={candidate.data_type_family}/{candidate.type_basis}, "
            f"current={left.type_family}/{live_basis}"
        )
    claimed_authority = (
        candidate.left_concept_authority,
        candidate.right_concept_authority,
    )
    current_authority = (left.concept_authority, right.concept_authority)
    if claimed_authority != current_authority:
        return (
            "entity_bridge candidate concept authority is stale: "
            f"candidate={claimed_authority}, current={current_authority}"
        )
    return None


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
                if c.assessment is not None and c.assessment.hard_conflicts:
                    continue
                cands[c.candidate_id] = c
    return tuple(cands[k] for k in sorted(cands))

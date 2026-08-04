"""Context Graph V1 — the one bounded READ MODEL around a catalog anchor (semantic plan Task 7).

**It is a composition, not a new truth.** Every node and edge here is projected from a reader that
already shipped:

* :func:`semantic_context.bundle_from_store` — source meaning, resolved meaning, concept ancestry,
  identifier namespace, party role, neighbouring columns, relationship context, missing-context
  codes. Read-scoped and batched at that seam (D11), so this module inherits the scope rather than
  re-deriving it.
* :func:`lineage.lineage_graph` — the bounded ONE-HOP structural neighbourhood (containment,
  joins, cross-catalog bridges, features and consumers). NO second BFS is implemented here; the
  review's finding was that a parallel traversal is how two views of the same catalog start
  disagreeing.
* :func:`dataset_profiles.build_dataset_profile` + the current catalog-narrative pointer — the
  profile node, identified by ``catalog_profile_revision_id`` and ``dataset_profile_hash``.
* :func:`semantic_adjudication.load_current_adjudication` — the adjudicator's alternatives and any
  ontology-gap suggestion, as UNCERTAINTY, never as authority.

**It is served as a DOSSIER SECTION, never as its own route.** The review killed the separate
``/context`` design: asset detail assembles every section inside ONE ``REPEATABLE READ``
transaction and fingerprints the result with a ``consistency_token``/ETag, and a second route would
serve a snapshot that no token covers — two views of one column, taken at two instants, presented
as one. :func:`build_context_section` is therefore called from ``asset_detail.build_asset_detail``
and its bytes ride that token.

**Edge honesty (Task 7's own rules).**

* Every inferred/governed edge carries the REAL D2 axes ``(producer, strength, lifecycle)``, its
  ``current`` flag, its backing ``evidence_ids`` and a concise ``why``.
* A STRUCTURAL ``contains`` edge carries ``authority="structural"`` and an EMPTY evidence list —
  containment is a fact about the upload's shape, not an assertion anybody made.
* Relationship edges are the shared :class:`semantic_context.RelationshipContextV1` projection:
  link + per-direction realizations, availability separate from review, review separate from
  safety. **"Executable now" comes ONLY from the revalidating reader**
  (``bridge_store.executable_bridge_realizations``); the pure ``production_eligible`` predicate on
  a realization labels stored history and is rendered as such. A review badge never implies
  executability, and a signed activation interlock is an EXECUTION state — links stay discoverable
  without it.
* Release-C-only identities (``definition_revision_id``/``execution_revision_id``/leg pins) are
  NEVER invented for a bridge: this contract does not carry them at all (D3).

**Absence is spoken, not implied.** Ownership, usage and data-product context have no producer on
this platform, so they are reported as ``not_supplied`` — not zero, not inferred, and never as a
gap in the data. Every bounded read reports what it left out via
:class:`lineage.TruncationReportV1`.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.overlay.upload.feature_metadata_snapshot import CatalogProjectionUnavailable
from featuregen.overlay.upload.lineage import TruncationReportV1, lineage_graph
from featuregen.overlay.upload.semantic_context import (
    RelationshipContextV1,
    SemanticContextBundleV1,
    SemanticValueV1,
    bundle_from_store,
)

CONTEXT_GRAPH_VERSION = "context-graph/v1"

#: The structural basis every ``contains`` edge carries. Named so no caller invents a producer for
#: an edge nobody asserted.
STRUCTURAL_AUTHORITY = "structural"

#: Context this platform has NO producer for. Reported as supplied-by-nobody rather than rendered
#: as zero or inferred from something adjacent (Task 7: "render missing ownership/usage/data-product
#: context as `not supplied`"). Adding a producer for one of these is what removes it from the list
#: — never a UI change that hides the row.
NOT_SUPPLIED_CONTEXT: tuple[str, ...] = ("ownership", "usage", "data_product")

#: Bounds on the section. The dossier is a page, not an export.
MAX_RELATED_COLUMNS = 40
MAX_RELATIONSHIPS = 20
MAX_LINEAGE_NODES = 60

#: The lineage neighbourhood is ONE hop by construction (Task 7: "a bounded one-hop context read").
_LINEAGE_DEPTH = 1
#: Lineage marks nodes stale against a freshness window; the dossier SHOWS stale context (it is a
#: map, not a governed read) so the window is wide and staleness rides the node flag.
_LINEAGE_FRESHNESS = timedelta(days=36500)


@dataclass(frozen=True, slots=True)
class ContextNodeV1:
    """One thing the anchor's context contains. ``detail`` is a bounded, kind-specific dict."""

    id: str
    kind: str
    label: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ContextEdgeV1:
    """One relationship between two context nodes, with its authority stated in full.

    ``authority`` is :data:`STRUCTURAL_AUTHORITY` for containment, otherwise the DERIVED D2 display
    label — display only, never branched on (the ``producer``/``strength``/``lifecycle`` triple
    beside it is the real axis). ``status`` is the surface's own lifecycle word where one exists
    (a link's review status, a realization's safety status); ``None`` when the edge has none, never
    a fabricated "ok"."""

    from_id: str
    to_id: str
    kind: str
    authority: str
    why: str
    status: str | None = None
    producer: str | None = None
    strength: str | None = None
    lifecycle: str | None = None
    current: bool = True
    evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "from": self.from_id, "to": self.to_id, "kind": self.kind,
            "authority": self.authority, "status": self.status, "why": self.why,
            "producer": self.producer, "strength": self.strength, "lifecycle": self.lifecycle,
            "current": self.current, "evidence_ids": list(self.evidence_ids),
        }


def _value_dict(value: SemanticValueV1) -> dict:
    """One semantic value with its FULL authority attribution — the D2 triple of every backing
    record, leading with the strongest, plus the derived display label. The label is published for
    the UI's chip; nothing may branch on it."""
    lead = value.evidence[0] if value.evidence else None
    return {
        "field": value.field_name,
        "value": value.value,
        "resolution_status": value.resolution_status,
        "operational_influence": value.operational_influence,
        "authority_label": value.display_label(),
        "producer": lead.producer if lead else None,
        "strength": lead.strength if lead else None,
        "lifecycle": lead.lifecycle if lead else None,
        "evidence_ids": [e.evidence_id for e in value.evidence if e.evidence_id],
    }


def _executable_realization_ids(conn: DbConn, relationship_ref: str) -> frozenset[str]:
    """The realization revisions that are executable RIGHT NOW, from the revalidating reader.

    D3 is explicit that the pure ``eligible_for_production`` predicate on a stored realization may
    label history and NEVER a live capability: it cannot see a dependency that has since been
    withdrawn. So "executable now" is asked of ``bridge_store.executable_bridge_realizations``,
    which revalidates, and of nothing else. A reader failure is NOT swallowed into a false
    ``True`` — it degrades to "nothing is executable", the honest fail-closed answer.

    Inside a SAVEPOINT, because failing closed is only half the guard: catching the exception does
    not un-abort the transaction a DATABASE fault aborted, and the next statement in the dossier's
    one repeatable-read transaction would then raise ``InFailedSqlTransaction``. Scoping the
    rollback (the ``runtime.dispatch`` idiom) is what makes "nothing is executable" a degraded
    answer rather than the first casualty of a dossier-wide failure."""
    from featuregen.overlay.upload.bridge_store import executable_bridge_realizations

    if relationship_ref.startswith("cwd_"):
        # A CROSSWALK, not a bridge. It has no realization to revalidate and no execution path on
        # this tree at all (Release C Task 10 builds representation + discovery only), so the
        # answer is "nothing", stated rather than arrived at by a bridge lookup that happens to
        # miss. Asking the bridge reader for a crosswalk id would also be a wrong question that
        # silently returns the right answer — the kind of accident that stops being right later.
        return frozenset()
    try:
        with conn.transaction():
            realizations = executable_bridge_realizations(
                conn, purpose="analysis", environment="production",
                bridge_fact_key=relationship_ref)
    except Exception:   # noqa: BLE001 — a revalidation fault must never READ AS executable
        return frozenset()
    return frozenset(r.revision.realization_revision_id for r in realizations)


def _relationship_dict(conn: DbConn, link: RelationshipContextV1) -> dict:
    """One link, projected with availability, review and deterministic safety kept SEPARATE.

    ``executable_now`` is per-realization and comes from the revalidating reader alone;
    ``production_eligible`` stays on each realization as what it is — a statement about the stored
    record. A caller that renders the review badge as permission is contradicted by the payload."""
    executable = _executable_realization_ids(conn, link.relationship_ref)
    crosswalk = None if link.crosswalk is None else {
        "definition_id": link.crosswalk.definition_id,
        "definition_revision_id": link.crosswalk.definition_revision_id,
        "mapping_dataset_ref": link.crosswalk.mapping_dataset_ref,
        # Both legs, always. Rendering one would be the "omit one leg" mutation Task 13 must kill,
        # and a crosswalk shown with a single leg reads exactly like a direct link.
        "source_to_mapping_refs": list(link.crosswalk.source_to_mapping_refs),
        "mapping_to_target_refs": list(link.crosswalk.mapping_to_target_refs),
        "mapping_temporal_policy_revision_id":
            link.crosswalk.mapping_temporal_policy_revision_id,
        # EMPTY at discovery, by contract: a leg is pinned by resolving it (Task 11), and nothing
        # in this release resolves anything.
        "leg_pins": [pin.identity_payload() for pin in link.crosswalk.leg_pins],
    }
    return {
        "relationship_ref": link.relationship_ref,
        "crosswalk": crosswalk,
        "kind": link.kind,
        "left_ref": link.left_ref,
        "right_ref": link.right_ref,
        "availability": link.availability,
        "review_status": link.review_status,
        "assessment_revision_id": link.assessment_revision_id,
        "producer": link.producer,
        "strength": link.strength,
        "lifecycle": link.lifecycle,
        "current": link.current,
        "evidence_ids": list(link.evidence_ids),
        # The ONE live-capability answer on this payload.
        "executable_now": bool(executable),
        "realizations": [
            {
                "realization_revision_id": r.realization_revision_id,
                "from_ref": r.from_ref, "to_ref": r.to_ref,
                "lifecycle": r.lifecycle, "safety_status": r.safety_status,
                # Direction and scope travel WITH the fan-out, never without it.
                "cardinality": r.cardinality, "scope_id": r.scope_id,
                "sandbox_eligible": r.sandbox_eligible,
                # Stored-record eligibility — history, not a live capability (D3).
                "production_eligible": r.production_eligible,
                "executable_now": r.realization_revision_id in executable,
            }
            for r in link.realizations
        ],
    }


def _link_why(link: RelationshipContextV1) -> str:
    """A concise, non-committal explanation of a link — never a claim of executability.

    A crosswalk names its mapping dataset, because "related through this table" is the whole
    difference between it and a direct link, and a reader who cannot see which table cannot tell
    the two apart."""
    review = link.review_status or "unreviewed"
    through = ("" if link.crosswalk is None
               else f" through {link.crosswalk.mapping_dataset_ref}")
    return (f"{link.kind} link between {link.left_ref} and {link.right_ref}{through}; "
            f"availability {link.availability}, review {review}")


def _bundle_nodes_and_edges(
    bundle: SemanticContextBundleV1, anchor_id: str,
) -> tuple[list[ContextNodeV1], list[ContextEdgeV1], Counter[str]]:
    """The semantic half of the graph: concept ancestry, identity axes, related columns, links."""
    nodes: list[ContextNodeV1] = []
    edges: list[ContextEdgeV1] = []
    omitted: Counter[str] = Counter()

    # Concept ancestry — selected concept first, then its is_a ancestors, chained.
    previous = anchor_id
    for depth, concept in enumerate(bundle.concept_path):
        node_id = f"concept:{concept}"
        nodes.append(ContextNodeV1(id=node_id, kind="concept", label=concept,
                                   detail={"depth": depth}))
        edges.append(ContextEdgeV1(
            from_id=previous, to_id=node_id,
            kind="classified_as" if depth == 0 else "is_a",
            authority=STRUCTURAL_AUTHORITY if depth else _concept_authority(bundle),
            why=("the concept selected for this column" if depth == 0
                 else f"{bundle.concept_path[depth - 1]} is a kind of {concept}"),
            evidence_ids=() if depth else _concept_evidence_ids(bundle),
            **({} if depth else _concept_axes(bundle))))
        previous = node_id

    namespace = bundle.identifier_namespace
    if namespace is not None:
        node_id = f"namespace:{namespace.scheme}"
        nodes.append(ContextNodeV1(
            id=node_id, kind="identifier_namespace", label=namespace.scheme,
            detail={"scheme": namespace.scheme, "issuer_scope": namespace.issuer_scope,
                    "basis": namespace.basis}))
        edges.append(ContextEdgeV1(
            from_id=anchor_id, to_id=node_id, kind="identifies_within",
            authority=STRUCTURAL_AUTHORITY,
            status=namespace.basis,
            why=("the value space this identifier belongs to; scheme equality alone is never "
                 "equality proof")))

    for neighbour in bundle.neighbouring_columns[:MAX_RELATED_COLUMNS]:
        node_id = f"column:{neighbour.object_ref}"
        nodes.append(ContextNodeV1(
            id=node_id, kind="related_column", label=neighbour.column_name,
            detail={"object_ref": neighbour.object_ref, "concept": neighbour.concept,
                    "party_role": neighbour.party_role}))
        edges.append(ContextEdgeV1(
            from_id=anchor_id, to_id=node_id, kind="same_table",
            authority=STRUCTURAL_AUTHORITY,
            why="a column of the same table, in the caller's read scope"))
    dropped = len(bundle.neighbouring_columns) - MAX_RELATED_COLUMNS
    if dropped > 0:
        omitted["related_column"] += dropped

    return nodes, edges, omitted


def _concept_lead(bundle: SemanticContextBundleV1) -> SemanticValueV1 | None:
    for value in bundle.resolved_semantics:
        if value.field_name == "concept":
            return value
    return None


def _concept_authority(bundle: SemanticContextBundleV1) -> str:
    lead = _concept_lead(bundle)
    return lead.display_label() if lead is not None else "system"


def _concept_axes(bundle: SemanticContextBundleV1) -> dict:
    lead = _concept_lead(bundle)
    if lead is None or not lead.evidence:
        return {}
    head = lead.evidence[0]
    return {"producer": head.producer, "strength": head.strength, "lifecycle": head.lifecycle,
            "status": lead.resolution_status}


def _concept_evidence_ids(bundle: SemanticContextBundleV1) -> tuple[str, ...]:
    lead = _concept_lead(bundle)
    if lead is None:
        return ()
    return tuple(e.evidence_id for e in lead.evidence if e.evidence_id)


def _lineage_nodes_and_edges(
    conn: DbConn, *, source: str, object_ref: str, roles: Iterable[str], now: datetime,
) -> tuple[list[ContextNodeV1], list[ContextEdgeV1], TruncationReportV1]:
    """The structural neighbourhood, from the EXISTING lineage builder (never a second BFS).

    Containment, joins, cross-catalog bridges, features and consumers arrive already read-scoped
    and already bounded, with the per-kind truncation accounting Task 7 requires."""
    graph = lineage_graph(conn, source, object_ref, now=now, depth=_LINEAGE_DEPTH,
                          roles=roles, fresh_within=_LINEAGE_FRESHNESS,
                          max_nodes=MAX_LINEAGE_NODES)
    if graph is None:   # unreachable from asset detail (the anchor already passed scope)
        return [], [], TruncationReportV1()
    nodes = [
        ContextNodeV1(id=n["id"], kind=f"lineage_{n['kind']}",
                      label=n.get("column") or n.get("name") or n.get("table") or n["id"],
                      detail={k: v for k, v in n.items() if k not in ("id", "kind")})
        for n in graph["nodes"]
    ]
    edges = [
        ContextEdgeV1(
            from_id=e["from"], to_id=e["to"], kind=e["kind"],
            # `contains` is the structural case Task 7 names explicitly: an explicit structural
            # basis and an EMPTY evidence list. Everything else on this layer is a DECLARED or
            # DERIVED edge whose own status fields ride in `status`.
            authority=STRUCTURAL_AUTHORITY if e["kind"] == "contains" else (
                e.get("authority") or e.get("trust_kind") or "declared"),
            status=e.get("approved_join_status") or e.get("link_review_status"),
            why=e.get("why") or _lineage_why(e),
            current=bool(e.get("resolved", True)),
        )
        for e in graph["edges"]
    ]
    report = graph["truncation"]
    # NAMESPACED on the way in. The lineage prune legitimately drops a sibling column that this
    # section still carries in `related_columns` (two different views of the same catalog: a
    # neighbourhood versus a roster), and an unprefixed `column: 1` beside a listed column reads as
    # a contradiction. `lineage_column: 1` says exactly what happened — the MAP left it out.
    return nodes, edges, TruncationReportV1(
        truncated=report["truncated"],
        omitted={f"lineage_{kind}": count for kind, count in report["omitted"].items()})


_LINEAGE_WHY = {
    "contains": "the table this column belongs to",
    "join": "a join declared in the upload",
    "entity_bridge": "a cross-catalog link between two identifier columns",
    "derives": "a feature derived from this column",
    "consumes": "a model consuming this feature",
}


def _lineage_why(edge: dict) -> str:
    return _LINEAGE_WHY.get(edge.get("kind", ""), "a catalog relationship")


def build_context_section(
    conn: DbConn,
    *,
    source: str,
    object_ref: str,
    kind: str,
    logical_ref: str,
    roles: Iterable[str],
    now: datetime,
    dataset_profile: dict | None = None,
    catalog_profile_revision_id: str | None = None,
) -> dict:
    """The dossier's ``context`` section for ONE anchor.

    Called from :func:`asset_detail.build_asset_detail` INSIDE its repeatable-read transaction, so
    every fact here belongs to the same snapshot the ``consistency_token`` fingerprints. The anchor
    has already passed read-scope at that seam; ``roles`` is threaded on so every neighbour, link
    and lineage read is scoped too (D11) rather than inheriting the anchor's clearance.

    ``status`` is one of:

    * ``available`` — a column anchor with an assembled semantic bundle;
    * ``table`` — a TABLE anchor: it has structural context and a dataset profile, but no
      column-grain bundle (the bundle answers "what does this COLUMN mean");
    * ``projection_unavailable`` — the load-bearing catalog projection is lagged or degraded, so
      the bundle would seal a stale view. Reported with the gate's own code, never silently served
      and never raised into the caller's dossier.

    None of those is an error state and none is rendered as one.
    """
    profiles = {
        "catalog_profile_revision_id": catalog_profile_revision_id,
        "dataset_profile_hash": (dataset_profile or {}).get("dataset_profile_hash"),
        "data_role": _profile_field_value(dataset_profile, "data_role"),
        "primary_entity": _profile_field_value(dataset_profile, "primary_entity"),
        "authority_role": _profile_field_value(dataset_profile, "authority_role"),
        "temporal_storage_model": _profile_field_value(dataset_profile, "temporal_storage_model"),
        "missing_context": list((dataset_profile or {}).get("missing_context", ())),
    }
    anchor_id = f"{source}:{object_ref}"
    nodes: list[ContextNodeV1] = [
        ContextNodeV1(id=anchor_id, kind=kind, label=object_ref,
                      detail={"object_ref": object_ref, "logical_ref": logical_ref})]
    edges: list[ContextEdgeV1] = []
    omitted: Counter[str] = Counter()

    lineage_nodes, lineage_edges, lineage_report = _lineage_nodes_and_edges(
        conn, source=source, object_ref=object_ref, roles=roles, now=now)
    omitted.update(lineage_report.omitted)

    profile_nodes, profile_edges = _profile_nodes_and_edges(anchor_id, profiles)

    body: dict = {
        "version": CONTEXT_GRAPH_VERSION,
        "anchor_id": anchor_id,
        "profiles": profiles,
        # Absence with a name (Task 7). Never zero, never inferred.
        "not_supplied": list(NOT_SUPPLIED_CONTEXT),
    }

    if kind != "column":
        nodes.extend(profile_nodes)
        nodes.extend(n for n in lineage_nodes if n.id != anchor_id)
        edges.extend(profile_edges)
        edges.extend(lineage_edges)
        body.update(_finish(status="table", nodes=nodes, edges=edges, omitted=omitted,
                            truncated=lineage_report.truncated))
        body["note"] = ("table asset — structural and profile context only; column meaning is "
                        "assembled per column")
        return body

    try:
        bundle = bundle_from_store(
            conn, source, object_ref, roles=roles,
            catalog_profile_revision_id=catalog_profile_revision_id,
            dataset_profile_hash=profiles["dataset_profile_hash"],
            # What the read could not serve lands in the section's own omission counter — a
            # crosswalk whose stored row failed content verification is OMITTED, and saying so is
            # what keeps it distinguishable from a catalog that never had one.
            omitted=omitted)
    except CatalogProjectionUnavailable as exc:
        # DISCLOSE, never raise into the dossier and never serve a lagged bundle as current: the
        # rest of the section (structural + profile context) is still true of this snapshot.
        nodes.extend(profile_nodes)
        nodes.extend(n for n in lineage_nodes if n.id != anchor_id)
        edges.extend(profile_edges)
        edges.extend(lineage_edges)
        body.update(_finish(status="projection_unavailable", nodes=nodes, edges=edges,
                            omitted=omitted, truncated=lineage_report.truncated))
        body["projection"] = {"code": exc.code, "detail": exc.detail}
        return body

    semantic_nodes, semantic_edges, semantic_omitted = _bundle_nodes_and_edges(bundle, anchor_id)
    omitted.update(semantic_omitted)

    relationships = [_relationship_dict(conn, link)
                     for link in bundle.relationship_context[:MAX_RELATIONSHIPS]]
    dropped_links = len(bundle.relationship_context) - MAX_RELATIONSHIPS
    if dropped_links > 0:
        omitted["relationship"] += dropped_links
    for link in bundle.relationship_context[:MAX_RELATIONSHIPS]:
        node_id = f"link:{link.relationship_ref}"
        semantic_nodes.append(ContextNodeV1(
            id=node_id, kind="relationship", label=link.relationship_ref,
            detail={"kind": link.kind, "availability": link.availability,
                    "review_status": link.review_status,
                    # The mapping dataset is what makes a crosswalk node readable AS a crosswalk;
                    # without it the graph draws two identical-looking edges for two different
                    # kinds of relationship.
                    **({} if link.crosswalk is None
                       else {"mapping_dataset_ref": link.crosswalk.mapping_dataset_ref})}))
        semantic_edges.append(ContextEdgeV1(
            from_id=anchor_id, to_id=node_id,
            kind="crosswalk_link" if link.crosswalk is not None else "identifier_link",
            authority=link.producer, status=link.review_status, why=_link_why(link),
            producer=link.producer, strength=link.strength, lifecycle=link.lifecycle,
            current=link.current, evidence_ids=link.evidence_ids))

    nodes.extend(profile_nodes)
    nodes.extend(semantic_nodes)
    nodes.extend(n for n in lineage_nodes if n.id != anchor_id)
    edges.extend(profile_edges)
    edges.extend(semantic_edges)
    edges.extend(lineage_edges)

    body.update({
        "source_meaning": [_value_dict(v) for v in bundle.source_semantics],
        "resolved_meaning": [_value_dict(v) for v in bundle.resolved_semantics],
        "table_context": [_value_dict(v) for v in bundle.table_context],
        "concept_path": list(bundle.concept_path),
        "identifier_namespace": (
            None if bundle.identifier_namespace is None else {
                "scheme": bundle.identifier_namespace.scheme,
                "issuer_scope": bundle.identifier_namespace.issuer_scope,
                "basis": bundle.identifier_namespace.basis}),
        "related_columns": [
            {"object_ref": n.object_ref, "column": n.column_name, "concept": n.concept,
             "party_role": n.party_role}
            for n in bundle.neighbouring_columns[:MAX_RELATED_COLUMNS]],
        "relationships": relationships,
        # `missing_context` describes what is NOT IN THIS BUNDLE for THIS caller — read-scoped by
        # construction. It is uncertainty, never a data-quality verdict and never a coverage
        # metric (the closed vocabulary's own contract).
        "uncertainty": {"missing_context": list(bundle.missing_context),
                        "not_supplied": list(NOT_SUPPLIED_CONTEXT)},
        "content_hash": bundle.content_hash,
    })
    body.update(_finish(status="available", nodes=nodes, edges=edges, omitted=omitted,
                        truncated=lineage_report.truncated))
    return body


def _finish(*, status: str, nodes: Sequence[ContextNodeV1], edges: Sequence[ContextEdgeV1],
            omitted: Counter[str], truncated: bool) -> dict:
    seen: dict[str, ContextNodeV1] = {}
    for node in nodes:
        seen.setdefault(node.id, node)
    present = set(seen)
    kept_edges = [e for e in edges if e.from_id in present and e.to_id in present]
    # An edge to a node this section does not carry is dropped rather than drawn into nothing —
    # and COUNTED, because a silently dangling relationship is exactly the omission Task 7 forbids.
    for edge in edges:
        if edge.from_id not in present or edge.to_id not in present:
            omitted[edge.kind] += 1
    report = TruncationReportV1(truncated=truncated,
                                omitted={k: v for k, v in omitted.items() if v})
    return {
        "status": status,
        "nodes": [n.as_dict() for n in seen.values()],
        "edges": [e.as_dict() for e in kept_edges],
        "truncation": report.as_dict(),
    }


def _profile_field_value(dataset_profile: dict | None, field_name: str) -> dict | None:
    """One profile field's DISPLAY value + authority, straight from the assembled profile payload.

    Returns ``None`` when the profile is absent or the field has nothing to display — the caller
    renders the profile's own ``state``/family, never a fabricated blank."""
    if not dataset_profile:
        return None
    entry = dataset_profile.get(field_name)
    if not isinstance(entry, dict):
        return None
    display = entry.get("display")
    if display is None:
        return {"value": None, "state": entry.get("state"),
                "unresolved_family": entry.get("unresolved_family")}
    return {"value": display.get("value"), "producer": display.get("producer"),
            "strength": display.get("strength"), "lifecycle": display.get("lifecycle"),
            "state": entry.get("state"), "unresolved_family": entry.get("unresolved_family")}


def _profile_nodes_and_edges(
    anchor_id: str, profiles: dict,
) -> tuple[list[ContextNodeV1], list[ContextEdgeV1]]:
    """The profile nodes Task 7 shares with profile Release-A Task 5 — identified by
    ``catalog_profile_revision_id`` and ``dataset_profile_hash``, and assembled from the SAME
    ``build_dataset_profile`` output feature generation and retrieval consume (never a second
    assembly)."""
    nodes: list[ContextNodeV1] = []
    edges: list[ContextEdgeV1] = []
    dataset_hash = profiles.get("dataset_profile_hash")
    if dataset_hash:
        node_id = f"dataset_profile:{dataset_hash}"
        nodes.append(ContextNodeV1(
            id=node_id, kind="dataset_profile", label=dataset_hash[:12],
            detail={"dataset_profile_hash": dataset_hash,
                    "data_role": profiles.get("data_role"),
                    "primary_entity": profiles.get("primary_entity"),
                    "authority_role": profiles.get("authority_role"),
                    "temporal_storage_model": profiles.get("temporal_storage_model"),
                    "missing_context": profiles.get("missing_context", [])}))
        edges.append(ContextEdgeV1(
            from_id=anchor_id, to_id=node_id, kind="profiled_by",
            authority=STRUCTURAL_AUTHORITY,
            why="the effective dataset profile assembled for this asset's table"))
    revision_id = profiles.get("catalog_profile_revision_id")
    if revision_id:
        node_id = f"catalog_profile:{revision_id}"
        nodes.append(ContextNodeV1(
            id=node_id, kind="catalog_profile", label=revision_id[:16],
            detail={"catalog_profile_revision_id": revision_id}))
        edges.append(ContextEdgeV1(
            from_id=anchor_id, to_id=node_id, kind="described_by",
            authority=STRUCTURAL_AUTHORITY,
            why="the current catalog narrative revision this profile was assembled under"))
    return nodes, edges

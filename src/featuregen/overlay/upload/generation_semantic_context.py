"""SE-2 (part 1) — the frozen generation semantic context: one read, one identity, no re-asks.

A generation run today lets each lens re-query the live catalog for meaning, so two stages of
one run can see two different catalogs and nothing can prove afterwards which state a decision
consumed. This module freezes LAYER A of the plan's two-layer design:

* **Layer A (here):** the read-scoped column UNIVERSE with its structural/semantic index facts
  — the same row shape template grounding has always loaded (`templates._Col`), loaded in ONE
  query — plus the concept index shortlisting searches, the catalog's drift watermark, and the
  registry/policy versions the run decides under. Assembled on the caller's connection (the
  feature-generation path's REPEATABLE READ one), then immutable in memory: every consumer
  reads this object, none re-queries `graph_node` for meaning.
* **Layer B (SE-3):** full `ColumnCapabilityV1` compilation for SHORTLIST members only, lazily,
  inside the same snapshot — bounded by roles × candidates, never by catalog width.

Identity: ``context_hash`` covers the read-scope key, the watermark, the version pins and every
column row (field-exhaustively, via the same canonicalizer as canonical-recipe-v2) — any change
in what a run could see is a different context identity. The durable SEAL of that hash into the
metadata snapshot (before the considered revision is written, preserving the C0 fail-closed
projection-lag abort) is part 2's wiring.

Budget (the rebased SE-2 gates): Layer A is exactly TWO queries — the column load and the
watermark — independent of column count; a query-count test pins it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.overlay.upload.field_resolution import FIELD_POLICY_VERSION
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.recipe_grounding_context import _canonical_dataclass
from featuregen.overlay.upload.taxonomy.versions import CONCEPT_REGISTRY_VERSION

GENERATION_CONTEXT_CONTRACT = "generation-semantic-context"
GENERATION_CONTEXT_VERSION = "1"
_OWNER = "featuregen.overlay.upload.generation_semantic_context"

register_contract_version(GENERATION_CONTEXT_CONTRACT, GENERATION_CONTEXT_VERSION, owner=_OWNER)


@dataclass(frozen=True, slots=True)
class ColumnIndexV1:
    """One Layer-A column row — the structural/semantic index facts shortlisting reads.
    Field-for-field the shape template grounding loads (`templates._Col`), frozen."""

    object_ref: str
    table: str
    column: str
    data_type: str | None
    is_grain: bool
    is_as_of: bool
    concept: str | None
    entity: str | None
    additivity: str | None
    sensitivity: str | None
    currency: str | None
    definition: str | None
    ai_summary: str | None
    semantic_terms: str | None


@dataclass(frozen=True, slots=True)
class GenerationSemanticContextV1:
    """The frozen Layer-A context one generation run reads — assembled once, queried never."""

    catalog_source: str
    read_scope_key: str                   # sorted allowed sensitivities — WHO could see this
    watermark: str                        # the catalog's drift watermark at assembly ("" = none)
    concept_registry_version: str
    field_policy_version: str
    columns: tuple[ColumnIndexV1, ...]
    concept_index: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def context_hash(self) -> str:
        """The run's context identity — field-exhaustive, so ANY visible difference (a column,
        a fact, the watermark, a version pin, the caller's scope) is a different context."""
        return contract_hash_v1(
            GENERATION_CONTEXT_CONTRACT, GENERATION_CONTEXT_VERSION,
            {"context": _canonical_dataclass(self)})


def build_generation_semantic_context(conn, *, catalog_source: str, roles=(),
                                      scope=None) -> GenerationSemanticContextV1:
    """Assemble Layer A on the CALLER's connection (the feature-gen path's REPEATABLE READ one —
    isolation is the connection's property, deliberately not re-established here). Exactly two
    queries, independent of catalog width. ``scope`` (a sensitivity list) overrides the
    roles-derived one — the freshness comparator rebuilds at a STORED scope, not a caller's."""
    scope = list(scope) if scope is not None else allowed_sensitivities(roles)
    rows = conn.execute(
        "SELECT object_ref, table_name, column_name, data_type, is_grain, is_as_of, "
        "       concept, entity, additivity, sensitivity, currency, "
        "       definition, ai_summary, semantic_terms "
        "FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND visible_requires <@ %s "
        "ORDER BY table_name, column_name",
        (catalog_source, scope)).fetchall()
    watermark_row = conn.execute(
        "SELECT last_completed_at FROM overlay_drift_watermark WHERE catalog_source = %s",
        (catalog_source,)).fetchone()

    columns = tuple(
        ColumnIndexV1(object_ref=r[0], table=r[1], column=r[2], data_type=r[3],
                      is_grain=bool(r[4]), is_as_of=bool(r[5]), concept=r[6], entity=r[7],
                      additivity=r[8], sensitivity=r[9], currency=r[10], definition=r[11],
                      ai_summary=r[12], semantic_terms=r[13])
        for r in rows)
    concept_index: dict[str, list[str]] = {}
    for col in columns:
        if col.concept:
            concept_index.setdefault(col.concept, []).append(col.object_ref)

    return GenerationSemanticContextV1(
        catalog_source=catalog_source,
        read_scope_key=",".join(sorted(scope)),
        watermark=str(watermark_row[0]) if watermark_row else "",
        concept_registry_version=CONCEPT_REGISTRY_VERSION,
        field_policy_version=FIELD_POLICY_VERSION,
        columns=columns,
        concept_index={k: tuple(v) for k, v in concept_index.items()})


__all__ = ["ColumnIndexV1", "GENERATION_CONTEXT_ITEM_KIND", "GenerationSemanticContextV1",
           "build_generation_semantic_context", "compare_generation_context_item",
           "context_snapshot_item"]


# ── the durable seal (SE-2 part 2): the context as a metadata-snapshot item ─────────────────────
#
# The frozen context's identity is sealed INTO the C0 metadata snapshot (one additive item kind)
# before the considered revision is written, so a stored run can prove which Layer-A state its
# decisions consumed — and the snapshot freshness check can answer "is that state still true?"
# by REBUILDING the context at the stored scope and comparing hashes: catalog drift since the
# run is an honest SNAPSHOT_ITEM_DRIFT, never a silent recompute.

GENERATION_CONTEXT_ITEM_KIND = "generation_semantic_context"
_CONTEXT_GRAPH_REF_PREFIX = "context:"


def context_snapshot_item(context: GenerationSemanticContextV1):
    """The context's one snapshot item. ``graph_ref`` carries the read-scope key because the
    freshness comparator receives only ``(conn, catalog_source, graph_ref, field)`` and must
    rebuild the SAME scope's context to verify the pin."""
    from featuregen.overlay.upload.feature_metadata_snapshot import (
        SnapshotItem,
        snapshot_item_hash,
    )

    context_hash = context.context_hash()
    item_hash = snapshot_item_hash(GENERATION_CONTEXT_ITEM_KIND, {
        "catalog_source": context.catalog_source,
        "read_scope_key": context.read_scope_key,
        "context_hash": context_hash,
    })
    return SnapshotItem(
        catalog_source=context.catalog_source,
        graph_ref=f"{_CONTEXT_GRAPH_REF_PREFIX}{context.read_scope_key}",
        logical_ref=None, physical_ref=None,
        item_kind=GENERATION_CONTEXT_ITEM_KIND,
        field_or_fact_type="layer_a",
        value=context_hash,
        authority="hint",                 # an identity pin, never an operational value
        provenance=f"{GENERATION_CONTEXT_CONTRACT}@{GENERATION_CONTEXT_VERSION}",
        status="not_operational",
        decision_event_id=None, fact_event_id=None,
        item_hash=item_hash)


def compare_generation_context_item(conn, catalog_source: str, graph_ref: str, field: str):
    """The freshness comparator for the context item kind (D6 dispatch): rebuild Layer A at the
    STORED scope and re-derive the item — a hash mismatch is real catalog drift."""
    del field                                          # one field only: "layer_a"
    scope_key = graph_ref.removeprefix(_CONTEXT_GRAPH_REF_PREFIX)
    scope = [s for s in scope_key.split(",") if s]
    rebuilt = build_generation_semantic_context(
        conn, catalog_source=catalog_source, scope=scope)
    return context_snapshot_item(rebuilt)

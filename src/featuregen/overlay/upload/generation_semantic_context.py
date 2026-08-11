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


def build_generation_semantic_context(conn, *, catalog_source: str,
                                      roles=()) -> GenerationSemanticContextV1:
    """Assemble Layer A on the CALLER's connection (the feature-gen path's REPEATABLE READ one —
    isolation is the connection's property, deliberately not re-established here). Exactly two
    queries, independent of catalog width."""
    scope = allowed_sensitivities(roles)
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


__all__ = ["ColumnIndexV1", "GenerationSemanticContextV1",
           "build_generation_semantic_context"]

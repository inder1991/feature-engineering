"""Phase 3C.2b-i-B · Task 7 — the governed SOURCE-SIDE structural binding.

A governed cross-catalog operand needs a *source-side* structural authority: the source table's
grain (its composite key columns) + the deterministic ``fact_key`` of the CURRENT VERIFIED grain
fact (the one A's ``governed_endpoint`` independently revalidates) + the **source grain entity**,
DERIVED from the grain-key columns' confirmed concepts. The spike (``b_slice_spike.py``) prototyped
the grain-key/fact_key half but **hard-injected** ``source_grain_entity``; this task derives the
entity itself from governed concepts, which introduces the one genuinely new seam below.

Every authority here is read from the REAL governed state, never manufactured:
  * the grain (its key columns) comes ONLY from the VERIFIED grain fact via ``resolve_fact`` —
    never from ``graph_node.is_grain`` / any advisory file flag;
  * the deterministic ``grain_fact_key`` comes from ``overlay.identity.fact_key`` over the SAME
    normalized ``table_ref`` A recomputes, so it matches byte-for-byte on the GO path;
  * each grain-key column's concept comes from T5's ``resolve_planner_concept_binding`` (the
    human-confirmed / source-attested cohort), and the entity from that concept's registry
    ``entity_link`` — never from a caller claim.

THE TWO KEY FORMS (the crux — read before touching the queries below)
--------------------------------------------------------------------
The grain fact, its ``key_refs``, and ``grain_fact_key`` use the **flattened** ``public.<table>.
<column>`` form (schema hardcoded to ``public``), because that is EXACTLY what A's
``governed_endpoint`` recomputes and compares — ``grain_fact_key`` must match byte-for-byte on the
GO path, and the graph writer (``overlay.upload.graph._column_ref``) always collapses the schema to
``public`` when it writes ``graph_node.object_ref``.

But resolving each grain-key column's **concept** needs the **schema-preserving** ``logical_ref`` =
``source::<real_schema>.<table>.<column>``, because concept field-evidence is keyed on the real
declared schema (for real FTR data that is ``DPL_EIB_COMPLIANCE``, NOT ``public``). The real schema
lives in ``graph_node.schema_name`` (nullable; ``NULL`` ⇒ fall back to ``"public"``, matching
``normalize_ref``'s own default). We look it up PER COLUMN and build the ``logical_ref`` with
``normalize_ref(catalog_source, schema_or_public, table, column)``. Feeding the ``public``-flattened
ref to the concept resolver would MISS the evidence — the whole point of this seam.

DISPOSITION FOLD (pinned — the design's coarse two-bucket collapse of GRAIN_UNRESOLVED)
--------------------------------------------------------------------------------------
The fine :class:`SourceBindingReason` folds onto the coarse ``BDisposition`` vocabulary as:

    no_verified_grain_fact  -> BDisposition.structural_need_ungoverned
    grain_columns_absent    -> BDisposition.structural_need_ungoverned
    source_entity_missing   -> BDisposition.source_entity_ungoverned
    source_entity_conflict  -> BDisposition.source_entity_ungoverned

The judgment call, pinned here: the STRUCTURE bucket vs the ENTITY bucket.
  * "no VERIFIED grain fact at all" (or a VERIFIED grain with no columns, or a grain naming a column
    the graph does not carry) means the source has no usable *structure* to bind ⇒
    ``structural_need_ungoverned``.
  * "the grain exists, but its keys do not resolve a SINGLE governed entity" — whether NONE of the
    keys carry a governed entity-link (``source_entity_missing``) OR the keys carry DISTINCT
    entity-links that cannot be folded to one source grain entity (``source_entity_conflict``) —
    means the structure is present but the *entity* is ungoverned ⇒ ``source_entity_ungoverned``.
    Conflicting entity-links deliberately fold to the SAME bucket as missing ones: from the
    consumer's view "cannot establish one governed source grain entity" is one outcome; the fine
    reason is retained on :class:`SourceBindingRejection` for telemetry.

BDisposition has NO ``grain_unresolved`` member by design — GRAIN_UNRESOLVED is exactly this
two-bucket collapse, so there is nothing finer to map onto.

Shadow-only; NO data plane. Read-only over the grain / concept / graph stores. Frozen slotted
dataclasses + a lowercase ``StrEnum``; no pydantic. A is UNCHANGED.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from featuregen.contracts import DbConn
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.identity import fact_key
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.b_concept_authority import (
    PlannerConceptBinding,
    resolve_planner_concept_binding,
)
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.multisource_contracts import GovernedSourceBindingV1
from featuregen.overlay.upload.upload_catalog import table_ref

SOURCE_BINDING_POLICY_VERSION = "3c2bib.srcbind.1.0.0"

# The flattened schema every graph node / grain key ref is scoped to (mirrors
# ``overlay.upload.upload_catalog._SCHEMA`` / ``graph._SCHEMA``). The operand's ``object_ref`` and
# the grain key refs are ``<_SCHEMA>.<table>.<column>``.
_SCHEMA = "public"


class SourceBindingReason(StrEnum):
    """The FINE rejection reason kept on :class:`SourceBindingRejection` for telemetry (§10). The
    coarse fold onto :class:`BDisposition` is :func:`reason_to_b_disposition` — see the module
    docstring for the pinned structure-vs-entity judgment call."""

    no_verified_grain_fact = "no_verified_grain_fact"
    grain_columns_absent = "grain_columns_absent"
    source_entity_missing = "source_entity_missing"
    source_entity_conflict = "source_entity_conflict"


@dataclass(frozen=True, slots=True)
class SourceBindingRejection:
    """A fail-closed non-binding, carrying the FINE reason for telemetry."""

    reason: SourceBindingReason


# The coarse fold onto ``BDisposition`` (pinned in the module docstring). A two-into-two collapse:
# structure-absent reasons -> structural_need_ungoverned; entity-unresolvable reasons (missing OR
# conflicting) -> source_entity_ungoverned.
_REASON_TO_B_DISPOSITION: dict[SourceBindingReason, BDisposition] = {
    SourceBindingReason.no_verified_grain_fact: BDisposition.structural_need_ungoverned,
    SourceBindingReason.grain_columns_absent: BDisposition.structural_need_ungoverned,
    SourceBindingReason.source_entity_missing: BDisposition.source_entity_ungoverned,
    SourceBindingReason.source_entity_conflict: BDisposition.source_entity_ungoverned,
}


def reason_to_b_disposition(reason: SourceBindingReason) -> BDisposition:
    """Fold the FINE :class:`SourceBindingReason` onto the coarse :class:`BDisposition`.

    ``no_verified_grain_fact`` / ``grain_columns_absent`` -> ``structural_need_ungoverned`` (the
    source has no usable structure to bind); ``source_entity_missing`` / ``source_entity_conflict``
    -> ``source_entity_ungoverned`` (structure present, but its keys do not resolve ONE governed
    entity). The fine reason stays on :class:`SourceBindingRejection`, so the fold loses nothing."""
    return _REASON_TO_B_DISPOSITION[reason]


# ── internals ─────────────────────────────────────────────────────────────────────────────────


def _table_from_object_ref(object_ref: str) -> str:
    """The table segment of a flattened operand ``object_ref`` (``public.<table>.<column>``).

    ``object_ref`` is a SYSTEM-produced flattened graph ref (the graph writer always renders
    ``public.<table>.<column>``), never user input, so a ref that is NOT that shape is a caller
    contract violation — raised as :class:`ValueError` rather than masked as a governance outcome
    (mislabelling a programming bug as a fail-closed disposition is exactly the anti-pattern the
    neighbouring resolvers avoid)."""
    parts = object_ref.split(".")
    if len(parts) != 3 or parts[0] != _SCHEMA or not parts[1] or not parts[2]:
        raise ValueError(
            f"operand object_ref is not a flattened {_SCHEMA}.<table>.<column> ref: {object_ref!r}"
        )
    return parts[1]


def _column_exists(conn: DbConn, catalog_source: str, table: str, column: str) -> bool:
    """Whether ``graph_node`` carries this column for the table (flattened ``public.<table>.<col>``
    key — the form the graph writer persists)."""
    row = conn.execute(
        "SELECT 1 FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (catalog_source, f"{_SCHEMA}.{table}.{column}"),
    ).fetchone()
    return row is not None


def _declared_schema(conn: DbConn, catalog_source: str, table: str, column: str) -> str:
    """The REAL declared schema for a column, from ``graph_node.schema_name`` (flattened key), with
    ``NULL``/blank falling back to ``"public"`` — matching ``normalize_ref``'s own default. This is
    the schema the concept field-evidence is keyed on (``DPL_EIB_COMPLIANCE`` for real FTR data)."""
    row = conn.execute(
        "SELECT schema_name FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (catalog_source, f"{_SCHEMA}.{table}.{column}"),
    ).fetchone()
    schema = row[0] if row is not None else None
    return schema or _SCHEMA


def _entity_link_of(conn: DbConn, catalog_source: str, table: str, column: str) -> str | None:
    """The governed entity a grain-key column links, or ``None`` when it contributes no entity-link.

    Resolves the column's concept via T5 over the SCHEMA-PRESERVING ``logical_ref`` (recovered from
    ``graph_node.schema_name``), then reads that concept's registry ``entity_link``. A
    ``ConceptRejection`` (ungoverned key), a concept absent from the registry, or a concept with no
    ``entity_link`` all contribute ``None`` — an ungoverned or non-identifying key cannot establish
    the source grain entity."""
    schema = _declared_schema(conn, catalog_source, table, column)
    logical_ref = normalize_ref(catalog_source, schema, table, column)
    binding = resolve_planner_concept_binding(conn, logical_ref)
    if not isinstance(binding, PlannerConceptBinding):
        return None
    record = concept(binding.authoritative_concept)
    if record is None:
        return None
    return record.entity_link


# ── the resolver ────────────────────────────────────────────────────────────────────────────────


def resolve_source_binding(
    conn: DbConn,
    adapter: CatalogAdapter,
    *,
    catalog_source: str,
    object_ref: str,
    now: datetime,
) -> GovernedSourceBindingV1 | SourceBindingRejection:
    """Resolve the governed source-side structural binding for an operand column.

    ``object_ref`` is the operand column's FLATTENED graph ref (``public.<table>.<column>``); the
    table is derived from it. Ordered, fail-closed (design §2):

      1. Derive the table from ``object_ref``.
      2. ``resolve_fact`` for the table's VERIFIED ``grain`` (VERIFIED-only; ``value is None`` for
         any other state) → ``no_verified_grain_fact`` when absent.
      3. The grain's ``columns`` (a VERIFIED grain with no columns is unusable) →
         ``no_verified_grain_fact`` when empty.
      4. Membership (defense-in-depth, mirroring A's ``governed_endpoint``): every grain-key column
         must exist in ``graph_node`` → ``grain_columns_absent`` when any is missing.
      5. Derive ``source_grain_entity`` from the grain-key columns' governed concepts (each via T5
         over the SCHEMA-PRESERVING ``logical_ref``): zero entity-links → ``source_entity_missing``;
         one → that entity (composite grain is fine — the remaining non-entity-linked keys are
         partition keys and stay in ``source_grain_key_refs``); more than one DISTINCT entity →
         ``source_entity_conflict``.
      6. Build :class:`GovernedSourceBindingV1` with the FLATTENED key refs (matching A) and the
         deterministic ``grain_fact_key`` A revalidates.
    """
    table = _table_from_object_ref(object_ref)
    ref = table_ref(catalog_source, table)

    grain = resolve_fact(conn, adapter, ref, "grain", now=now)
    # resolve_fact serves VERIFIED-only: ``value`` is None for any other state, and a VERIFIED
    # grain's value is the validated ``{"columns": [...], "is_unique": bool}`` mapping.
    value = grain.value
    if not isinstance(value, Mapping):
        return SourceBindingRejection(SourceBindingReason.no_verified_grain_fact)
    columns = value.get("columns")
    if not isinstance(columns, list) or not columns:
        # a VERIFIED grain with no usable columns is not a usable grain.
        return SourceBindingRejection(SourceBindingReason.no_verified_grain_fact)

    # (4) membership — every grain-key column present in the graph, before any entity derivation.
    for col in columns:
        if not _column_exists(conn, catalog_source, table, col):
            return SourceBindingRejection(SourceBindingReason.grain_columns_absent)

    # (5) derive the ONE governed source grain entity from the keys' confirmed concepts.
    entity_links = {
        link
        for col in columns
        if (link := _entity_link_of(conn, catalog_source, table, col)) is not None
    }
    if not entity_links:
        return SourceBindingRejection(SourceBindingReason.source_entity_missing)
    if len(entity_links) > 1:
        return SourceBindingRejection(SourceBindingReason.source_entity_conflict)

    (source_grain_entity,) = entity_links
    return GovernedSourceBindingV1(
        source_grain_entity=source_grain_entity,
        source_grain_key_refs=tuple(f"{_SCHEMA}.{table}.{col}" for col in columns),
        grain_fact_key=fact_key(ref, "grain"),
    )

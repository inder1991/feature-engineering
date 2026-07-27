"""Spec §3.3 / §3.5 — the STATIC half of a physical input, and the physical resolution it rests on.

**This module is generation-time only.** A run's business date is deliberately unreachable from it.
Resolving concrete partitions here would put a run observation into ``ir_hash`` and
``CompilationIdentity``, so the generated project would change every business date — and a project
that changes daily is not a hash-identified artifact, it is a daily rebuild wearing one's name.
So the requirement records partition COLUMNS and the DECLARED mapping; the exact partitions a run
reads are a ``PhysicalInputSnapshot``, produced at run preparation (§3.3) against a live metastore.

**Physical schema is RESOLVED, never parsed.** ``build_graph`` flattens every ``object_ref`` to
``public.<table>[.<column>]`` (verified interfaces §17), so the schema segment of a logical ref
carries no cluster meaning at all: ``hdfc::public.transactions.txn_amt`` may perfectly well name a
table that lives in ``banking``. The real declared schema survives only in ``graph_node.schema_name``,
which is NULLABLE. Resolution therefore runs §3.5's three steps in order —

1. the governed catalog's ``schema_name`` (the ``column_authority.logical_ref_of`` pattern: a
   ``graph_node`` lookup, not a string parse);
2. the environment's DECLARED logical-to-physical mapping, when the catalog attests nothing;
3. otherwise ``PHYSICAL_SCHEMA_NOT_RESOLVED``.

— and step 3 is the load-bearing one. ``logical_ref_of`` falls back to ``"public"`` when
``schema_name`` is NULL, which is exactly right for its job (rebuilding the key that evidence is
stored under) and exactly wrong for this one: ``public.transactions`` on the cluster is a DIFFERENT
TABLE from ``banking.transactions``, and reading it produces plausible numbers rather than an error.
That is why the pattern is reused and the fallback is not.

**Case.** ``build_graph`` writes ``table_name``/``schema_name`` in the upload's own casing while a
canonical ``logical_ref`` is case-folded, and the shipped governed readers disagree about which one
addresses a node (``column_authority._scalar`` folds, ``entity.effective_entity`` matches exactly).
Every lookup here folds, and the resolved identity is folded too — unquoted SQL identifiers fold, so
``Banking`` and ``banking`` are one schema, and a resolved identity that carried an upload's casing
would make ``layout_fingerprint`` depend on how somebody typed a CSV header.

**What is refused, and what is a caller error.** Governed verdicts use §14 codes and are RETURNED;
a malformed ref or a column ref where a table ref belongs is a call assembled wrongly, which the
closed vocabulary has no member for, so it raises ``ValueError`` — the distinction
``joins.plan_join`` draws for a cross-catalog identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.contracts.db import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    PartitionMappingKind,
    PartitionMappingV1,
    TableLayout,
)
from featuregen.materialize.joins import PhysicalIdentity
from featuregen.overlay.catalog_changes import drift_head_seq
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.planner.contracts import (
    CatalogOmissionReason,
    CatalogStateStampKind,
)

__all__ = ["PhysicalInputRequirement", "derive_requirement", "resolve_physical_identity"]


@dataclass(frozen=True, slots=True)
class PhysicalInputRequirement:
    """What a compilation needs to be true of one physical table — and NOTHING about a given run.

    The field set is closed and matches spec §3.3. ``captured_at``, refresh time, warehouse location
    and any other OBSERVATION provenance are deliberately absent: re-capturing identical metadata
    must not change ``ir_hash``, or an operator re-running an inventory scan would invalidate every
    compiled artifact without changing a single fact.

    ``layout_fingerprint`` summarizes the SEMANTIC layout (partition columns+types, physical column
    types, the declared mapping). ``catalog_state_stamp`` records WHICH catalog state the governed
    reads above were decided against, as ordered ``(name, value)`` pairs.
    """

    catalog_source: str
    schema: str
    table: str
    partition_columns: tuple[tuple[str, str], ...] | None
    partition_mapping: PartitionMappingV1 | None
    layout_fingerprint: str
    catalog_state_stamp: tuple[tuple[str, str], ...]

    def identity_payload(self) -> dict[str, Any]:
        """Every field, because every field is identity — this type holds no provenance at all.

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a hash, where an exception would be indistinguishable from a governed refusal.
        """
        return {
            "catalog_source": self.catalog_source,
            "schema": self.schema,
            "table": self.table,
            "partition_columns": (None if self.partition_columns is None
                                  else [[c, t] for c, t in self.partition_columns]),
            "partition_mapping": (None if self.partition_mapping is None
                                  else self.partition_mapping.identity_payload()),
            "layout_fingerprint": self.layout_fingerprint,
            "catalog_state_stamp": [[k, v] for k, v in self.catalog_state_stamp],
        }

    def requirement_id(self) -> str:
        """The stable id run preparation keys snapshots by (§3.3), derived — never stored.

        Derived so it cannot disagree with the requirement it names: a stored id would be one more
        field to keep in step, and the field that drifts is the one nobody notices.
        """
        return materialize_hash(self.identity_payload())


def _fold(identifier: str) -> str:
    """Case-fold an SQL identifier (``object_ref._norm``'s fold)."""
    return identifier.strip().lower()


def _refuse(code: CompilationRefusalCode, detail: str) -> MaterializationRefused:
    return MaterializationRefused(code, detail)


def _catalog_schemas(conn: DbConn, catalog_source: str, table: str) -> list[str]:
    """Distinct folded physical schemas the CATALOG attests for one bare table name.

    ``schema_name`` NULL or blank contributes nothing: it is unknown, not a rival candidate — the
    same reading ``joins._schema_candidates`` takes, so the two modules cannot disagree about what
    the catalog said.
    """
    rows = conn.execute(
        "SELECT DISTINCT lower(schema_name) FROM graph_node "
        "WHERE catalog_source = %s AND lower(table_name) = %s "
        "  AND schema_name IS NOT NULL AND schema_name <> '' "
        "ORDER BY 1",
        (catalog_source, table)).fetchall()
    return [row[0] for row in rows]


def resolve_physical_identity(
    conn: DbConn, inventory: ClusterInventoryV1, *, logical_ref: str
) -> PhysicalIdentity | MaterializationRefused:
    """Resolve a governed logical ref onto the table it names ON THE CLUSTER (spec §3.5).

    Accepts a column ref or a table ref — both name one table, and asking the same question two ways
    would be two chances to answer it differently.

    Raises:
        ValueError: ``logical_ref`` is not a normalized logical ref (a caller error, not a governed
            verdict; §14 has no member for it).
    """
    source, ref_schema, table, _column = parse_ref(logical_ref)
    source, table = _fold(source), _fold(table)
    table_ref = f"{source}::{_fold(ref_schema)}.{table}"

    attested = _catalog_schemas(conn, source, table)
    declared = inventory.declared_schema_for(table_ref)
    declared_fold = _fold(declared) if declared and declared.strip() else None

    if len(attested) > 1:
        return _refuse(
            CompilationRefusalCode.AMBIGUOUS_TABLE_NAME,
            f"the governed catalog attests {len(attested)} physical schemas for table {table!r} on "
            f"{source} ({', '.join(attested)}): exactly one of them holds the rows this ref "
            f"governs, and picking any of them reads a table nobody approved")

    if attested:
        physical = attested[0]
        if declared_fold is not None and declared_fold != physical:
            return _refuse(
                CompilationRefusalCode.AMBIGUOUS_TABLE_NAME,
                f"the governed catalog attests schema {physical!r} for {table_ref} while the "
                f"{inventory.environment_id!r} inventory declares {declared_fold!r}: the two "
                f"describe different physical tables, and preferring either one silently discards a "
                f"stated belief about which table the numbers come from")
        return PhysicalIdentity(catalog_source=source, schema=physical, table=table)

    if declared_fold is not None:
        return PhysicalIdentity(catalog_source=source, schema=declared_fold, table=table)

    return _refuse(
        CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED,
        f"no physical schema for {table_ref}: graph_node.schema_name is NULL for every node of "
        f"{table!r} (a schema-less upload attests none) and the {inventory.environment_id!r} "
        f"inventory declares no mapping for it. The ref's own schema segment is catalog-side and "
        f"schema-flattened, so parsing it — or defaulting to it — would read a different table than "
        f"the catalog governs")


def _catalog_state_stamp(conn: DbConn, catalog_source: str) -> tuple[tuple[str, str], ...]:
    """WHICH catalog state this requirement was derived against, as ordered ``(name, value)`` pairs.

    ``head_seq`` is the monotone global event sequence the catalog's last drift scan advanced to —
    identity-bearing, because a moved catalog can yield a different governed answer. The
    watermark's ``last_completed_at`` is deliberately NOT included: it is when a scan happened to
    run, so including it would change every compiled identity each time the projection re-ran over
    an unchanged catalog, which is the same defect as putting an inventory's capture time in.

    No watermark at all is recorded honestly rather than refused. §14's vocabulary has no member for
    "catalog state unknown", and minting one here would fork a closed enum; the stamp says so
    instead, and because the stamp is identity-bearing a later compile against a stamped catalog is
    visibly a different artifact. Freshness itself is enforced where it can be — the governed
    readers fail closed on a degraded projection (GATE 3), and L1 re-checks at run preparation.
    """
    head_seq = drift_head_seq(conn, catalog_source)
    if head_seq is None:
        return (("stamp_kind", CatalogOmissionReason.no_usable_state_stamp.value),)
    return (("stamp_kind", CatalogStateStampKind.drift_watermark.value),
            ("head_seq", str(head_seq)))


def _refuse_incoherent_layout(
    identity: PhysicalIdentity, layout: TableLayout
) -> MaterializationRefused | None:
    """Contradictions inside the DECLARATION — provable with no catalog and no cluster.

    Every one of these would otherwise surface as an empty or over-wide partition set at run time,
    i.e. as a number rather than an error, so they are refused where they are cheap to see.
    """
    if (_fold(layout.schema), _fold(layout.table)) != (_fold(identity.schema),
                                                       _fold(identity.table)):
        return _refuse(
            CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
            f"the inventory entry found under {identity.schema}.{identity.table} describes "
            f"{layout.schema}.{layout.table}: the layout of one table cannot stand in for another, "
            f"and a requirement built from it would name a table it never described")

    mapping = layout.partition_mapping
    if mapping is None:
        return _refuse(
            CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED,
            f"{identity.schema}.{identity.table} declares no partition mapping. A partition column "
            f"name does not say how a time window maps onto it — late-arriving rows sit in "
            f"partitions outside the event range — so the mapping is declared, and inferring one "
            f"here would drop those rows with no error anywhere")

    if layout.partition_columns is None:
        if mapping.kind is not PartitionMappingKind.VERIFIED_UNPARTITIONED:
            return _refuse(
                CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED,
                f"{identity.schema}.{identity.table} is declared VERIFIED unpartitioned but carries "
                f"a {mapping.kind.value} mapping: one of the two declarations is stale, and a "
                f"mapping over partitions that do not exist selects nothing")
        return None

    if mapping.kind is PartitionMappingKind.VERIFIED_UNPARTITIONED:
        return _refuse(
            CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED,
            f"{identity.schema}.{identity.table} declares "
            f"{len(layout.partition_columns)} partition column(s) but a "
            f"{mapping.kind.value} mapping: the mapping would read the table as if the partitions "
            f"were not there")

    if not layout.partition_columns:
        return _refuse(
            CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
            f"{identity.schema}.{identity.table} declares an EMPTY partition column list. `None` "
            f"means the capture verified there are none; empty means nobody said — and 'we do not "
            f"know how this table is partitioned' is not 'scan it'")

    declared = {_fold(column) for column, _type in layout.partition_columns}
    named = [column for column in mapping.partition_columns_named() if _fold(column) not in declared]
    if named:
        return _refuse(
            CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED,
            f"the {mapping.kind.value} mapping for {identity.schema}.{identity.table} names "
            f"{', '.join(sorted(named))}, which the table does not partition by (it partitions by "
            f"{', '.join(column for column, _type in layout.partition_columns)}): the mapping "
            f"describes a table other than the one it is attached to")
    return None


def derive_requirement(
    conn: DbConn, inventory: ClusterInventoryV1, *, table_ref: str
) -> PhysicalInputRequirement | MaterializationRefused:
    """The STATIC requirement for one physical table — stable across every run (spec §3.3).

    Checks run in this order, and the FIRST failure decides the code:

    1. physical resolution (§3.5) — until the table is known, nothing else is answerable;
    2. the inventory captured it at all — absent is ``PARTITION_IDENTITY_UNKNOWN``, never
       "unpartitioned";
    3. the declaration is internally coherent (§3.4).

    A refusal is RETURNED, not raised: a refused input is one governed verdict among the many a
    compilation collects.

    Raises:
        ValueError: ``table_ref`` is not a normalized TABLE ref. A requirement is per table, and
            accepting a column ref would derive the same requirement once per column while inviting
            a per-column reading of ``layout_fingerprint``.
    """
    _source, _schema, _table, column = parse_ref(table_ref)
    if column is not None:
        raise ValueError(
            f"derive_requirement takes a TABLE ref, got the column ref {table_ref!r}: a physical "
            f"input requirement describes a table, and one table has exactly one of them")

    identity = resolve_physical_identity(conn, inventory, logical_ref=table_ref)
    if isinstance(identity, MaterializationRefused):
        return identity

    layout = inventory.layout_for(identity.schema, identity.table)
    if layout is None:
        return _refuse(
            CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN,
            f"the {inventory.environment_id!r} inventory has no entry for "
            f"{identity.schema}.{identity.table}: a table nobody captured is UNKNOWN, not "
            f"unpartitioned, and treating the two alike would let an unexamined table be read whole")

    incoherent = _refuse_incoherent_layout(identity, layout)
    if incoherent is not None:
        return incoherent

    return PhysicalInputRequirement(
        catalog_source=identity.catalog_source,
        schema=identity.schema,
        table=identity.table,
        partition_columns=layout.partition_columns,
        partition_mapping=layout.partition_mapping,
        layout_fingerprint=materialize_hash(layout.semantic_payload()),
        catalog_state_stamp=_catalog_state_stamp(conn, identity.catalog_source),
    )

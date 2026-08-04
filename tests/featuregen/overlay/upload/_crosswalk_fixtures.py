"""The CIB/FTR crosswalk fixture: a real mapping table, a real catalog, real identifier concepts.

ONE fixture for the store, discovery and visibility suites — three hand-rolled copies of a fixture
this load-bearing is how two suites end up asserting against two different banks and both passing
(the `_bridge_fixtures` lesson, applied before it happens again).

FIXTURES ONLY. No engine, no LLM, no cluster: `build_graph` writes into the suite's own rolled-back
Postgres transaction.
"""
from __future__ import annotations

from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    KeyMemberRole,
    TupleKeyRole,
    TypeBasis,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.crosswalk import (
    CrosswalkDefinitionRevisionV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

#: (column, concept, sensitivity). The mapping table is the crosswalk's mechanism: it carries BOTH
#: identifier namespaces (`cif` via customer_id, `external_account` via external_account_ref is the
#: decoy in the discovery suite), which is exactly the §4-correction-10 shape — role PLUS identifier
#: structure proposes a candidate; the role alone proposes nothing.
CIB_COLUMNS = (("cust_num", "customer_id", ""), ("cust_name", "party_name", ""))
FTR_COLUMNS = (("cif_id", "customer_id", ""), ("party_lei", "lei", ""))
MAP_COLUMNS = (("cust_num", "customer_id", ""), ("cif_id", "customer_id", ""),
               ("valid_from", "effective_from", ""))

CIB_TABLE, FTR_TABLE, MAP_TABLE = "bo_cib_customer", "party_master", "cust_cif_xref"

CIB = normalize_ref("cib", "public", CIB_TABLE)
FTR = normalize_ref("ftr", "public", FTR_TABLE)
MAP = normalize_ref("cib", "public", MAP_TABLE)
CIB_KEY = normalize_ref("cib", "public", CIB_TABLE, "cust_num")
FTR_KEY = normalize_ref("ftr", "public", FTR_TABLE, "cif_id")
MAP_CIB = normalize_ref("cib", "public", MAP_TABLE, "cust_num")
MAP_FTR = normalize_ref("cib", "public", MAP_TABLE, "cif_id")


def load_table(db, source: str, table: str, columns, *, table_role: str | None = None,
               declared: str = "varchar(64)") -> None:
    """Build one table's graph nodes with concepts, sensitivities and an optional table role."""
    rows = [CanonicalRow(source, table, name, "unknown", sensitivity=sensitivity)
            for name, _concept, sensitivity in columns]
    build_graph(
        db, source, rows,
        concepts={content_hash(row): concept_name
                  for row, (_n, concept_name, _s) in zip(rows, columns, strict=True)},
        declared_types={f"public.{table}.{name}": declared for name, _c, _s in columns})
    if table_role is not None:
        db.execute(
            "UPDATE graph_node SET table_role = %s WHERE catalog_source = %s AND kind = 'table' "
            "AND object_ref = %s", (table_role, source, f"public.{table}"))


def build_catalog(db, *, map_table_role: str | None = "bridge",
                  cib_columns=CIB_COLUMNS, ftr_columns=FTR_COLUMNS,
                  map_columns=MAP_COLUMNS) -> None:
    """CIB (customers + the mapping table) and FTR (the party master), as one bank sees them."""
    load_table(db, "ftr", FTR_TABLE, ftr_columns)
    # One `build_graph` call per SOURCE rebuilds that source's nodes, so both CIB tables go in one.
    rows, concepts, declared = [], {}, {}
    for table, columns in ((CIB_TABLE, cib_columns), (MAP_TABLE, map_columns)):
        for name, concept_name, sensitivity in columns:
            row = CanonicalRow("cib", table, name, "unknown", sensitivity=sensitivity)
            rows.append(row)
            concepts[content_hash(row)] = concept_name
            declared[f"public.{table}.{name}"] = "varchar(64)"
    build_graph(db, "cib", rows, concepts=concepts, declared_types=declared)
    if map_table_role is not None:
        db.execute(
            "UPDATE graph_node SET table_role = %s WHERE catalog_source = 'cib' "
            "AND kind = 'table' AND object_ref = %s", (map_table_role, f"public.{MAP_TABLE}"))


def endpoint(source: str, table: str, column: str, *, concept: str = "customer_id",
             entity: str = "customer") -> IdentifierEndpointV1:
    """A LOGICAL, UNBOUND identifier endpoint — a crosswalk definition accepts nothing else."""
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=(IdentifierColumnMemberV1(
            normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED,
            KeyMemberRole.PRIMARY),),
        entity_id=entity, concept=concept, concept_authority=ConceptAuthority.DETERMINISTIC,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY)


def wide_endpoint(source: str, table: str, columns: tuple[str, ...]) -> IdentifierEndpointV1:
    """A composite tuple key — legal in the contract, which is why storage bounds live in the store."""
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(
            IdentifierColumnMemberV1(
                normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED,
                KeyMemberRole.PRIMARY if index == 0 else KeyMemberRole.PARTITION)
            for index, column in enumerate(columns)),
        entity_id="customer", concept="customer_id",
        concept_authority=ConceptAuthority.DETERMINISTIC,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY)


def definition(**over) -> CrosswalkDefinitionRevisionV1:
    """The CIB customer number <-> FTR CIF crosswalk through `cust_cif_xref`."""
    kwargs: dict = dict(
        source_endpoint=endpoint("cib", CIB_TABLE, "cust_num"),
        mapping_dataset_ref=MAP,
        source_to_mapping_pairs=(LogicalMappingPairV1(CIB_KEY, MAP_CIB),),
        mapping_to_target_pairs=(LogicalMappingPairV1(FTR_KEY, MAP_FTR),),
        target_endpoint=endpoint("ftr", FTR_TABLE, "cif_id"),
    )
    kwargs.update(over)
    return CrosswalkDefinitionRevisionV1(**kwargs)

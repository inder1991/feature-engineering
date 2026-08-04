"""The CIB/FTR crosswalk fixture: a real mapping table, a real catalog, real identifier concepts.

ONE fixture for the store, discovery and visibility suites — three hand-rolled copies of a fixture
this load-bearing is how two suites end up asserting against two different banks and both passing
(the `_bridge_fixtures` lesson, applied before it happens again).

**Why the fixture is accounts, not "customer number maps to CIF".** The plan's headline example is a
customer number mapping to a CIF. Under the SHIPPED concept registry both sides of that pair are the
`customer_id` concept in the `cif` namespace — one namespace, which makes it a DIRECT-equality
bridge (the existing bridge family's job), not a crosswalk. A crosswalk exists precisely where the
identifiers are NOT directly equal, so the fixture models the shape that actually needs one: an
internal account number (`account_id`, namespace `internal_account`) mapping to a correspondent's
account reference (`external_account_ref`, namespace `external_account`) through a cross-reference
table. Same story, two real schemes.

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

CIB_TABLE, FTR_TABLE, MAP_TABLE = "bo_cib_account", "tran_repos", "acct_xref"

#: (column, concept, sensitivity). `cust_num` is a deliberate DECOY: its `cif` namespace is absent
#: from the mapping table, so it must never take part in a candidate.
CIB_COLUMNS = (("acct_no", "account_id", ""), ("cust_num", "customer_id", ""))
FTR_COLUMNS = (("counter_party_acct_no", "external_account_ref", ""), ("party_lei", "lei", ""))
#: The mapping table carries BOTH namespaces plus a validity column — the §4-correction-10 shape:
#: the role PLUS this identifier structure proposes a candidate; the role alone proposes nothing.
MAP_COLUMNS = (("acct_no", "account_id", ""), ("ext_acct_ref", "external_account_ref", ""),
               ("valid_from", "effective_from", ""))

CIB = normalize_ref("cib", "public", CIB_TABLE)
FTR = normalize_ref("ftr", "public", FTR_TABLE)
MAP = normalize_ref("cib", "public", MAP_TABLE)
CIB_KEY = normalize_ref("cib", "public", CIB_TABLE, "acct_no")
FTR_KEY = normalize_ref("ftr", "public", FTR_TABLE, "counter_party_acct_no")
MAP_CIB = normalize_ref("cib", "public", MAP_TABLE, "acct_no")
MAP_FTR = normalize_ref("cib", "public", MAP_TABLE, "ext_acct_ref")


def build_catalog(db, *, map_table_role: str | None = "bridge",
                  cib_columns=CIB_COLUMNS, ftr_columns=FTR_COLUMNS,
                  map_columns=MAP_COLUMNS) -> None:
    """CIB (accounts + the cross-reference table) and FTR (the transaction repository)."""
    rows = [CanonicalRow("ftr", FTR_TABLE, name, "unknown", sensitivity=sensitivity)
            for name, _c, sensitivity in ftr_columns]
    build_graph(
        db, "ftr", rows,
        concepts={content_hash(row): concept_name
                  for row, (_n, concept_name, _s) in zip(rows, ftr_columns, strict=True)},
        declared_types={f"public.{FTR_TABLE}.{name}": "varchar(64)"
                        for name, _c, _s in ftr_columns})
    # One `build_graph` call per SOURCE rebuilds that source's nodes, so both CIB tables go in one.
    cib_rows, concepts, declared = [], {}, {}
    for table, columns in ((CIB_TABLE, cib_columns), (MAP_TABLE, map_columns)):
        for name, concept_name, sensitivity in columns:
            row = CanonicalRow("cib", table, name, "unknown", sensitivity=sensitivity)
            cib_rows.append(row)
            concepts[content_hash(row)] = concept_name
            declared[f"public.{table}.{name}"] = "varchar(64)"
    build_graph(db, "cib", cib_rows, concepts=concepts, declared_types=declared)
    if map_table_role is not None:
        db.execute(
            "UPDATE graph_node SET table_role = %s WHERE catalog_source = 'cib' "
            "AND kind = 'table' AND object_ref = %s", (map_table_role, f"public.{MAP_TABLE}"))


def endpoint(source: str, table: str, column: str, *, concept: str = "account_id",
             entity: str = "account") -> IdentifierEndpointV1:
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
        entity_id="account", concept="account_id",
        concept_authority=ConceptAuthority.DETERMINISTIC,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY)


def definition(**over) -> CrosswalkDefinitionRevisionV1:
    """The CIB internal account <-> FTR correspondent account crosswalk through `acct_xref`."""
    kwargs: dict = dict(
        source_endpoint=endpoint("cib", CIB_TABLE, "acct_no"),
        mapping_dataset_ref=MAP,
        source_to_mapping_pairs=(LogicalMappingPairV1(CIB_KEY, MAP_CIB),),
        mapping_to_target_pairs=(LogicalMappingPairV1(FTR_KEY, MAP_FTR),),
        target_endpoint=endpoint("ftr", FTR_TABLE, "counter_party_acct_no",
                                 concept="external_account_ref"),
    )
    kwargs.update(over)
    return CrosswalkDefinitionRevisionV1(**kwargs)

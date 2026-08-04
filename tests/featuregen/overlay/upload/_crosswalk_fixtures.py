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


# ── Task 11: observation fixtures ───────────────────────────────────────────────────────────────
#
# Everything below is measurement-shaped and lives here for the same reason the catalog does: three
# suites (store, measurement, admission) need one bank, and three hand-rolled copies is how two of
# them end up asserting against two different banks and both passing.

MAP_VALID_FROM = normalize_ref("cib", "public", MAP_TABLE, "valid_from")


def binding(source: str, table: str, *, database: str = "edp_cluster",
            schema: str = "dpl_eib", partition_columns: tuple[str, ...] = ()):
    """One physical binding for a fixture table. Addresses only — nothing here opens a cursor."""
    from featuregen.data_agent.physical import (
        PhysicalDatasetBindingV1,
        PhysicalObjectIdentityV1,
    )

    return PhysicalDatasetBindingV1(
        binding_id=f"derived-{source}-{table}",
        catalog_logical_ref=normalize_ref(source, schema, table),
        connection_id="route-1",
        identity=PhysicalObjectIdentityV1(
            catalog_source=source, database=database, schema=schema, table=table,
            object_kind="table"),
        partition_columns=partition_columns,
        business_time_column=partition_columns[0] if partition_columns else None)


def record_bindings(db, *bindings) -> None:
    from featuregen.data_agent.physical import record_binding_revision

    for item in bindings:
        record_binding_revision(db, item, recorded_by="task11-fixture")


def leg(db=None, *, which: str, endpoint_binding, mapping_binding, endpoint_column: str,
        mapping_column: str, as_of: str | None = "2026-08-04",
        principal: str = "crosswalk-profiler", cross_catalog: bool = False,
        v2_observation_revision_id: str | None = None,
        realization_revision_id: str | None = None, complete: bool = True,
        method: str = "exact", row_coverage=None, **counts):
    """One measured leg with sane, internally-consistent default counts."""
    from featuregen.data_agent.relationship_observation import RowCoverage
    from featuregen.overlay.upload.crosswalk import JoinLegKind
    from featuregen.overlay.upload.crosswalk_observation import CrosswalkLegObservationV1

    defaults = dict(
        endpoint_row_count=4, endpoint_non_null_row_count=4, endpoint_distinct_tuple_count=4,
        endpoint_duplicate_tuple_count=0, endpoint_max_rows_per_tuple=1,
        matched_endpoint_distinct=3, unmatched_endpoint_distinct=1, endpoint_orphan_rows=1,
        joined_row_count=3, max_mapping_matches_per_endpoint_row=1,
        max_endpoint_matches_per_mapping_row=1)
    defaults.update(counts)
    return CrosswalkLegObservationV1(
        leg=which,
        leg_kind=(JoinLegKind.CROSS_CATALOG if cross_catalog
                  else JoinLegKind.SAME_CATALOG).value,
        endpoint_dataset_ref=endpoint_binding.identity.table_id,
        endpoint_binding_revision_id=endpoint_binding.binding_revision_id,
        endpoint_columns=(endpoint_column,),
        mapping_columns=(mapping_column,),
        normalization_ids=("identity_v1",),
        endpoint_source_snapshot_id="endpoint-snapshot-1",
        mapping_source_snapshot_id="mapping-snapshot-1",
        as_of=as_of,
        execution_principal=principal,
        method=method,
        row_coverage=row_coverage or RowCoverage.FULL,
        complete=complete,
        v2_observation_revision_id=v2_observation_revision_id,
        realization_revision_id=realization_revision_id,
        **defaults)


def mapping_observation(mapping_binding, **counts):
    from featuregen.overlay.upload.crosswalk_observation import MappingTupleObservationV1

    defaults = dict(
        row_count=3, non_null_row_count=3, distinct_source_tuple_count=3,
        distinct_target_tuple_count=3, distinct_pair_count=3,
        duplicate_source_tuple_count=0, duplicate_target_tuple_count=0,
        max_rows_per_source_tuple=1, max_rows_per_target_tuple=1)
    defaults.update(counts)
    return MappingTupleObservationV1(
        physical_id=mapping_binding.identity.table_id,
        binding_revision_id=mapping_binding.binding_revision_id,
        binding_content_hash=mapping_binding.content_hash,
        source_columns=("acct_no",), target_columns=("ext_acct_ref",), **defaults)

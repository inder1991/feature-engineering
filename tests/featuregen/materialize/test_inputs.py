"""Spec A Task 5 — the STATIC half of a physical input (§3.3), and physical schema resolution (§3.5).

Two silent wrong answers are in scope here, and every test below is shaped around one of them.

**1. A run-time observation reaching a generation-time identity.** `business_dt` is a RUN parameter.
Resolving concrete partitions during compilation would put run-specific observations into `ir_hash`
and `CompilationIdentity`, so the generated project would change every business date — which
defeats the entire point of a hash-identified artifact. The requirement therefore records partition
COLUMNS and the DECLARED mapping, never partition VALUES, and `business_dt` appears nowhere in the
module (asserted against the source, not just the signature).

**2. Reading a different table than the catalog governs.** Logical refs are SCHEMA-FLATTENED:
`build_graph` writes every `object_ref` under `public` and the real declared schema survives only in
the NULLABLE `graph_node.schema_name` (verified interfaces §17). So `hdfc::public.transactions.amount`
may legitimately name a table that lives in `banking`, and parsing a physical schema out of the ref
segment — or defaulting it to `public` when nothing attests one — reads a DIFFERENT table and
produces plausible numbers. That is why resolution refuses rather than defaults.

Every ref here is `hdfc::public.…`, matching what an upload really produces. The spec's
`hdfc::banking.…` examples are shorthand for "the ref for that table" (§3.5), never a physical
schema; `test_spine.py` states the same thing for Task 4.
"""
import dataclasses
import inspect

import pytest

from featuregen.materialize import inputs
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.inputs import (
    PhysicalInputRequirement,
    derive_requirement,
    resolve_physical_identity,
)
from featuregen.materialize.inventory import (
    PARTITION_MAPPING_TYPES,
    AvailabilityPartition,
    ClusterInventoryV1,
    EventTimePartition,
    FullScan,
    PartitionMappingKind,
    PartitionTransform,
    StaticSnapshot,
    TableLayout,
    VerifiedUnpartitioned,
)

_SRC = "hdfc"


def _code_of(module) -> str:
    """The module's executable text, lower-cased, with comments and docstrings stripped.

    A source-scanning assertion that also read the prose would be satisfied by deleting an
    explanation, and would fail for a docstring that names the very thing it forbids.
    """
    import io
    import tokenize

    kept: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(module)).readline)
    previous = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            continue                      # a docstring: the only string that stands alone
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            kept.append(token.string)
        previous = token.type
    return " ".join(kept).lower()


TXN = f"{_SRC}::public.transactions"
TXN_AMT = f"{TXN}.txn_amt"
TXN_DT = f"{TXN}.txn_dt"
CUSTOMERS = f"{_SRC}::public.customers"

#: The environment's DECLARED logical -> physical schema mapping (§3.5 step 2). Keyed by the LOGICAL
#: table ref, because that is the only thing a caller holds; the value is a physical Hive schema.
SCHEMA_MAP = {TXN: "banking", CUSTOMERS: "banking"}


# ── the governed catalog: PUBLIC-FLATTENED refs, the real schema in `schema_name` ────────────────


def _col(db, table, column, *, schema="banking"):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name) VALUES (%s, %s, 'column', %s, %s, %s)",
        (_SRC, f"public.{table}.{column}", table, column, schema))


def _watermark(db, *, head_seq=7):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', %s) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now(), head_seq = EXCLUDED.head_seq", (_SRC, head_seq))


@pytest.fixture
def seeded_catalog(db):
    """`transactions` and `customers`, both attesting the physical schema `banking`."""
    _col(db, "transactions", "txn_amt")
    _col(db, "transactions", "txn_dt")
    _col(db, "customers", "cif_id")
    _watermark(db)
    return db


@pytest.fixture
def no_schema_name(db):
    """The same catalog from a schema-less (technical) upload: `schema_name` is NULL everywhere."""
    _col(db, "transactions", "txn_amt", schema=None)
    _col(db, "transactions", "txn_dt", schema=None)
    _watermark(db)
    return db


# ── the declared inventory ───────────────────────────────────────────────────────────────────────

TXN_MAPPING = EventTimePartition(
    time_ref=TXN_DT, partition_column="load_dt", transform=PartitionTransform.DATE_ISO,
    timezone="Asia/Kolkata")


def _txn_layout(**overrides) -> TableLayout:
    fields = {
        "schema": "banking", "table": "transactions",
        "partition_columns": (("load_dt", "string"),),
        "partition_mapping": TXN_MAPPING,
        "columns": (("txn_amt", "decimal(18,2)"), ("txn_dt", "date")),
        "location": "hdfs://nn/warehouse/banking.db/transactions",
        "rewritten_in_place": False,
    }
    fields.update(overrides)
    return TableLayout(**fields)


def _customers_layout(**overrides) -> TableLayout:
    fields = {
        "schema": "banking", "table": "customers",
        "partition_columns": None,                      # VERIFIED unpartitioned, not "unknown"
        "partition_mapping": VerifiedUnpartitioned(),
        "columns": (("cif_id", "string"), ("status_cd", "string")),
        "location": "hdfs://nn/warehouse/banking.db/customers",
        "rewritten_in_place": True,
    }
    fields.update(overrides)
    return TableLayout(**fields)


def _inventory(*layouts, schema_map=SCHEMA_MAP, captured_at="2026-07-27T09:00:00+05:30",
               environment_id="hdfc-local") -> ClusterInventoryV1:
    return ClusterInventoryV1(
        environment_id=environment_id,
        tables={f"{layout.schema}.{layout.table}": layout for layout in layouts},
        logical_schema_map=dict(schema_map),
        captured_at=captured_at)


@pytest.fixture
def inventory():
    return _inventory(_txn_layout(), _customers_layout())


@pytest.fixture
def bare_inventory():
    """An environment that declares NOTHING — no tables and no logical->physical mapping."""
    return _inventory(schema_map={})


@pytest.fixture
def empty_inventory():
    """The mapping is declared, so resolution succeeds; the TABLE is simply not in the inventory."""
    return _inventory()


# ── 1. the static half: no `business_dt`, anywhere ───────────────────────────────────────────────


def test_requirement_takes_no_business_dt():
    """A `business_dt` parameter would put a run observation into `ir_hash` (§3.3)."""
    assert "business_dt" not in inspect.signature(derive_requirement).parameters
    assert "business_dt" not in inspect.signature(resolve_physical_identity).parameters


def test_no_identifier_in_the_module_names_a_run_time_observation():
    """Stronger than the signature check: a helper reading a business date, or a partition VALUE,
    would leak the same run-time observation into a generation-time identity by another door.

    Only real code is inspected — the module's prose has to be free to NAME what it excludes."""
    code = _code_of(inputs)
    for banned in ("business_dt", "partition_spec", "partitionspec", "resolved_at"):
        assert banned not in code


def test_requirement_is_stable_across_business_dates(db, seeded_catalog, inventory):
    """The whole point: the generated project must not change every day."""
    a = derive_requirement(seeded_catalog, inventory, table_ref=TXN)
    b = derive_requirement(seeded_catalog, inventory, table_ref=TXN)
    assert a == b
    assert a.requirement_id() == b.requirement_id()


def test_requirement_records_partition_COLUMNS_not_values(db, seeded_catalog, inventory):
    req = derive_requirement(seeded_catalog, inventory, table_ref=TXN)
    assert req.partition_columns == (("load_dt", "string"),)
    assert not hasattr(req, "partition_specs")     # values are a RUN-time concern (§3.3)


def test_the_requirement_carries_no_observation_provenance(db, seeded_catalog, inventory):
    """The field set is CLOSED and matches spec §3.3 exactly: no `captured_at`, no location, no
    refresh time. Re-capturing identical metadata must not change `ir_hash`."""
    names = {f.name for f in dataclasses.fields(PhysicalInputRequirement)}
    assert names == {"catalog_source", "schema", "table", "partition_columns", "partition_mapping",
                     "layout_fingerprint", "catalog_state_stamp"}
    payload = derive_requirement(seeded_catalog, inventory, table_ref=TXN).identity_payload()
    for banned in ("captured_at", "location", "rewritten_in_place", "environment_id"):
        assert banned not in payload


def test_recapturing_identical_metadata_keeps_the_same_identity(db, seeded_catalog):
    """`captured_at` must NOT reach identity, or a rescan changes the generated project."""
    inv_a = _inventory(_txn_layout(), captured_at="2026-07-27T09:00:00+05:30")
    inv_b_same_layout_later = _inventory(_txn_layout(), captured_at="2026-08-01T23:45:00+05:30")
    assert derive_requirement(seeded_catalog, inv_a, table_ref=TXN) == \
        derive_requirement(seeded_catalog, inv_b_same_layout_later, table_ref=TXN)


def test_a_relocated_table_keeps_its_identity(db, seeded_catalog):
    """`location` and `rewritten_in_place` are operational facts about the SAME governed table, and
    §3.3 names the fingerprint SEMANTIC: partition columns+types, physical types, mapping."""
    moved = _txn_layout(location="hdfs://nn2/warehouse/banking.db/transactions",
                        rewritten_in_place=True)
    assert derive_requirement(seeded_catalog, _inventory(_txn_layout()), table_ref=TXN) == \
        derive_requirement(seeded_catalog, _inventory(moved), table_ref=TXN)


def test_changing_layout_or_a_physical_type_changes_the_fingerprint(db, seeded_catalog):
    inv_a = _inventory(_txn_layout())
    relayout = _inventory(_txn_layout(partition_columns=(("load_dt", "string"),
                                                         ("region_cd", "string"))))
    retyped = _inventory(_txn_layout(columns=(("txn_amt", "double"), ("txn_dt", "date"))))
    remapped = _inventory(_txn_layout(partition_mapping=AvailabilityPartition(
        time_ref=TXN_DT, partition_column="load_dt", transform=PartitionTransform.DATE_ISO,
        timezone="Asia/Kolkata", late_arrival_days=3)))

    base = derive_requirement(seeded_catalog, inv_a, table_ref=TXN).layout_fingerprint
    for changed in (relayout, retyped, remapped):
        other = derive_requirement(seeded_catalog, changed, table_ref=TXN)
        assert isinstance(other, PhysicalInputRequirement)
        assert other.layout_fingerprint != base


# ── 2. physical schema is RESOLVED, never parsed ─────────────────────────────────────────────────


def test_physical_schema_is_RESOLVED_never_parsed_from_the_ref(db, seeded_catalog, inventory):
    """Refs are schema-flattened to `public`; the real schema lives in graph_node.schema_name."""
    ident = resolve_physical_identity(seeded_catalog, inventory, logical_ref=TXN_AMT)
    assert ident.schema == "banking"          # from schema_name, NOT from the ref segment
    assert ident.table == "transactions"
    assert ident.catalog_source == _SRC


def test_a_table_ref_resolves_the_same_way_a_column_ref_does(db, seeded_catalog, inventory):
    assert resolve_physical_identity(seeded_catalog, inventory, logical_ref=TXN) == \
        resolve_physical_identity(seeded_catalog, inventory, logical_ref=TXN_AMT)


def test_null_schema_name_falls_back_to_the_DECLARED_mapping(db, no_schema_name, inventory):
    ident = resolve_physical_identity(no_schema_name, inventory, logical_ref=TXN_AMT)
    assert ident.schema == "banking"


def test_unresolvable_schema_REFUSES_rather_than_defaulting_to_public(
        db, no_schema_name, bare_inventory):
    """Silently defaulting to `public` would read a DIFFERENT table than the catalog governs."""
    r = resolve_physical_identity(no_schema_name, bare_inventory, logical_ref=TXN_AMT)
    assert isinstance(r, MaterializationRefused)
    assert r.code is CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED


def test_an_unknown_table_is_refused_not_defaulted(db, seeded_catalog, bare_inventory):
    r = resolve_physical_identity(seeded_catalog, bare_inventory,
                                  logical_ref=f"{_SRC}::public.nowhere.col")
    assert r.code is CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED


def test_a_MIXED_CASE_catalog_is_the_same_catalog(db, inventory):
    """`build_graph` writes `table_name`/`schema_name` in the UPLOAD's own casing, while a canonical
    logical ref is case-folded. Unquoted SQL identifiers fold, so `Banking.Transactions` and
    `banking.transactions` are ONE table — a resolver that did not fold would refuse a catalog whose
    schema is perfectly attested."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name) VALUES (%s, %s, 'column', %s, %s, %s)",
        (_SRC, "public.Transactions.TXN_AMT", "Transactions", "TXN_AMT", "Banking"))
    _watermark(db)
    ident = resolve_physical_identity(db, inventory, logical_ref=TXN_AMT)
    assert (ident.schema, ident.table) == ("banking", "transactions")


def test_two_physical_schemas_for_one_table_name_REFUSE(db, inventory):
    """One bare table name that resolves to two physical schemas cannot be resolved to either:
    picking one reads a table the catalog does not govern."""
    _col(db, "transactions", "txn_amt", schema="banking")
    _col(db, "transactions", "txn_dt", schema="risk")
    _watermark(db)
    r = resolve_physical_identity(db, inventory, logical_ref=TXN_AMT)
    assert r.code is CompilationRefusalCode.AMBIGUOUS_TABLE_NAME


def test_a_declared_mapping_that_CONTRADICTS_the_catalog_refuses(db, seeded_catalog):
    """The catalog is authority (§3.5 step 1) and the declaration is a fallback for NULL — so a
    declaration that disagrees is not silently ignored. Ignoring it would leave the operator's
    stated belief unreconciled with the table the run actually reads."""
    r = resolve_physical_identity(seeded_catalog, _inventory(schema_map={TXN: "risk"}),
                                  logical_ref=TXN_AMT)
    assert r.code is CompilationRefusalCode.AMBIGUOUS_TABLE_NAME


# ── 3. the mapping is DECLARED, never inferred (§3.4) ────────────────────────────────────────────


def test_partition_mapping_kinds_are_closed():
    assert {k.value for k in PartitionMappingKind} == {
        "event_time_partition", "availability_partition", "static_snapshot", "full_scan",
        "verified_unpartitioned"}
    assert set(PARTITION_MAPPING_TYPES) == set(PartitionMappingKind)
    assert len(set(PARTITION_MAPPING_TYPES.values())) == len(PartitionMappingKind)


def test_mapping_comes_from_the_DECLARATION_not_the_column_name(db, seeded_catalog):
    """A partition column called `load_dt` says NOTHING about how an event-time window maps onto
    load partitions — late arrivals sit outside the event range, so an inferred mapping drops data
    silently."""
    inv_load_dt_no_mapping = _inventory(_txn_layout(partition_mapping=None))
    r = derive_requirement(seeded_catalog, inv_load_dt_no_mapping, table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


def test_an_availability_mapping_declares_its_late_arrival_widening(db, seeded_catalog):
    """§3.4: an availability mapping must EXTEND the partition set beyond the event window. By how
    much is a declaration, not an inference, so it is a required field and it enters identity."""
    narrow = AvailabilityPartition(time_ref=TXN_DT, partition_column="load_dt",
                                   transform=PartitionTransform.DATE_ISO,
                                   timezone="Asia/Kolkata", late_arrival_days=3)
    wide = AvailabilityPartition(time_ref=TXN_DT, partition_column="load_dt",
                                 transform=PartitionTransform.DATE_ISO,
                                 timezone="Asia/Kolkata", late_arrival_days=10)
    a = derive_requirement(seeded_catalog, _inventory(_txn_layout(partition_mapping=narrow)),
                           table_ref=TXN)
    b = derive_requirement(seeded_catalog, _inventory(_txn_layout(partition_mapping=wide)),
                           table_ref=TXN)
    assert a.layout_fingerprint != b.layout_fingerprint


def test_a_mapping_naming_a_column_the_table_does_not_partition_by_REFUSES(db, seeded_catalog):
    off_target = EventTimePartition(time_ref=TXN_DT, partition_column="event_dt",
                                    transform=PartitionTransform.DATE_ISO,
                                    timezone="Asia/Kolkata")
    r = derive_requirement(seeded_catalog, _inventory(_txn_layout(partition_mapping=off_target)),
                           table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


def test_verified_unpartitioned_is_None(db, seeded_catalog, inventory):
    req = derive_requirement(seeded_catalog, inventory, table_ref=CUSTOMERS)
    assert req.partition_columns is None
    assert req.partition_mapping == VerifiedUnpartitioned()


def test_an_unpartitioned_table_may_not_carry_a_PARTITIONED_mapping(db, seeded_catalog):
    r = derive_requirement(seeded_catalog,
                           _inventory(_customers_layout(partition_mapping=FullScan())),
                           table_ref=CUSTOMERS)
    assert r.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


def test_a_partitioned_table_may_not_claim_to_be_unpartitioned(db, seeded_catalog):
    r = derive_requirement(seeded_catalog,
                           _inventory(_txn_layout(partition_mapping=VerifiedUnpartitioned())),
                           table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


def test_a_static_snapshot_may_only_pin_declared_partition_columns(db, seeded_catalog):
    good = StaticSnapshot(partition_values=(("load_dt", "2026-03-31"),))
    bad = StaticSnapshot(partition_values=(("as_of_dt", "2026-03-31"),))
    assert isinstance(
        derive_requirement(seeded_catalog, _inventory(_txn_layout(partition_mapping=good)),
                           table_ref=TXN), PhysicalInputRequirement)
    r = derive_requirement(seeded_catalog, _inventory(_txn_layout(partition_mapping=bad)),
                           table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


def test_no_partition_columns_at_all_is_UNKNOWN_not_unpartitioned(db, seeded_catalog):
    """`None` means VERIFIED unpartitioned. An empty tuple means the capture found nothing to say —
    which is exactly the "unknown" §3.4 refuses."""
    r = derive_requirement(seeded_catalog, _inventory(_txn_layout(partition_columns=())),
                           table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_a_table_absent_from_the_inventory_REFUSES(db, seeded_catalog, empty_inventory):
    r = derive_requirement(seeded_catalog, empty_inventory, table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_an_inventory_entry_keyed_under_the_wrong_table_REFUSES(db, seeded_catalog):
    """The key and the layout's own identity must agree, or the requirement describes one table and
    is looked up under another."""
    inv = ClusterInventoryV1(
        environment_id="hdfc-local",
        tables={"banking.transactions": _txn_layout(table="txns_archive")},
        logical_schema_map=dict(SCHEMA_MAP), captured_at="2026-07-27T09:00:00+05:30")
    r = derive_requirement(seeded_catalog, inv, table_ref=TXN)
    assert r.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_an_unresolvable_schema_refuses_BEFORE_the_inventory_lookup(db, no_schema_name,
                                                                    bare_inventory):
    r = derive_requirement(no_schema_name, bare_inventory, table_ref=TXN)
    assert r.code is CompilationRefusalCode.PHYSICAL_SCHEMA_NOT_RESOLVED


# ── 4. the catalog state stamp ───────────────────────────────────────────────────────────────────


def test_the_stamp_records_catalog_state_and_excludes_wall_clock(db, seeded_catalog, inventory):
    """`head_seq` is a monotone event counter — identity-bearing. `last_completed_at` is when a scan
    happened to run: re-running the projection over an unchanged catalog must not move identity."""
    before = derive_requirement(seeded_catalog, inventory, table_ref=TXN)
    assert dict(before.catalog_state_stamp)["head_seq"] == "7"

    _watermark(seeded_catalog, head_seq=7)          # a fresh scan, same events
    assert derive_requirement(seeded_catalog, inventory, table_ref=TXN) == before

    _watermark(seeded_catalog, head_seq=9)          # the catalog actually moved
    assert derive_requirement(seeded_catalog, inventory, table_ref=TXN) != before


def test_a_catalog_with_no_watermark_is_recorded_honestly(db, inventory):
    """There is no §14 member for "catalog state unknown", and minting one would fork the closed
    vocabulary. The stamp says so instead, and it enters identity — so a later compile against a
    stamped catalog is visibly a different artifact."""
    _col(db, "transactions", "txn_amt")
    req = derive_requirement(db, inventory, table_ref=TXN)
    assert dict(req.catalog_state_stamp)["stamp_kind"] == "no_usable_state_stamp"
    assert "head_seq" not in dict(req.catalog_state_stamp)


# ── 5. every failure is a GOVERNED refusal ───────────────────────────────────────────────────────


def test_refusals_are_returned_not_raised(db, seeded_catalog, empty_inventory):
    r = derive_requirement(seeded_catalog, empty_inventory, table_ref=TXN)
    assert isinstance(r, MaterializationRefused)
    assert isinstance(r.code, CompilationRefusalCode)


def test_a_malformed_ref_is_a_caller_error_not_a_governed_refusal(db, seeded_catalog, inventory):
    """§14's vocabulary describes governed verdicts. "the call was assembled wrongly" is not one —
    the same distinction `joins.plan_join` draws for a cross-catalog identity."""
    with pytest.raises(ValueError):
        resolve_physical_identity(seeded_catalog, inventory, logical_ref="not-a-ref")


def test_derive_requirement_rejects_a_COLUMN_ref(db, seeded_catalog, inventory):
    """A requirement is per TABLE (§3.3). Accepting a column ref would silently derive the same
    requirement many times per table and invite a per-column reading of `layout_fingerprint`."""
    with pytest.raises(ValueError):
        derive_requirement(seeded_catalog, inventory, table_ref=TXN_AMT)


def test_no_pyspark_import():
    assert "pyspark" not in inspect.getsource(inputs)

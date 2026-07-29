"""Task 0 — Spec §0/§3.4: the two ways a `ClusterInventoryV1` comes into existence, and what
neither of them is allowed to invent.

The tables here are the REAL catalogued ones, not the plan's `banking.*` placeholders: an inventory
keyed by a table nobody has is an inventory that can never be checked against a cluster.
`DPL_EIB_COMPLIANCE.COMP_FINANCIAL_TRAN_REPOS_DLY` prints Hive-style bare types (`string`, `double`,
`timestamp`); `BO_DPL_CIB.BO_CIB_CUSTOMER` prints RDBMS types WITH parameters (`varchar(150)`,
`timestamp(0)`) — the two spellings coexist on purpose, because a loader that normalised either into
the other would erase a physical-type difference §6's adapter is required to see.

The partition layouts below are FIXTURES, not claims about the cluster. Nobody has run
`DESCRIBE FORMATTED` against it yet; `conf/environments/hdfc-local-inventory.yml` still refuses, and
one test pins that it does.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.inventory import (
    PARTITION_MAPPING_TYPES,
    AvailabilityPartition,
    EngineVersions,
    EventTimePartition,
    FullScan,
    MetastoreInventoryAdapter,
    PartitionMappingKind,
    PartitionTransform,
    StaticSnapshot,
    TableDeclaration,
    VerifiedUnpartitioned,
    load_inventory,
)

TRANSACTIONS = "DPL_EIB_COMPLIANCE.COMP_FINANCIAL_TRAN_REPOS_DLY"
CUSTOMER = "BO_DPL_CIB.BO_CIB_CUSTOMER"
TRAN_DT = "hdfc::public.comp_financial_tran_repos_dly.tran_dt"

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_ENVIRONMENT = REPO_ROOT / "conf" / "environments" / "hdfc-local-inventory.yml"


# ── fixture documents ────────────────────────────────────────────────────────────────────────────


def _engine_versions() -> dict[str, str]:
    return {"hive": "3.1.3", "spark": "3.5.1", "metastore": "3.1.3", "python": "3.11.9",
            "java": "11.0.22", "pyspark": "3.5.1", "kedro": "0.19.6", "kedro_datasets": "3.0.0"}


def _transactions_layout() -> dict[str, Any]:
    return {
        "partition_columns": [["load_dt", "string"]],
        "partition_mapping": {
            "kind": "availability_partition", "time_ref": TRAN_DT,
            "partition_column": "load_dt", "transform": "date_iso",
            "timezone": "Asia/Kolkata", "late_arrival_days": 3},
        "columns": [["tran_id", "string"], ["tran_amt", "double"], ["tran_dt", "timestamp"]],
        "location": "hdfs://nn/warehouse/dpl_eib_compliance.db/comp_financial_tran_repos_dly",
        "rewritten_in_place": False,
    }


def _customer_layout() -> dict[str, Any]:
    return {
        "partition_columns": None,                       # VERIFIED unpartitioned, not "unknown"
        "partition_mapping": {"kind": "verified_unpartitioned"},
        "columns": [["cif_id", "varchar(150)"], ["business_dt", "timestamp(0)"],
                    ["cust_status_cd", "varchar(20)"]],
        "location": "hdfs://nn/warehouse/bo_dpl_cib.db/bo_cib_customer",
        "rewritten_in_place": True,
    }


def _document() -> dict[str, Any]:
    return {
        "environment_id": "hdfc-local",
        "captured_at": "2026-07-29T09:00:00+05:30",
        "engine_versions": _engine_versions(),
        "logical_schema_map": {TRAN_DT.rsplit(".", 1)[0]: "DPL_EIB_COMPLIANCE"},
        "tables": {TRANSACTIONS: _transactions_layout(), CUSTOMER: _customer_layout()},
    }


def _write(tmp_path: Path, document: Any, name: str = "inventory.yml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def inventory_yaml(tmp_path):
    return _write(tmp_path, _document())


# ── the loader: absent is not unpartitioned ──────────────────────────────────────────────────────


def test_unpartitioned_is_explicit_not_absent(inventory_yaml):
    """`partition_columns is None` is a POSITIVE statement: somebody looked and there are none."""
    inv = load_inventory(inventory_yaml)
    assert inv.tables[CUSTOMER].partition_columns is None
    assert inv.tables[CUSTOMER].partition_mapping == VerifiedUnpartitioned()


def test_a_table_missing_from_the_inventory_is_not_None(tmp_path):
    """The other half of the same distinction — and it must be a KeyError, not a `None`.

    A `.get()`-shaped absence would let an unexamined table read as "just scan it".
    """
    document = _document()
    del document["tables"][TRANSACTIONS]
    inv = load_inventory(_write(tmp_path, document))
    with pytest.raises(KeyError):
        inv.tables[TRANSACTIONS]


def test_an_absent_partition_columns_key_is_not_a_verified_unpartitioned_table(tmp_path):
    """`partition_columns: null` says none exist; omitting the key says nothing at all.

    This is the loader-level form of the same trap: `.get("partition_columns")` returns `None` for
    both, which would silently promote "nobody looked" to "verified unpartitioned".
    """
    document = _document()
    del document["tables"][TRANSACTIONS]["partition_columns"]
    with pytest.raises(ValueError, match="partition_columns"):
        load_inventory(_write(tmp_path, document))


def test_an_empty_partition_column_list_refuses(tmp_path):
    document = _document()
    document["tables"][TRANSACTIONS]["partition_columns"] = []
    with pytest.raises(ValueError, match="EMPTY"):
        load_inventory(_write(tmp_path, document))


def test_partition_column_ORDER_survives_the_loader(tmp_path):
    """Order is semantic: the partition directory path is built from it, left to right."""
    document = _document()
    document["tables"][TRANSACTIONS]["partition_columns"] = [
        ["load_dt", "string"], ["src_sys_cd", "string"]]
    document["tables"][TRANSACTIONS]["partition_mapping"] = {
        "kind": "static_snapshot",
        "partition_values": [["load_dt", "2026-07-29"], ["src_sys_cd", "EIB"]]}
    inv = load_inventory(_write(tmp_path, document))
    assert inv.tables[TRANSACTIONS].partition_columns == (
        ("load_dt", "string"), ("src_sys_cd", "string"))


# ── the loader: every runtime version is required ────────────────────────────────────────────────


def test_every_runtime_version_is_required(tmp_path):
    document = _document()
    del document["engine_versions"]["kedro"]
    with pytest.raises(ValueError, match="kedro"):
        load_inventory(_write(tmp_path, document))


def test_kedro_datasets_is_required_too(tmp_path):
    """Kedro ships its dataset classes in a SEPARATELY versioned distribution.

    `spark.SparkHiveDataset` — the class that reads every governed source table — resolves out of
    `kedro-datasets`, so a lock naming only `kedro` leaves it unpinned.
    """
    document = _document()
    del document["engine_versions"]["kedro_datasets"]
    with pytest.raises(ValueError, match="kedro_datasets"):
        load_inventory(_write(tmp_path, document))


@pytest.mark.parametrize("field", sorted(_engine_versions()))
def test_no_runtime_version_may_be_null(tmp_path, field):
    """A null is what an unfilled template looks like, and it must not read as a pin."""
    document = _document()
    document["engine_versions"][field] = None
    with pytest.raises(ValueError, match=field):
        load_inventory(_write(tmp_path, document))


def test_a_runtime_version_nothing_runs_refuses(tmp_path):
    document = _document()
    document["engine_versions"]["scala"] = "2.12.18"
    with pytest.raises(ValueError, match="scala"):
        load_inventory(_write(tmp_path, document))


# ── the loader: the closed §3.4 variants ─────────────────────────────────────────────────────────


MAPPING_DOCUMENTS: dict[PartitionMappingKind, dict[str, Any]] = {
    PartitionMappingKind.EVENT_TIME_PARTITION: {
        "kind": "event_time_partition", "time_ref": TRAN_DT, "partition_column": "load_dt",
        "transform": "date_iso", "timezone": "Asia/Kolkata"},
    PartitionMappingKind.AVAILABILITY_PARTITION: {
        "kind": "availability_partition", "time_ref": TRAN_DT, "partition_column": "load_dt",
        "transform": "date_compact", "timezone": "Asia/Kolkata", "late_arrival_days": 3},
    PartitionMappingKind.STATIC_SNAPSHOT: {
        "kind": "static_snapshot", "partition_values": [["load_dt", "2026-07-29"]]},
    PartitionMappingKind.FULL_SCAN: {"kind": "full_scan"},
    PartitionMappingKind.VERIFIED_UNPARTITIONED: {"kind": "verified_unpartitioned"},
}

EXPECTED_MAPPINGS = {
    PartitionMappingKind.EVENT_TIME_PARTITION: EventTimePartition(
        time_ref=TRAN_DT, partition_column="load_dt", transform=PartitionTransform.DATE_ISO,
        timezone="Asia/Kolkata"),
    PartitionMappingKind.AVAILABILITY_PARTITION: AvailabilityPartition(
        time_ref=TRAN_DT, partition_column="load_dt", transform=PartitionTransform.DATE_COMPACT,
        timezone="Asia/Kolkata", late_arrival_days=3),
    PartitionMappingKind.STATIC_SNAPSHOT: StaticSnapshot(
        partition_values=(("load_dt", "2026-07-29"),)),
    PartitionMappingKind.FULL_SCAN: FullScan(),
    PartitionMappingKind.VERIFIED_UNPARTITIONED: VerifiedUnpartitioned(),
}


def test_the_loader_covers_every_closed_variant():
    """A kind with no loadable document is a kind that can be declared and never read."""
    assert set(MAPPING_DOCUMENTS) == set(PartitionMappingKind) == set(PARTITION_MAPPING_TYPES)
    assert set(EXPECTED_MAPPINGS) == set(PartitionMappingKind)


@pytest.mark.parametrize("kind", sorted(PartitionMappingKind, key=str))
def test_every_partition_mapping_variant_round_trips(tmp_path, kind):
    document = _document()
    document["tables"][TRANSACTIONS]["partition_mapping"] = MAPPING_DOCUMENTS[kind]
    if kind is PartitionMappingKind.VERIFIED_UNPARTITIONED:
        document["tables"][TRANSACTIONS]["partition_columns"] = None
    inv = load_inventory(_write(tmp_path, document))
    assert inv.tables[TRANSACTIONS].partition_mapping == EXPECTED_MAPPINGS[kind]


def test_a_mapping_kind_nobody_implemented_refuses(tmp_path):
    """The fallback an unlisted kind would land in is 'read the window's own partitions' — which is
    exactly the late-arrival data loss §3.4 exists to prevent."""
    document = _document()
    document["tables"][TRANSACTIONS]["partition_mapping"] = {"kind": "load_dt_partition"}
    with pytest.raises(ValueError, match="load_dt_partition"):
        load_inventory(_write(tmp_path, document))


def test_an_unlisted_transform_refuses(tmp_path):
    document = _document()
    document["tables"][TRANSACTIONS]["partition_mapping"]["transform"] = "%Y-%m"
    with pytest.raises(ValueError, match="transform"):
        load_inventory(_write(tmp_path, document))


def test_an_availability_mapping_must_say_by_how_much_it_widens(tmp_path):
    document = _document()
    del document["tables"][TRANSACTIONS]["partition_mapping"]["late_arrival_days"]
    with pytest.raises(ValueError, match="late_arrival_days"):
        load_inventory(_write(tmp_path, document))


def test_a_yaml_boolean_is_not_a_late_arrival_window(tmp_path):
    """`late_arrival_days: yes` parses as `True`, and `True == 1` — a one-day widening nobody chose."""
    document = _document()
    document["tables"][TRANSACTIONS]["partition_mapping"]["late_arrival_days"] = True
    with pytest.raises(ValueError, match="late_arrival_days"):
        load_inventory(_write(tmp_path, document))


def test_a_timezone_that_is_not_a_zone_refuses(tmp_path):
    document = _document()
    document["tables"][TRANSACTIONS]["partition_mapping"]["timezone"] = "IST"
    with pytest.raises(ValueError, match="IANA"):
        load_inventory(_write(tmp_path, document))


def test_a_null_partition_mapping_loads_and_refuses_later(tmp_path):
    """Loadable so a table can be captured while its mapping is still being worked out; the refusal
    is `PARTITION_MAPPING_NOT_DECLARED` at compile time, not a file nobody can open."""
    document = _document()
    document["tables"][TRANSACTIONS]["partition_mapping"] = None
    assert load_inventory(_write(tmp_path, document)).tables[TRANSACTIONS].partition_mapping is None


# ── the loader: fail closed on everything else ───────────────────────────────────────────────────


def test_a_key_nothing_reads_refuses(tmp_path):
    """A misspelling and an omission look identical to a permissive parser."""
    document = _document()
    document["enviroment_id"] = document.pop("environment_id")
    with pytest.raises(ValueError, match="enviroment_id"):
        load_inventory(_write(tmp_path, document))


def test_a_layout_key_nothing_reads_refuses(tmp_path):
    document = _document()
    document["tables"][TRANSACTIONS]["late_arrival_days"] = 3     # right field, wrong level
    with pytest.raises(ValueError, match="late_arrival_days"):
        load_inventory(_write(tmp_path, document))


def test_the_engines_block_is_declared_but_not_yet_read(tmp_path):
    """§3.4b's per-`(source_type, source)` engine map has a refusal code and no reader.

    It stays loadable — deleting it from the environment file to satisfy a strict parser would throw
    away the one place the mapping is written down — but nothing typed carries it yet, so it must
    not be mistaken for something the compiler consults.
    """
    document = _document()
    document["engines"] = {"edp/hive": {"kind": "hive", "default_schema": None}}
    inv = load_inventory(_write(tmp_path, document))
    assert not hasattr(inv, "engines")


def test_a_table_key_that_is_not_schema_dot_table_refuses(tmp_path):
    document = _document()
    document["tables"]["COMP_FINANCIAL_TRAN_REPOS_DLY"] = document["tables"].pop(TRANSACTIONS)
    with pytest.raises(ValueError, match="schema"):
        load_inventory(_write(tmp_path, document))


def test_two_spellings_of_one_table_refuse(tmp_path):
    """Unquoted SQL identifiers fold, so only one of the two entries would ever be read."""
    document = _document()
    document["tables"][TRANSACTIONS.lower()] = _transactions_layout()
    with pytest.raises(ValueError, match="twice"):
        load_inventory(_write(tmp_path, document))


def test_rewritten_in_place_is_a_boolean_not_a_truthy_string(tmp_path):
    document = _document()
    document["tables"][TRANSACTIONS]["rewritten_in_place"] = "false"
    with pytest.raises(ValueError, match="rewritten_in_place"):
        load_inventory(_write(tmp_path, document))


def test_unparseable_yaml_refuses(tmp_path):
    path = tmp_path / "broken.yml"
    path.write_text("environment_id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="parseable"):
        load_inventory(path)


def test_a_document_that_is_not_a_mapping_refuses(tmp_path):
    path = tmp_path / "list.yml"
    path.write_text("- hdfc-local\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_inventory(path)


def test_the_shipped_environment_file_refuses_until_a_human_fills_it():
    """`conf/environments/hdfc-local-inventory.yml` is a TEMPLATE — every version is still null.

    Pinned so the placeholder can never be mistaken for a captured environment: the moment somebody
    fills it in, this test tells them by failing, and the fill-in is reviewed rather than assumed.
    The refusal must name a RUNTIME VERSION, not a structural problem — a template that also had the
    wrong shape would refuse for a reason that says nothing about how far from done it is.
    """
    assert SHIPPED_ENVIRONMENT.exists()
    with pytest.raises(ValueError, match="engine_versions"):
        load_inventory(SHIPPED_ENVIRONMENT)


def test_the_shipped_environment_file_is_otherwise_the_right_shape(tmp_path):
    """Filling in the versions must be the ONLY thing between the template and a load.

    Read it, substitute plausible versions, and it loads — so the template's remaining nulls are a
    to-do list and not a second, undiscovered set of problems. It also pins that the `engines:`
    block the template still carries does not stop the file being read.
    """
    document = yaml.safe_load(SHIPPED_ENVIRONMENT.read_text(encoding="utf-8"))
    assert "engines" in document
    document["engine_versions"] = _engine_versions()
    document["captured_at"] = "2026-07-29T09:00:00+05:30"

    inv = load_inventory(_write(tmp_path, document, "filled.yml"))
    assert inv.environment_id == "hdfc-local"
    assert inv.tables == {}            # nobody has captured a table yet
    assert inv.logical_schema_map == {}


def test_loading_twice_is_the_same_inventory(inventory_yaml):
    first, second = load_inventory(inventory_yaml), load_inventory(inventory_yaml)
    assert first == second
    assert materialize_hash(first.tables[TRANSACTIONS].semantic_payload()) == \
        materialize_hash(second.tables[TRANSACTIONS].semantic_payload())


# ── the adapter ──────────────────────────────────────────────────────────────────────────────────


class FakeMetastore:
    """A metastore that answers metadata questions and cannot answer any other kind."""

    def __init__(self, tables: dict[str, dict[str, Any]]) -> None:
        self.tables = {key.lower(): value for key, value in tables.items()}

    def _entry(self, schema: str, table: str) -> dict[str, Any] | None:
        return self.tables.get(f"{schema}.{table}".lower())

    def describe_columns(self, *, schema: str, table: str):
        entry = self._entry(schema, table)
        return None if entry is None else entry["columns"]

    def describe_partition_columns(self, *, schema: str, table: str):
        entry = self._entry(schema, table)
        return () if entry is None else entry["partition_columns"]

    def table_location(self, *, schema: str, table: str) -> str:
        entry = self._entry(schema, table)
        assert entry is not None
        return entry["location"]


@pytest.fixture
def fake_metastore():
    return FakeMetastore({
        TRANSACTIONS: {
            "columns": [["tran_id", "string"], ["tran_amt", "double"], ["tran_dt", "timestamp"]],
            "partition_columns": [["load_dt", "string"], ["src_sys_cd", "string"]],
            "location": "hdfs://nn/warehouse/dpl_eib_compliance.db/comp_financial_tran_repos_dly"},
        CUSTOMER: {
            "columns": [["cif_id", "varchar(150)"], ["business_dt", "timestamp(0)"]],
            "partition_columns": [],
            "location": "hdfs://nn/warehouse/bo_dpl_cib.db/bo_cib_customer"},
    })


TXN_MAPPING = AvailabilityPartition(
    time_ref=TRAN_DT, partition_column="load_dt", transform=PartitionTransform.DATE_ISO,
    timezone="Asia/Kolkata", late_arrival_days=3)

DECLARATIONS = {
    TRANSACTIONS: TableDeclaration(partition_mapping=TXN_MAPPING, rewritten_in_place=False),
    CUSTOMER: TableDeclaration(partition_mapping=VerifiedUnpartitioned(), rewritten_in_place=True),
}

ENGINE_VERSIONS = EngineVersions(**_engine_versions())


def _capture(conn, tables, *, declarations=None, clock=None):
    return MetastoreInventoryAdapter().capture(
        conn, tables,
        environment_id="hdfc-local",
        engine_versions=ENGINE_VERSIONS,
        declarations=DECLARATIONS if declarations is None else declarations,
        logical_schema_map={},
        clock=clock or (lambda: "2026-07-29T09:00:00+05:30"))


def test_adapter_captures_partition_columns_in_order(fake_metastore):
    """Partition order is semantic, not cosmetic — the folder path is built from it."""
    inv = _capture(fake_metastore, [TRANSACTIONS])
    assert inv.tables[TRANSACTIONS].partition_columns == (
        ("load_dt", "string"), ("src_sys_cd", "string"))


def test_adapter_records_no_partition_columns_as_VERIFIED_unpartitioned(fake_metastore):
    """`None`, never `()`. An empty tuple is what "nobody said" looks like downstream."""
    layout = _capture(fake_metastore, [CUSTOMER]).tables[CUSTOMER]
    assert layout.partition_columns is None
    assert layout.partition_mapping == VerifiedUnpartitioned()


def test_adapter_never_invents_a_partition_mapping(fake_metastore):
    """A metastore knows a column is called `load_dt`. It cannot know whether a 90-day EVENT window
    maps onto 90 of its partitions, so an undeclared mapping stays undeclared."""
    undeclared = {TRANSACTIONS: TableDeclaration(
        partition_mapping=None, rewritten_in_place=False)}
    inv = _capture(fake_metastore, [TRANSACTIONS], declarations=undeclared)
    assert inv.tables[TRANSACTIONS].partition_mapping is None


def test_recapturing_an_unchanged_table_is_fingerprint_stable(fake_metastore):
    """The property `runprep` rests on: it refuses a run whose table was repartitioned since compile
    by comparing `materialize_hash(layout.semantic_payload())` against the bound fingerprint. That is
    only a repartition detector if OBSERVATION provenance stays out of the semantic shape — otherwise
    every re-capture would look like a repartition and stop every run."""
    first = _capture(fake_metastore, [TRANSACTIONS], clock=lambda: "2026-07-29T09:00:00+05:30")
    later = _capture(fake_metastore, [TRANSACTIONS], clock=lambda: "2026-09-01T23:45:00+05:30")

    assert first.captured_at != later.captured_at
    assert first.tables[TRANSACTIONS].semantic_payload() == \
        later.tables[TRANSACTIONS].semantic_payload()
    assert materialize_hash(first.tables[TRANSACTIONS].semantic_payload()) == \
        materialize_hash(later.tables[TRANSACTIONS].semantic_payload())


def test_a_moved_table_is_not_a_repartitioned_one(fake_metastore):
    """Moving a warehouse directory changes `location` and nothing a feature means."""
    before = _capture(fake_metastore, [TRANSACTIONS]).tables[TRANSACTIONS]
    moved = copy.deepcopy(fake_metastore)
    moved.tables[TRANSACTIONS.lower()]["location"] = "hdfs://nn2/warehouse/relocated"
    after = _capture(moved, [TRANSACTIONS]).tables[TRANSACTIONS]

    assert before.location != after.location
    assert materialize_hash(before.semantic_payload()) == materialize_hash(after.semantic_payload())


def test_a_repartitioned_table_IS_a_different_fingerprint(fake_metastore):
    """The other side of the same property — otherwise the guard would never fire."""
    before = _capture(fake_metastore, [TRANSACTIONS]).tables[TRANSACTIONS]
    repartitioned = copy.deepcopy(fake_metastore)
    repartitioned.tables[TRANSACTIONS.lower()]["partition_columns"] = [["load_dt", "string"]]
    after = _capture(repartitioned, [TRANSACTIONS]).tables[TRANSACTIONS]

    assert materialize_hash(before.semantic_payload()) != materialize_hash(after.semantic_payload())


def test_a_reordered_partition_key_IS_a_different_fingerprint(fake_metastore):
    """Same columns, same types, different order — a different physical layout and a different path."""
    before = _capture(fake_metastore, [TRANSACTIONS]).tables[TRANSACTIONS]
    swapped = copy.deepcopy(fake_metastore)
    swapped.tables[TRANSACTIONS.lower()]["partition_columns"] = [
        ["src_sys_cd", "string"], ["load_dt", "string"]]
    after = _capture(swapped, [TRANSACTIONS]).tables[TRANSACTIONS]

    assert materialize_hash(before.semantic_payload()) != materialize_hash(after.semantic_payload())


def test_a_capture_and_a_transcription_of_one_layout_agree(tmp_path, fake_metastore):
    """The two entry points must produce the same fingerprint for the same table, or a hand-written
    inventory and a refreshed one would stop every run that crossed between them."""
    captured = _capture(fake_metastore, [CUSTOMER]).tables[CUSTOMER]
    document = _document()
    document["tables"] = {CUSTOMER: {
        "partition_columns": None,
        "partition_mapping": {"kind": "verified_unpartitioned"},
        "columns": [["cif_id", "varchar(150)"], ["business_dt", "timestamp(0)"]],
        "location": "hdfs://nn/warehouse/bo_dpl_cib.db/bo_cib_customer",
        "rewritten_in_place": True}}
    transcribed = load_inventory(_write(tmp_path, document)).tables[CUSTOMER]

    assert captured == transcribed
    assert materialize_hash(captured.semantic_payload()) == \
        materialize_hash(transcribed.semantic_payload())


def test_adapter_refuses_a_table_the_metastore_does_not_have(fake_metastore):
    """It must not simply leave it out: an absent entry reads downstream as "nobody looked"."""
    declarations = dict(DECLARATIONS)
    declarations["BO_DPL_CIB.BO_CIB_ACCOUNT"] = TableDeclaration(
        partition_mapping=None, rewritten_in_place=False)
    with pytest.raises(ValueError, match="BO_CIB_ACCOUNT"):
        _capture(fake_metastore, ["BO_DPL_CIB.BO_CIB_ACCOUNT"], declarations=declarations)


def test_adapter_refuses_a_table_with_no_declaration(fake_metastore):
    """`rewritten_in_place` has no safe default: both answers are a claim about a feed."""
    with pytest.raises(ValueError, match="no declaration"):
        _capture(fake_metastore, [TRANSACTIONS], declarations={})


def test_adapter_refuses_a_stale_verified_unpartitioned_declaration(fake_metastore):
    stale = {TRANSACTIONS: TableDeclaration(
        partition_mapping=VerifiedUnpartitioned(), rewritten_in_place=False)}
    with pytest.raises(ValueError, match="VERIFIED unpartitioned"):
        _capture(fake_metastore, [TRANSACTIONS], declarations=stale)


def test_adapter_refuses_a_mapping_over_partitions_that_do_not_exist(fake_metastore):
    stale = {CUSTOMER: TableDeclaration(partition_mapping=TXN_MAPPING, rewritten_in_place=True)}
    with pytest.raises(ValueError, match="stale"):
        _capture(fake_metastore, [CUSTOMER], declarations=stale)


def test_adapter_refuses_a_column_that_is_both_data_and_partition(fake_metastore):
    """One name with two declared physical types has no single answer to either question."""
    doubled = copy.deepcopy(fake_metastore)
    doubled.tables[TRANSACTIONS.lower()]["columns"].append(["load_dt", "date"])
    with pytest.raises(ValueError, match="load_dt"):
        _capture(doubled, [TRANSACTIONS])


def test_adapter_refuses_one_table_requested_twice(fake_metastore):
    with pytest.raises(ValueError, match="twice"):
        _capture(fake_metastore, [TRANSACTIONS, TRANSACTIONS.lower()])


def test_adapter_captures_what_the_metastore_prints(fake_metastore):
    """Parameterised RDBMS types are carried through unnormalised — `varchar(150)` is not `string`,
    and §6's physical-type adapter is the thing entitled to decide what the difference means."""
    layout = _capture(fake_metastore, [CUSTOMER]).tables[CUSTOMER]
    assert layout.columns == (("cif_id", "varchar(150)"), ("business_dt", "timestamp(0)"))
    assert layout.schema == "BO_DPL_CIB" and layout.table == "BO_CIB_CUSTOMER"

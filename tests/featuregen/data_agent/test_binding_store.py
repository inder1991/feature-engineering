"""The configuration nothing could supply.

`PhysicalDatasetBindingV1` was constructed nowhere in `src/` — only in tests — because the
information to build one existed nowhere: the catalog records a schema and no database, and there was
no connection registry. That is why `ExecutionInputs` could not be assembled by any caller, and why
the analysis route honestly reports that a plan cannot execute.

The property under test throughout: **a stored binding is a record, not a permission.** Resolution
re-checks the connection's allowlist and active flag every time, because the row may have been
written before a grant was revoked.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.binding_store import (
    declare_catalog_engine,
    read_connection,
    record_binding,
    record_connection,
    resolve_binding,
    resolve_table,
)
from featuregen.data_agent.connection import ConnectionError_, DataSourceConnectionV1
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1


def _connection(**over) -> DataSourceConnectionV1:
    kw = dict(connection_id="hive-uat", environment_id="uat", kind="hive",
              host="hiveserver2.internal", port=10000, auth_mechanism="kerberos",
              secret_ref="vault://featuregen/hive-uat", execution_principal="svc_ro",
              allowed_schemas=frozenset({"dpl_eib"}), active=True)
    kw.update(over)
    return DataSourceConnectionV1(**kw)


def _binding(**over) -> PhysicalDatasetBindingV1:
    identity_kw = dict(catalog_source="ftr", database="prod_warehouse", schema="dpl_eib",
                       table="tran_repos", object_kind="table")
    identity_kw.update(over.pop("identity", {}))
    kw = dict(binding_id="b-tran-repos", catalog_logical_ref="ftr::dpl_eib.tran_repos",
              connection_id="hive-uat", identity=PhysicalObjectIdentityV1(**identity_kw))
    kw.update(over)
    return PhysicalDatasetBindingV1(**kw)


@pytest.fixture
def configured(db):
    record_connection(db, _connection())
    record_binding(db, _binding())
    return db


# ── the round trip ───────────────────────────────────────────────────────────────────────────────

def test_a_binding_resolves_to_the_address_the_catalog_could_not_supply(configured):
    """The DATABASE is the whole point: it is the component `PhysicalObjectIdentityV1` requires and
    the catalog does not carry."""
    binding, connection = resolve_binding(configured, catalog_source="ftr", table="tran_repos")
    assert binding.identity.database == "prod_warehouse"
    assert binding.identity.schema == "dpl_eib"
    assert connection.connection_id == "hive-uat"


def test_lookup_is_by_SOURCE_and_TABLE_because_that_is_all_a_logical_ref_carries(configured):
    """`ftr::tran_repos.cif_id` names no schema, so resolution cannot require one — the schema comes
    back FROM the binding."""
    binding, _ = resolve_binding(configured, catalog_source="ftr", table="TRAN_REPOS")
    assert binding.identity.schema == "dpl_eib"


def test_an_unconfigured_table_is_an_ABSENCE_not_an_error(configured):
    """Most deployments have no bindings. Raising would turn "not configured" into a fault, and the
    preview's honest `EXECUTION_INPUTS_ABSENT` into a 500."""
    assert resolve_binding(configured, catalog_source="ftr", table="not_bound") is None
    assert resolve_binding(configured, catalog_source="other", table="tran_repos") is None


# ── a record is not a permission ─────────────────────────────────────────────────────────────────

def test_a_schema_no_longer_on_the_allowlist_REFUSES_at_resolve_time(configured):
    """THE property. The binding was written while `dpl_eib` was approved; the grant was then
    narrowed. Trusting the stored row would let anyone who can write one widen what may be read."""
    record_connection(configured, _connection(allowed_schemas=frozenset({"something_else"})))
    with pytest.raises(ConnectionError_, match="allowlist"):
        resolve_binding(configured, catalog_source="ftr", table="tran_repos")


def test_an_INACTIVE_connection_refuses_rather_than_reading_as_unconfigured(configured):
    """Distinct outcomes on purpose: "no binding" means nobody has set this up, and "inactive" means
    somebody turned it off. Collapsing the second into the first hides a revoked grant."""
    record_connection(configured, _connection(active=False))
    with pytest.raises(ConnectionError_, match="not active"):
        resolve_binding(configured, catalog_source="ftr", table="tran_repos")


def test_a_binding_naming_a_connection_that_does_not_exist_is_refused(db):
    record_connection(db, _connection())
    record_binding(db, _binding())
    db.execute("UPDATE physical_dataset_binding SET connection_id = 'hive-uat'")
    db.execute("ALTER TABLE physical_dataset_binding DROP CONSTRAINT "
               "physical_dataset_binding_connection_id_fkey")
    db.execute("UPDATE physical_dataset_binding SET connection_id = 'ghost'")
    with pytest.raises(ConnectionError_, match="does not exist"):
        resolve_binding(db, catalog_source="ftr", table="tran_repos")


# ── the credential never lands in the table ──────────────────────────────────────────────────────

def test_only_a_secret_REFERENCE_is_stored(configured):
    """So the registry can be read, dumped and reviewed without exposing anything. A password column
    would put the credential in every backup of the catalog."""
    columns = [r[0] for r in configured.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'data_source_connection'").fetchall()]
    assert "secret_ref" in columns
    assert not any(c in columns for c in ("password", "secret", "credential", "token"))


def test_the_connection_round_trips_including_its_allowlist(configured):
    got = read_connection(configured, "hive-uat")
    assert got is not None
    assert got.allowed_schemas == frozenset({"dpl_eib"})
    assert got.execution_principal == "svc_ro"
    assert got.secret_ref == "vault://featuregen/hive-uat"


# ── one address per physical table ───────────────────────────────────────────────────────────────

def test_rebinding_a_table_REPLACES_rather_than_duplicating(configured):
    """Two bindings for one table would let two callers reach it under different principals and
    different allowlists, with read order deciding which applied."""
    record_binding(configured, _binding(binding_id="b-second",
                                        identity={"database": "dr_warehouse"}))
    count = configured.execute(
        "SELECT count(*) FROM physical_dataset_binding WHERE catalog_source = 'ftr'").fetchone()[0]
    assert count == 1
    binding, _ = resolve_binding(configured, catalog_source="ftr", table="tran_repos")
    assert binding.identity.database == "dr_warehouse"


# ── one catalog is one engine ────────────────────────────────────────────────────────────────────

def _route(db, *, engine="hive", tier="edp", schemas=("dpl_eib",), active=True,
           env="dev", database="edp_cluster"):
    record_connection(db, _connection(connection_id="route-1", environment_id=env,
                                      kind=engine, allowed_schemas=frozenset(schemas),
                                      active=active),
                      tier=tier, database_name=database)


def _catalog_row(db, *, source="ftr", schema="dpl_eib", table="tran_repos"):
    """One graph_node row is enough: resolution needs the table's REAL schema, which is the
    namespace half of the address."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  schema_name) VALUES (%s,%s,'column',%s,'cif_id',%s) "
        "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
        (source, f"public.{table}.cif_id", table, schema))


def test_a_table_is_addressable_with_NO_per_table_binding(db):
    """THE point. A real EDP has thousands of tables and one engine; binding each individually is
    correct and unmaintainable. The instance comes from the connection, the namespace from the row."""
    _route(db)
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp",
                           declared_by="priya")

    resolved = resolve_table(db, catalog_source="ftr", table="tran_repos")
    assert resolved is not None, "a declared catalog with a routed connection did not resolve"
    binding, connection = resolved
    assert binding.identity.schema == "dpl_eib"        # from the catalog
    assert binding.identity.table == "tran_repos"
    assert connection.connection_id == "route-1"       # from the route
    assert binding.identity.database == "edp_cluster"  # identity only; never in SQL


def test_an_explicit_binding_WINS_over_the_catalog_rule(db):
    """The exception mechanism must beat the general rule or it is not an exception — a table pointed
    at a snapshot, or mid-migration."""
    _route(db)
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    record_connection(db, _connection())                      # the 1037-style explicit pair
    record_binding(db, _binding(identity={"database": "snapshot_2026_06"}))

    binding, _ = resolve_table(db, catalog_source="ftr", table="tran_repos")
    assert binding.identity.database == "snapshot_2026_06"


def test_an_UNDECLARED_catalog_does_not_resolve(db):
    _route(db)
    _catalog_row(db)
    assert resolve_table(db, catalog_source="ftr", table="tran_repos") is None


def test_a_declared_catalog_with_NO_routed_connection_does_not_resolve(db):
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    assert resolve_table(db, catalog_source="ftr", table="tran_repos") is None


def test_the_ENVIRONMENT_is_a_hard_match(db):
    """A UAT deployment resolving a production connection returns a plausible answer from the wrong
    data, and nothing in the result says which cluster it came from."""
    _route(db, env="prod")                                     # deployment env is 'dev'
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    assert resolve_table(db, catalog_source="ftr", table="tran_repos") is None


def test_the_TIER_routes_separately_from_the_engine(db):
    """hive+edp and hive+ods are different clusters. Matching on engine alone would send an ODS
    question to the EDP."""
    _route(db, tier="ods")
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    assert resolve_table(db, catalog_source="ftr", table="tran_repos") is None


def test_a_derived_binding_is_STILL_not_self_authorizing(db):
    """Derived is not privileged: the connection's allowlist decides, exactly as for a stored row."""
    _route(db, schemas=("something_else",))
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    with pytest.raises(ConnectionError_, match="allowlist"):
        resolve_table(db, catalog_source="ftr", table="tran_repos")


def test_an_INACTIVE_route_does_not_resolve(db):
    _route(db, active=False)
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    assert resolve_table(db, catalog_source="ftr", table="tran_repos") is None


def test_two_ACTIVE_routes_for_one_engine_tier_environment_are_impossible(db):
    """Two would mean the same catalog resolves to different clusters depending on read order — and
    in a bank the two candidates are UAT and production."""
    import psycopg

    _route(db)
    with pytest.raises(psycopg.errors.UniqueViolation):
        # SAME (engine, tier, environment) — the first version of this test varied the environment
        # and so proved nothing.
        record_connection(db, _connection(connection_id="route-2", kind="hive",
                                          environment_id="dev"),
                          tier="edp", database_name="other")

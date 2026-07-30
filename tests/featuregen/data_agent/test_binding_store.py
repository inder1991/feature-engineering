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
    read_connection,
    record_binding,
    record_connection,
    resolve_binding,
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

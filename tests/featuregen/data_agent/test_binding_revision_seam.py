"""Release-B Task 7 — the binding/revision reconciliation.

The defect this closes: `resolve_table`'s derived branch returned an in-memory binding that was
never recorded anywhere, while `record_binding` was a flat upsert that wrote no revision and the
append-only revision writer lived in another module. A `DatasetSourceSelectionV1` naming that
binding's `selected_binding_revision_id` would have referenced a row that does not exist.

THE PIN: the explicit and derived paths cannot fork identity. `select_table_binding` called twice
goes down the derived branch and then the explicit one — and must produce the SAME deterministic
`pbr_` revision id, with exactly ONE revision row.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.binding_store import (
    binding_revision_exists,
    binding_revision_id_of,
    declare_catalog_engine,
    record_binding,
    record_connection,
    resolve_table,
    select_table_binding,
)
from featuregen.data_agent.connection import ConnectionError_, DataSourceConnectionV1
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.overlay.upload.source_selection import (
    SelectionError,
    normalize_binding_revision_id,
)


def _connection(**over) -> DataSourceConnectionV1:
    kw = dict(connection_id="route-1", environment_id="dev", kind="hive",
              host="hiveserver2.internal", port=10000, auth_mechanism="kerberos",
              secret_ref="vault://featuregen/hive", execution_principal="svc_ro",
              allowed_schemas=frozenset({"dpl_eib"}), active=True)
    kw.update(over)
    return DataSourceConnectionV1(**kw)


def _catalog_row(db, *, source="ftr", schema="dpl_eib", table="tran_repos"):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  schema_name) VALUES (%s,%s,'column',%s,'cif_id',%s) "
        "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
        (source, f"public.{table}.cif_id", table, schema))


@pytest.fixture
def routed(db):
    """A catalog with a declared engine and a routed connection — the DERIVED-binding shape (no
    per-table row), which is the realistic one: a real EDP has thousands of tables and one engine."""
    record_connection(db, _connection(), tier="edp", database_name="edp_cluster")
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    return db


def _revisions(db) -> list[tuple[str, str, str]]:
    return [tuple(r) for r in db.execute(
        "SELECT binding_revision_id, binding_id, physical_id "
        "FROM physical_dataset_binding_revision ORDER BY binding_revision_id").fetchall()]


# ── the fork pin ────────────────────────────────────────────────────────────────────────────────

def test_a_derived_binding_is_persisted_the_first_time_a_plan_selects_it(routed):
    """"Persisted when first selected, before any decision/observation/snapshot references it."""
    assert _revisions(routed) == []
    selected = select_table_binding(routed, catalog_source="ftr", table="tran_repos",
                                    recorded_by="areq-1")
    assert selected is not None
    binding, connection, revision_id = selected
    assert connection.connection_id == "route-1"
    assert revision_id == binding.binding_revision_id
    assert binding_revision_exists(routed, revision_id) is True
    assert routed.execute(
        "SELECT count(*) FROM physical_dataset_binding WHERE catalog_source = 'ftr'"
    ).fetchone()[0] == 1


def test_the_derived_and_explicit_paths_cannot_fork_identity(routed):
    """THE pin. The second call resolves through the EXPLICIT branch (the row now exists) and must
    reconstruct byte-identical content — same address, same connection, same layout — therefore the
    same content hash and the same `pbr_` id. A single revision row, twice recorded."""
    _binding_a, _conn_a, first = select_table_binding(routed, catalog_source="ftr",
                                                      table="tran_repos")
    _binding_b, _conn_b, second = select_table_binding(routed, catalog_source="ftr",
                                                       table="tran_repos")
    assert first == second
    assert len(_revisions(routed)) == 1
    # And the second resolve really did take the explicit branch.
    binding, _ = resolve_table(routed, catalog_source="ftr", table="tran_repos")
    assert binding.binding_id == "derived-ftr-tran_repos"
    assert binding_revision_id_of(binding) == first


def test_the_physical_id_is_the_one_from_the_identity_not_a_second_derivation(routed):
    """`physical_id` is written from `PhysicalObjectIdentityV1.table_id` — the ONE computation.
    A store composing its own address string is how the two writers drifted apart."""
    binding, _connection, revision_id = select_table_binding(
        routed, catalog_source="ftr", table="tran_repos")
    ((stored_revision, stored_binding_id, stored_physical_id),) = _revisions(routed)
    assert stored_revision == revision_id
    assert stored_binding_id == binding.binding_id
    assert stored_physical_id == binding.identity.table_id == "ftr::edp_cluster::dpl_eib::tran_repos"


def test_recording_the_same_binding_twice_is_idempotent_on_both_rows(db):
    binding = PhysicalDatasetBindingV1(
        binding_id="b-tran-repos", catalog_logical_ref="ftr::dpl_eib.tran_repos",
        connection_id="route-1",
        identity=PhysicalObjectIdentityV1(catalog_source="ftr", database="prod_warehouse",
                                          schema="dpl_eib", table="tran_repos",
                                          object_kind="table"))
    record_connection(db, _connection())
    record_binding(db, binding)
    record_binding(db, binding, recorded_by="someone_else")
    assert db.execute("SELECT count(*) FROM physical_dataset_binding").fetchone()[0] == 1
    assert len(_revisions(db)) == 1


def test_an_explicit_rebinding_appends_a_revision_and_never_rewrites_the_old_one(db):
    """The address row is one-per-table (a second would let two callers reach it under different
    principals); the REVISION history is append-only, so a decision pinned to the old address stays
    resolvable after the table is re-pointed."""
    record_connection(db, _connection())
    original = PhysicalDatasetBindingV1(
        binding_id="b-tran-repos", catalog_logical_ref="ftr::dpl_eib.tran_repos",
        connection_id="route-1",
        identity=PhysicalObjectIdentityV1(catalog_source="ftr", database="prod_warehouse",
                                          schema="dpl_eib", table="tran_repos",
                                          object_kind="table"))
    record_binding(db, original)
    moved = PhysicalDatasetBindingV1(
        binding_id="b-tran-repos", catalog_logical_ref="ftr::dpl_eib.tran_repos",
        connection_id="route-1",
        identity=PhysicalObjectIdentityV1(catalog_source="ftr", database="snapshot_2026_06",
                                          schema="dpl_eib", table="tran_repos",
                                          object_kind="table"))
    record_binding(db, moved)
    assert db.execute("SELECT count(*) FROM physical_dataset_binding").fetchone()[0] == 1
    assert {r[0] for r in _revisions(db)} == {original.binding_revision_id,
                                              moved.binding_revision_id}
    assert binding_revision_exists(db, original.binding_revision_id) is True


# ── what a selection may reference ──────────────────────────────────────────────────────────────

def test_a_selection_may_only_reference_a_PERSISTED_revision(routed):
    """The contract validates the `pbr_` SHAPE; only the store can answer whether the row exists.
    Both halves are needed: a typo is caught without a database, and a plausible-but-absent id is
    caught before a decision names it."""
    _binding, _connection, revision_id = select_table_binding(routed, catalog_source="ftr",
                                                              table="tran_repos")
    assert normalize_binding_revision_id(revision_id) == revision_id
    assert binding_revision_exists(routed, revision_id) is True

    plausible = "pbr_" + "f" * 64
    assert normalize_binding_revision_id(plausible) == plausible       # shape is fine
    assert binding_revision_exists(routed, plausible) is False  # the row is not
    with pytest.raises(SelectionError, match="physical binding revision id"):
        normalize_binding_revision_id("derived-ftr-tran_repos")


# ── selection does not paper over governance ────────────────────────────────────────────────────

def test_an_unaddressable_table_records_nothing_and_stays_an_absence(db):
    _catalog_row(db)
    assert select_table_binding(db, catalog_source="ftr", table="tran_repos") is None
    assert _revisions(db) == []


def test_a_revoked_grant_still_raises_rather_than_being_recorded(db):
    """"Not configured" must never absorb "no longer allowed" — and a refused binding must not
    leave a revision behind that a later decision could reference."""
    record_connection(db, _connection(allowed_schemas=frozenset({"something_else"})),
                      tier="edp", database_name="edp_cluster")
    _catalog_row(db)
    declare_catalog_engine(db, catalog_source="ftr", engine="hive", tier="edp", declared_by="p")
    with pytest.raises(ConnectionError_, match="allowlist"):
        select_table_binding(db, catalog_source="ftr", table="tran_repos")
    assert _revisions(db) == []

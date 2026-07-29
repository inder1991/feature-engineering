"""Migration 1034 — the APPEND-ONLY materialization control plane (Spec §12, plan Task 14 step 1).

The control plane is the record of *what was published*. A record that can be rewritten proves
nothing, so all seven tables reject UPDATE, DELETE **and TRUNCATE**.

**Why TRUNCATE needs its own trigger, and why these tests use `CASCADE`.** A ``FOR EACH ROW``
trigger does not fire on TRUNCATE at all — measured on the server this suite runs against, a table
carrying a row-level UPDATE/DELETE/INSERT trigger was silently emptied by ``TRUNCATE``. So each
table also carries a statement-level ``BEFORE TRUNCATE … FOR EACH STATEMENT`` trigger.

A second measured fact shapes every TRUNCATE assertion here: a bare ``TRUNCATE parent`` on a table
referenced by a foreign key fails with ``FeatureNotSupported`` ("cannot truncate a table referenced
in a foreign key constraint") **before** any BEFORE-TRUNCATE trigger fires. A test that asserted
only "TRUNCATE raises" on such a table would therefore pass with **no trigger installed at all** —
it would be measuring the FK graph, not the guard. Every TRUNCATE test below uses ``CASCADE`` (which
does reach the trigger) and asserts the guard's OWN exception class and message, so removing the
statement trigger makes it fail.

The plan names six tables and Task 10 records that "the ``group_binding`` **tables**" land here —
plural, because §10.1's binding is two records: the write-once ``group_binding`` and the appended
``group_plan_revision``. Both are covered.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import featuregen.db.migrations as _migrations
from featuregen.materialize.control_plane import (
    TERMINAL_RUN_EVENT_KINDS,
    RunEventKind,
    RunStatus,
)

MIGRATION_NAME = "1034_materialization_control_plane"

#: Every table the migration creates. Listed explicitly (the migration installs its guards from a
#: loop) so a table dropped from the migration's loop cannot also vanish from what is asserted.
CONTROL_PLANE_TABLES = (
    "materialization_generation",
    "pipeline_validation_report",
    "materialization_run_event",
    "materialization_run_manifest",
    "publication_capability_attestation",
    "group_binding",
    "group_plan_revision",
)


def _migration_sql() -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / f"{MIGRATION_NAME}.sql").read_text(encoding="utf-8")


# ── seeders: one valid row per table, in dependency order ────────────────────────────────────────

GEN = "gen-1034"
RUN = "run-1034"
ATT = "att-1034"
BND = "bnd-1034"
NOW = "2026-07-28T10:00:00+00:00"


def _generation(conn, generation_id: str = GEN) -> str:
    conn.execute(
        "INSERT INTO materialization_generation (generation_id, logical_group_name, "
        "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
        "VALUES (%s, 'cif_daily', 'ct-hash', 'gp-hash', 'proj-hash', %s)",
        (generation_id, NOW))
    return generation_id


def _attestation(conn, attestation_id: str = ATT) -> str:
    conn.execute(
        "INSERT INTO publication_capability_attestation (attestation_id, environment_id, "
        "hive_version, spark_version, metastore_version, mechanism, passed, "
        "covers_schema_evolution, evidence_hash, attested_at) "
        "VALUES (%s, 'env-1', '3.1.3', '4.2.0', '3.1.3', 'VERSIONED_POINTER', true, true, "
        "'ev-hash', %s)",
        (attestation_id, NOW))
    return attestation_id


def _binding(conn, binding_id: str = BND, *, logical_group_name: str = "cif_daily") -> str:
    conn.execute(
        "INSERT INTO group_binding (binding_id, logical_group_name, "
        "materialization_contract_hash, physical_target) VALUES (%s, %s, 'ct-hash', %s)",
        (binding_id, logical_group_name, f"sandbox_feature.{logical_group_name}"))
    return binding_id


def _plan_revision(conn, *, binding_id: str = BND, generation_id: str = GEN) -> None:
    conn.execute(
        "INSERT INTO group_plan_revision (binding_id, generation_id, group_plan_hash, created_at) "
        "VALUES (%s, %s, 'gp-hash', %s)", (binding_id, generation_id, NOW))


def _report(conn, report_id: str = "rep-1034", *, generation_id: str = GEN,
            level: str = "L1", status: str = "passed", findings: str = "[]") -> None:
    conn.execute(
        "INSERT INTO pipeline_validation_report (report_id, generation_id, run_id, "
        "generated_project_hash, group_plan_hash, level, environment_id, status, started_at, "
        "finished_at, findings) VALUES (%s, %s, %s, 'proj-hash', 'gp-hash', %s, 'env-1', %s, "
        "%s, %s, %s::jsonb)",
        (report_id, generation_id, RUN, level, status, NOW, NOW, findings))


def _event(conn, *, run_id: str = RUN, seq: int = 0,
           kind: str = RunEventKind.RUN_PREPARED, generation_id: str = GEN) -> None:
    conn.execute(
        "INSERT INTO materialization_run_event (run_id, seq, generation_id, event_kind, "
        "occurred_at) VALUES (%s, %s, %s, %s, %s)", (run_id, seq, generation_id, str(kind), NOW))


def _manifest(conn, *, run_id: str = RUN, generation_id: str = GEN,
              attestation_id: str = ATT, status: str = "published") -> None:
    published = status == "published"
    conn.execute(
        "INSERT INTO materialization_run_manifest (run_id, generation_id, group_plan_hash, "
        "materialization_contract_hash, generated_project_hash, sandbox_execution_hash, "
        "business_dt, publication_mechanism, capability_attestation_id, expected_feature_columns, "
        "staged_row_count, published_row_count, schema_hash, key_uniqueness_result, "
        "required_column_result, orphan_grain_key_count, publication_location, started_at, "
        "published_at, status) VALUES (%s, %s, 'gp-hash', 'ct-hash', 'proj-hash', 'exec-hash', "
        "'2026-07-27', 'VERSIONED_POINTER', %s, %s, 10, 10, 'schema-hash', 'unique', 'present', "
        "0, %s, %s, %s, %s)",
        (run_id, generation_id, attestation_id, ["total_debit_amount_30d"],
         "sandbox_feature.cif_daily/gen-1034" if published else None,
         NOW, NOW if published else None, status))


#: One valid row per table, so every guard test runs against a table that actually has a row —
#: a guard "proved" against an empty table proves nothing about what it protects.
_SEEDERS = {
    "materialization_generation": lambda c: _generation(c),
    "publication_capability_attestation": lambda c: _attestation(c),
    "group_binding": lambda c: _binding(c),
    "group_plan_revision": lambda c: (_generation(c), _binding(c), _plan_revision(c)),
    "pipeline_validation_report": lambda c: (_generation(c), _report(c)),
    "materialization_run_event": lambda c: (_generation(c), _event(c)),
    "materialization_run_manifest": lambda c: (_generation(c), _attestation(c), _manifest(c)),
}

#: A column that exists on every table and is safe to aim an UPDATE at.
_UPDATABLE_COLUMN = {
    "materialization_generation": "generated_project_hash",
    "publication_capability_attestation": "evidence_hash",
    "group_binding": "materialization_contract_hash",
    "group_plan_revision": "group_plan_hash",
    "pipeline_validation_report": "status",
    "materialization_run_event": "detail",
    "materialization_run_manifest": "schema_hash",
}


def _seed(conn, table: str) -> None:
    _SEEDERS[table](conn)


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _check_literals(conn, constraint_name: str) -> set[str]:
    """The string literals a named CHECK admits, read back from the catalogue.

    Looked up BY NAME rather than by matching the definition text: two constraints on one table can
    both mention `status`, and picking whichever the planner returned first would compare the wrong
    one — which is how the first draft of this test passed for the wrong reason.
    """
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (constraint_name,)).fetchone()
    assert row is not None, f"no CHECK constraint named {constraint_name}"
    inside = row[0].split("ARRAY[")[1].split("]")[0]
    return {literal.split("::")[0].strip().strip("'") for literal in inside.split(",")}


# ── 1. the tables exist, and the migration is in the ledger ──────────────────────────────────────


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_control_plane_table_exists(conn, table: str) -> None:
    assert conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is not None


def test_the_migration_is_applied_by_apply_migrations(conn) -> None:
    """The session fixture ran `apply_migrations`; the ledger row proves the file was picked up
    under the number it claims, rather than the tables having appeared by some other route."""
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s",
                        (MIGRATION_NAME,)).fetchone() is not None


# ── 2. append-only: UPDATE, DELETE and TRUNCATE ──────────────────────────────────────────────────


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_update_is_blocked(conn, table: str) -> None:
    _seed(conn, table)
    column = _UPDATABLE_COLUMN[table]
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(f"UPDATE {table} SET {column} = 'tampered'")
    assert _count(conn, table) == 1


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_delete_is_blocked(conn, table: str) -> None:
    _seed(conn, table)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(f"DELETE FROM {table}")
    assert _count(conn, table) == 1


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_truncate_is_blocked_and_the_row_survives(conn, table: str) -> None:
    """The trap: a FOR EACH ROW trigger does not fire on TRUNCATE, so a table that blocks UPDATE and
    DELETE can still be silently emptied. CASCADE is used because a bare TRUNCATE of an
    FK-referenced parent raises FeatureNotSupported *before* triggers run — which would let this
    test pass with no guard at all."""
    _seed(conn, table)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(f"TRUNCATE {table} CASCADE")
    assert _count(conn, table) == 1, f"{table} was emptied by TRUNCATE"


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_the_truncate_guard_is_a_STATEMENT_level_trigger(conn, table: str) -> None:
    """Structural companion to the behaviour test: pg_trigger's tgtype must show a BEFORE TRUNCATE
    trigger that is NOT row-level (bit 0 = ROW, bit 1 = BEFORE, bit 5 = TRUNCATE). A row-level
    trigger that merely *names* TRUNCATE cannot be created by PostgreSQL, and one that names
    UPDATE/DELETE never sees a TRUNCATE — so the absence of this trigger is the absence of the
    guard."""
    rows = conn.execute(
        "SELECT tgname, tgtype FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relname = %s AND NOT t.tgisinternal", (table,)).fetchall()
    truncate = [(name, tgtype) for name, tgtype in rows if tgtype & 32]
    assert truncate, f"{table} has no TRUNCATE trigger: {rows}"
    for name, tgtype in truncate:
        assert tgtype & 2, f"{name} is not a BEFORE trigger (tgtype={tgtype})"
        assert not tgtype & 1, f"{name} is FOR EACH ROW, which never fires on TRUNCATE"

    row_guard = [(name, tgtype) for name, tgtype in rows if tgtype & 1]
    assert row_guard, f"{table} has no row-level UPDATE/DELETE guard: {rows}"
    for name, tgtype in row_guard:
        assert tgtype & 8 and tgtype & 16, f"{name} does not cover both UPDATE and DELETE"


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_INSERT_still_works(conn, table: str) -> None:
    """The must-survive control. Append-only means appends still happen; a guard that blocked
    INSERT would make every one of the tests above pass for the wrong reason."""
    _seed(conn, table)
    assert _count(conn, table) == 1


@pytest.mark.parametrize("table", CONTROL_PLANE_TABLES)
def test_destructive_dml_is_revoked_from_the_app_role(db, table: str) -> None:
    """Defence in depth, mirroring 1012: production runs as the NON-superuser `featuregen_app`, and
    a superuser can suppress ORIGIN triggers (`session_replication_role = replica`). The role is
    absent in the test cluster, so the guarded REVOKE is exercised by creating it first."""
    db.execute("CREATE ROLE featuregen_app NOLOGIN")
    db.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} TO featuregen_app")
    db.execute(_migration_sql())
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                          (table, priv)).fetchone()[0] is False, \
            f"{priv} on {table} must be revoked from featuregen_app"
    for priv in ("SELECT", "INSERT"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                          (table, priv)).fetchone()[0] is True, \
            f"{priv} on {table} must survive the revoke"


# ── 3. the constraints the plan names ────────────────────────────────────────────────────────────


def test_run_id_and_seq_are_unique_on_events(conn) -> None:
    _generation(conn)
    _event(conn, seq=0)
    _event(conn, seq=1)                       # a second event in the same run is fine
    _event(conn, run_id="other-run", seq=0)   # …as is seq 0 in a different run
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _event(conn, seq=1, kind=RunEventKind.RUN_SUBMITTED)


def test_event_kind_is_a_CLOSED_vocabulary(conn) -> None:
    _generation(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO materialization_run_event (run_id, seq, generation_id, event_kind, "
            "occurred_at) VALUES ('r', 0, %s, 'MADE_UP_KIND', %s)", (GEN, NOW))


def test_every_RunEventKind_is_accepted_and_the_CHECK_matches_the_enum_EXACTLY(conn) -> None:
    """`==`, never `>=`: the SQL CHECK and the Python enum are two spellings of one closed
    vocabulary, and a CHECK that admitted a kind the fold cannot classify would store a run state
    nothing could read back."""
    _generation(conn)
    for seq, kind in enumerate(RunEventKind):
        _event(conn, run_id=f"run-{kind}", seq=seq, kind=kind)

    assert _check_literals(conn, "materialization_run_event_kind_is_closed") == {
        kind.value for kind in RunEventKind}


def test_the_manifest_status_CHECK_matches_the_TERMINAL_statuses_EXACTLY(conn) -> None:
    """A manifest is written once, at the end (§12), so its status is a terminal one. A manifest
    claiming `submitted` would be a terminal record of a run that had not finished."""
    assert _check_literals(conn, "materialization_run_manifest_status_is_terminal") == {
        status.value for status in RunStatus if status.is_terminal()}


@pytest.mark.parametrize(
    ("table", "insert"),
    [("materialization_run_event", lambda c: _event(c, generation_id="no-such-gen")),
     ("materialization_run_manifest", lambda c: _manifest(c, generation_id="no-such-gen")),
     ("pipeline_validation_report", lambda c: _report(c, generation_id="no-such-gen")),
     ("group_plan_revision", lambda c: _plan_revision(c, generation_id="no-such-gen"))])
def test_every_record_FKs_to_its_generation(conn, table: str, insert) -> None:
    """§12: the manifest names "the exact generation". An unresolvable generation_id would make the
    whole record unattributable to any compilation."""
    _generation(conn)
    _attestation(conn)
    _binding(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        insert(conn)


def test_exactly_one_terminal_manifest_per_run(conn) -> None:
    _generation(conn)
    _attestation(conn)
    _manifest(conn)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _manifest(conn, status="failed")


def test_at_most_one_TERMINAL_event_per_run(conn) -> None:
    """The fold derives status from events; two terminal events would make "current status" a
    choice rather than a reading."""
    _generation(conn)
    _event(conn, seq=0, kind=RunEventKind.RUN_PREPARED)
    _event(conn, seq=1, kind=RunEventKind.PUBLISHED)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _event(conn, seq=2, kind=RunEventKind.RUN_FAILED)
    # …but two terminal events in DIFFERENT runs are ordinary.
    _event(conn, run_id="other", seq=0, kind=RunEventKind.RUN_FAILED)
    assert TERMINAL_RUN_EVENT_KINDS  # the partial index is keyed by exactly this set


def test_the_manifest_FKs_to_its_capability_attestation(conn) -> None:
    """§10.3: nothing publishes without a passing attestation for the exact environment. A manifest
    naming an attestation that does not exist would record a publication with no proven capability."""
    _generation(conn)
    _attestation(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _manifest(conn, attestation_id="no-such-attestation")


def test_a_published_manifest_must_say_where_when_and_how_many(conn) -> None:
    _generation(conn)
    _attestation(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO materialization_run_manifest (run_id, generation_id, group_plan_hash, "
            "materialization_contract_hash, generated_project_hash, sandbox_execution_hash, "
            "business_dt, publication_mechanism, capability_attestation_id, "
            "expected_feature_columns, status) VALUES ('r', %s, 'g', 'c', 'p', 'e', "
            "'2026-07-27', 'VERSIONED_POINTER', %s, %s, 'published')",
            (GEN, ATT, ["f"]))


def test_one_binding_per_logical_group_name(conn) -> None:
    """§10.1: the binding is written ONCE per logical name. A second binding for the same name is
    exactly the silent replacement GROUP_BINDING_CONFLICT exists to refuse."""
    _binding(conn, "b1")
    _binding(conn, "b2", logical_group_name="other_daily")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _binding(conn, "b3")


def test_an_error_report_may_carry_no_findings(conn) -> None:
    """§11.2: an unreachable cluster yields status="error" with ZERO findings — never invented
    ones. Made physical, so an implementation cannot fabricate one."""
    _generation(conn)
    _report(conn, "rep-ok", status="error", findings="[]")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _report(conn, "rep-bad", status="error", findings='[{"code": "PARTITION_ABSENT"}]')


def test_validation_report_level_is_closed(conn) -> None:
    _generation(conn)
    for level in ("L0", "L1", "L2"):
        _report(conn, f"rep-{level}", level=level)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _report(conn, "rep-l9", level="L9")


# ── 4. re-application against a control plane that ALREADY HOLDS ROWS ─────────────────────────────


def test_reapplying_over_a_populated_control_plane_preserves_every_row(db) -> None:
    """CI can never catch a migration that aborts on EXISTING rows: `apply_migrations` always runs
    against a fresh database. This migration creates only new tables, so the one legacy shape it
    can meet is a control plane that is already populated — a re-run must neither abort nor destroy
    what is recorded, and must leave the guards armed."""
    _generation(db)
    _attestation(db)
    _binding(db)
    _plan_revision(db)
    _report(db)
    _event(db)
    _manifest(db)
    before = {table: _count(db, table) for table in CONTROL_PLANE_TABLES}
    assert all(count == 1 for count in before.values()), before

    db.execute(_migration_sql())
    db.execute(_migration_sql())   # …and again: the file must be re-runnable, as the repo requires

    assert {table: _count(db, table) for table in CONTROL_PLANE_TABLES} == before
    for table in CONTROL_PLANE_TABLES:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), db.transaction():
            db.execute(f"TRUNCATE {table} CASCADE")

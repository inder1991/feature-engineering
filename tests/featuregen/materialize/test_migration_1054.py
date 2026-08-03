"""Migration 1054 — ``materialization_compiled_artifact``: what a run INTENDED to read survives it.

Phase G §3.6 item 2. Before 1054 a compile left only *hashes* behind — ``group_plan_hash`` and
``materialization_contract_hash`` in the plane, ``generated_project_hash`` in ``GENERATED.lock`` —
and the bodies those hashes name had no writer at all. A human holding a ``group_plan_hash`` could
not answer "which features, which columns, which spine?" without re-deriving the whole compile, and
§3.3's reconciler had no compile-side evidence to reconcile against.

**This table is EVIDENCE, not coordination**, and these tests assert the difference rather than
describing it:

* ``materialization_request`` (1053) is a *mutable coordination record* — accepted, leased, renewed,
  linked — and carries no append-only guard, because a row that could not be updated could not carry
  a lease. :func:`test_the_request_table_is_NOT_append_only_and_that_is_the_point` proves it there.
* ``materialization_compiled_artifact`` describes what WAS compiled. It can never be right to
  change it afterwards, so it takes 1034's guards — UPDATE, DELETE **and TRUNCATE** — and takes them
  from 1034's OWN function rather than a second copy
  (:func:`test_the_append_only_guard_is_1034s_SHARED_function`).

**The legacy-replay test is the only guard CI has.** ``apply_migrations`` runs on a fresh database in
CI, so nothing there can catch a migration that aborts (or silently rewrites) an existing plane.
:func:`test_1054_applies_cleanly_over_a_POPULATED_control_plane` drops the table inside the test's
own (rolled-back) transaction to recreate a genuine pre-migration state, seeds a realistic plane in
1034's dependency order — generation, attestation, binding, plan revision, an ordered run-event
stream ending in a terminal event, a validation report and a terminal manifest — and only then
applies 1054. It deliberately does NOT drop ``materialization_control_plane_append_only()``: that
function is 1034's, and dropping it would remove the guards from seven tables this migration does
not own.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import featuregen.db.migrations as _migrations
from featuregen.materialize.control_plane import RunEventKind

MIGRATION_NAME = "1054_materialization_compiled_artifact"
PLANE_MIGRATION = "1034_materialization_control_plane"
ORDERING_MIGRATION = "1044_run_event_ordering"

TABLE = "materialization_compiled_artifact"
GEN = "gen-1054"
RUN = "run-1054"
ATT = "att-1054"
BND = "bnd-1054"
NOW = "2026-08-03T10:00:00+00:00"

#: The plane tables the legacy-replay test seeds and then proves untouched.
PLANE_TABLES = (
    "materialization_generation",
    "publication_capability_attestation",
    "group_binding",
    "group_plan_revision",
    "pipeline_validation_report",
    "materialization_run_event",
    "materialization_run_manifest",
)


def _migration_sql(name: str) -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / f"{name}.sql").read_text(encoding="utf-8")


# ── seeders: the plane, in 1034's dependency order ───────────────────────────────────────────────


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


def _binding(conn, binding_id: str = BND) -> str:
    conn.execute(
        "INSERT INTO group_binding (binding_id, logical_group_name, "
        "materialization_contract_hash, physical_target) "
        "VALUES (%s, 'cif_daily', 'ct-hash', 'sandbox_feature.cif_daily')", (binding_id,))
    return binding_id


def _plan_revision(conn) -> None:
    conn.execute(
        "INSERT INTO group_plan_revision (binding_id, generation_id, group_plan_hash, created_at) "
        "VALUES (%s, %s, 'gp-hash', %s)", (BND, GEN, NOW))


def _report(conn) -> None:
    conn.execute(
        "INSERT INTO pipeline_validation_report (report_id, generation_id, run_id, "
        "generated_project_hash, group_plan_hash, level, environment_id, status, started_at, "
        "finished_at, findings) VALUES ('rep-1054', %s, %s, 'proj-hash', 'gp-hash', 'L1', 'env-1', "
        "'passed', %s, %s, '[]'::jsonb)", (GEN, RUN, NOW, NOW))


def _event(conn, *, seq: int, kind: str) -> None:
    conn.execute(
        "INSERT INTO materialization_run_event (run_id, seq, generation_id, event_kind, "
        "occurred_at) VALUES (%s, %s, %s, %s, %s)", (RUN, seq, GEN, str(kind), NOW))


def _manifest(conn) -> None:
    conn.execute(
        "INSERT INTO materialization_run_manifest (run_id, generation_id, group_plan_hash, "
        "materialization_contract_hash, generated_project_hash, sandbox_execution_hash, "
        "business_dt, publication_mechanism, capability_attestation_id, expected_feature_columns, "
        "staged_row_count, published_row_count, schema_hash, key_uniqueness_result, "
        "required_column_result, orphan_grain_key_count, publication_location, started_at, "
        "published_at, status) VALUES (%s, %s, 'gp-hash', 'ct-hash', 'proj-hash', 'exec-hash', "
        "'2026-07-27', 'VERSIONED_POINTER', %s, %s, 10, 10, 'schema-hash', 'unique', 'present', "
        "0, 'sandbox_feature.cif_daily/gen-1054', %s, %s, 'published')",
        (RUN, GEN, ATT, ["total_debit_amount_30d"], NOW, NOW))


def _seed_legacy_plane(conn) -> None:
    """A realistic PRE-1054 plane: every table populated, in dependency order, with a run whose
    event stream is ordered and terminal. A migration proved against an empty database proves
    nothing about the databases it will actually meet."""
    _generation(conn)
    _attestation(conn)
    _binding(conn)
    _plan_revision(conn)
    _report(conn)
    for seq, kind in enumerate((RunEventKind.RUN_PREPARED, RunEventKind.RUN_SUBMITTED,
                                RunEventKind.COMPUTATION_COMPLETED, RunEventKind.GATES_PASSED,
                                RunEventKind.PUBLISHED)):
        _event(conn, seq=seq, kind=kind)
    _manifest(conn)


def _snapshot(conn) -> dict[str, list[tuple[Any, ...]]]:
    """Every plane row, ordered deterministically — compared before and after 1054 runs."""
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in PLANE_TABLES}


# ── seeder: one artifact row, with per-test overrides ────────────────────────────────────────────

#: Bodies of the SHAPE the store writes — ``FeatureGroupPlanV1.identity_payload()`` and
#: ``MaterializationContractV1.identity_payload()``. Written literally here because this file tests
#: the TABLE: the real payloads, and the fact that they re-derive to the hashes stored beside them,
#: are `test_control_plane.py`'s subject.
_PLAN_BODY: dict[str, Any] = {
    "logical_group_name": "cif_daily",
    "materialization_contract_hash": "ct-hash",
    "entity_key_columns": ["cif_id"],
    "business_dt_column": "business_dt",
    "features": [{"column_name": "total_debit_amount_30d", "ir_hash": "ir-hash",
                  "physical_type": {"sql_type": "DECIMAL(38,6)", "nullable": True,
                                    "rounding": "half_up", "overflow": "error"}}],
    "system_columns": ["__generation_id", "__generated_project_hash", "__sandbox_execution_hash"],
    "physical_type_policy_version": 1,
}

_CONTRACT_BODY: dict[str, Any] = {
    "entity": "customer",
    "ordered_keys": ["hdfc::public.customers.cif_id"],
    "sensitivity_class": "internal",
    "publication_policy": "atomic_group",
    "classification_policy_version": 1,
    "physical_type_policy_version": 1,
}

_ARTIFACT_COLUMNS = ("generation_id", "group_plan", "group_plan_hash",
                     "materialization_contract", "contract_hash")

_DEFAULTS: dict[str, Any] = {
    "generation_id": GEN,
    "group_plan": _PLAN_BODY,
    "group_plan_hash": "gp-hash",
    "materialization_contract": _CONTRACT_BODY,
    "contract_hash": "ct-hash",
}


def _artifact(conn, **overrides: Any) -> None:
    values = {**_DEFAULTS, **overrides}
    for body in ("group_plan", "materialization_contract"):
        values[body] = Jsonb(values[body])
    columns = ", ".join(_ARTIFACT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _ARTIFACT_COLUMNS)
    conn.execute(f"INSERT INTO {TABLE} ({columns}) VALUES ({placeholders})", values)


# ── 1. the table exists under the number it claims ───────────────────────────────────────────────


def test_the_migration_is_applied_by_apply_migrations(conn) -> None:
    """The session fixture ran ``apply_migrations``; the ledger row proves the file was picked up
    under the number it claims (filename-stem keying), rather than the table existing only because
    some other file created it."""
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s",
                        (MIGRATION_NAME,)).fetchone() is not None


def test_the_compiled_artifact_table_exists(conn) -> None:
    assert conn.execute(f"SELECT to_regclass('public.{TABLE}')").fetchone()[0] is not None


def test_the_column_shape_is_pinned(conn) -> None:
    """Name and nullability for every column. NOTHING is nullable: the row is written once, at
    commit, by a compile that holds both bodies and both hashes — a nullable column here would be a
    field somebody intended to fill later, on a table whose append-only guard makes "later"
    impossible."""
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY column_name", (TABLE,)).fetchall()
    assert dict(rows) == {
        "generation_id": "NO",
        "group_plan": "NO",
        "group_plan_hash": "NO",
        "materialization_contract": "NO",
        "contract_hash": "NO",
        "recorded_at": "NO",
    }


def test_an_ordinary_artifact_inserts(conn) -> None:
    """The must-survive control: a table whose constraints refused the ordinary row would make every
    refusal test below pass for the wrong reason."""
    _generation(conn)
    _artifact(conn)
    assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 1


# ── 2. identity: one artifact, for one generation that exists ────────────────────────────────────


def test_the_generation_reference_must_resolve(conn) -> None:
    """An artifact naming a generation that does not exist describes a compile nothing recorded."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _artifact(conn)


def test_one_generation_records_exactly_ONE_artifact(conn) -> None:
    """``generation_id`` is the PRIMARY KEY. One generation compiles one plan under one contract, so
    a second artifact row would be a second answer to a question that has one — and on an append-only
    table nothing could ever say which answer was right."""
    _generation(conn)
    _artifact(conn)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _artifact(conn, group_plan_hash="gp-hash-2")


@pytest.mark.parametrize("body", ["group_plan", "materialization_contract"])
def test_a_body_must_be_a_json_OBJECT(conn, body: str) -> None:
    """Opaque in CONTENT — interpreting a plan here would make the table a second definition of one —
    but pinned in SHAPE: an array or a bare scalar is a writer that passed a fragment where the whole
    identity payload belongs, and it would re-derive to a hash nothing in the plane holds."""
    _generation(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _artifact(conn, **{body: ["not", "an", "object"]})


@pytest.mark.parametrize("hash_column", ["group_plan_hash", "contract_hash"])
def test_a_hash_may_not_be_blank(conn, hash_column: str) -> None:
    """A blank hash is what a body compares against when the comparison is meaningless: the whole
    point of storing the body beside the hash is that the two describe the same object."""
    _generation(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _artifact(conn, **{hash_column: "   "})


# ── 3. this is EVIDENCE — the three guards ───────────────────────────────────────────────────────


def test_the_artifact_refuses_UPDATE(conn) -> None:
    """It describes what WAS compiled. A record that can be rewritten proves nothing, and a rewritten
    plan body would mean §9's gates were auditable against a packing list nobody packed."""
    _generation(conn)
    _artifact(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(f"UPDATE {TABLE} SET group_plan_hash = 'rewritten'")


def test_the_artifact_refuses_DELETE(conn) -> None:
    """The other half, and it is not implied by the first: a guard installed BEFORE UPDATE only would
    leave "delete then re-insert" as a complete rewrite path."""
    _generation(conn)
    _artifact(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(f"DELETE FROM {TABLE}")


def test_the_artifact_refuses_TRUNCATE_through_its_own_STATEMENT_trigger(conn) -> None:
    """A ``FOR EACH ROW`` trigger does not fire on TRUNCATE at all (1034's header records the
    measurement), so this needs the separate statement-level guard.

    Nothing references this table, so a bare TRUNCATE is not short-circuited by 1034's
    ``FeatureNotSupported`` trap ("cannot truncate a table referenced in a foreign key constraint")
    and genuinely reaches the trigger — which is why the guard's OWN exception and message are
    asserted rather than merely "TRUNCATE raises"."""
    _generation(conn)
    _artifact(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(f"TRUNCATE {TABLE}")


def test_the_append_only_guard_is_1034s_SHARED_function(conn) -> None:
    """ONE guard definition, not a second copy. The rule ("the plane is append-only, and a record
    that can be rewritten proves nothing") is stated once in 1034; a private copy here would be a
    second place it could drift, and an operator would meet two different messages for one rule.

    Asserted by function OID, not by name: two functions may share a name across schemas, and the
    thing that matters is that the triggers on this table and on ``materialization_generation``
    execute the very same one."""
    rows = conn.execute(
        "SELECT c.relname, t.tgfoid FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND c.relname IN (%s, 'materialization_generation')",
        (TABLE,)).fetchall()
    guarded = {name for name, _ in rows}
    assert guarded == {TABLE, "materialization_generation"}, \
        f"the artifact table carries no append-only guard: {sorted(guarded)}"
    assert len({oid for _, oid in rows}) == 1, \
        "the artifact table installed its own guard function instead of sharing 1034's"


# ── 4. legacy replay — the only guard against a migration that meets real data ───────────────────


def test_1054_applies_cleanly_over_a_POPULATED_control_plane(conn) -> None:
    """CI applies migrations to a FRESH database, so nothing there can catch a migration that
    aborts (or rewrites) an existing plane. Dropping the table inside this rolled-back transaction
    recreates a genuine pre-1054 database; the plane is then seeded exactly as 1034's own seeders
    do, and only afterwards is 1054 applied.

    The artifact row inserted first is what makes the DROP load-bearing: if the table survived (a
    renamed table, a drop that quietly matched nothing), the empty-table assertion below would see
    it and this test would stop measuring a pre-migration database.

    ``materialization_control_plane_append_only()`` is deliberately NOT dropped alongside it, unlike
    1053's touch function: that function belongs to 1034 and guards seven tables 1054 does not own.
    A pre-1054 database HAS it — which is exactly why 1054 must reference it rather than define it.

    The pre-drop row hangs off its own generation so that the legacy seeding below — which mints
    ``GEN`` — is not answering a primary-key violation instead of measuring the migration.
    """
    _generation(conn, "gen-1054-pre")
    _artifact(conn, generation_id="gen-1054-pre")
    conn.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
    conn.execute(_migration_sql(PLANE_MIGRATION))
    conn.execute(_migration_sql(ORDERING_MIGRATION))
    _seed_legacy_plane(conn)
    before = _snapshot(conn)
    assert all(rows for rows in before.values()), "the pre-migration plane must not be empty"

    conn.execute(_migration_sql(MIGRATION_NAME))

    assert _snapshot(conn) == before, "1054 altered rows that belong to the append-only plane"
    assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
    _artifact(conn)


def test_re_applying_1054_over_a_populated_table_keeps_its_rows(conn) -> None:
    """The repo applies migrations by filename ledger and the suite re-executes this SQL against a
    populated database — so re-application must be a no-op, not a reset. It must also not fail: the
    guards are re-installed over rows that already exist, and a ``CREATE TRIGGER`` without
    ``OR REPLACE`` would raise the second time."""
    _generation(conn)
    _artifact(conn)
    conn.execute(_migration_sql(MIGRATION_NAME))
    assert conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 1

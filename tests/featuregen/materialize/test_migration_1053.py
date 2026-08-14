"""Migration 1053 — ``materialization_request``: a run acquires DURABLE IDENTITY before any work.

Phase G §3.2. Before 1053 a run had no database identity until someone appended an event, and
``fold_run_status`` raises on an empty stream — so a crash between "we decided to run" and the first
append left **zero trace**. This table is the anchor that exists *before* any work begins.

It is deliberately a DIFFERENT KIND OF RECORD from the control plane, and these tests assert the
difference rather than describing it:

* the plane (1034/1044) is **immutable evidence** — append-only, one terminal, ordering-triggered;
* ``materialization_request`` is a **mutable coordination record** — accepted, leased, renewed,
  linked to its generation and run. It carries NO append-only guard, and
  :func:`test_the_request_table_is_NOT_append_only_and_that_is_the_point` proves it, because a
  coordination row that could not be updated could not carry a lease at all.

**The legacy-replay test is the only guard CI has.** ``apply_migrations`` runs on a fresh database in
CI, so nothing there can catch a migration that aborts (or silently rewrites) an existing plane.
:func:`test_1053_applies_cleanly_over_a_POPULATED_control_plane` drops the table inside the test's
own (rolled-back) transaction to recreate a genuine pre-migration state, seeds a realistic plane in
1034's dependency order — generation, attestation, binding, plan revision, an ordered run-event
stream ending in a terminal event, a validation report and a terminal manifest — and only then
applies 1053.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import featuregen.db.migrations as _migrations
from featuregen.materialize.control_plane import RunEventKind
from featuregen.materialize.request_store import RequestLifecycle

MIGRATION_NAME = "1053_materialization_request"
PLANE_MIGRATION = "1034_materialization_control_plane"
ORDERING_MIGRATION = "1044_run_event_ordering"

GEN = "gen-1053"
RUN = "run-1053"
ATT = "att-1053"
BND = "bnd-1053"
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
        "finished_at, findings) VALUES ('rep-1053', %s, %s, 'proj-hash', 'gp-hash', 'L1', 'env-1', "
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
        "0, 'sandbox_feature.cif_daily/gen-1053', %s, %s, 'published')",
        (RUN, GEN, ATT, ["total_debit_amount_30d"], NOW, NOW))


def _seed_legacy_plane(conn) -> None:
    """A realistic PRE-1053 plane: every table populated, in dependency order, with a run whose
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
    """Every plane row, ordered deterministically — compared before and after 1053 runs."""
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in PLANE_TABLES}


# ── seeder: one request row, with per-test overrides ─────────────────────────────────────────────

_REQUEST_COLUMNS = ("request_id", "logical_group_name", "requested_by", "authorized_roles",
                    "idempotency_key", "activation_state", "lifecycle_state", "generation_id",
                    "run_id", "resolved_input_digest", "accepted_at", "lease_expires_at")

_DEFAULTS: dict[str, Any] = {
    "request_id": "req-1053",
    "logical_group_name": "cif_daily",
    "requested_by": "analyst@bank.example",
    "authorized_roles": ["feature_engineer"],
    "idempotency_key": "idem-1053",
    "activation_state": {"OVERLAY_PASS_C": True},
    "lifecycle_state": "requested",
    "generation_id": None,
    "run_id": None,
    "resolved_input_digest": None,
    "accepted_at": None,
    "lease_expires_at": None,
}


def _request(conn, **overrides: Any) -> None:
    values = {**_DEFAULTS, **overrides}
    values["activation_state"] = Jsonb(values["activation_state"])
    columns = ", ".join(_REQUEST_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _REQUEST_COLUMNS)
    conn.execute(f"INSERT INTO materialization_request ({columns}) VALUES ({placeholders})", values)


def _check_literals(conn, constraint_name: str) -> set[str]:
    """The literal set of a closed-vocabulary CHECK, read by NAME (mirrors 1034's test): two
    constraints on one table can mention the same column, so picking whichever the planner returned
    first would compare the wrong one."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (constraint_name,)).fetchone()
    assert row is not None, f"no CHECK constraint named {constraint_name}"
    inside = row[0].split("ARRAY[")[1].split("]")[0]
    return {literal.split("::")[0].strip().strip("'") for literal in inside.split(",")}


# ── 1. the table exists under the number it claims ───────────────────────────────────────────────


def test_the_migration_is_applied_by_apply_migrations(conn) -> None:
    """The session fixture ran ``apply_migrations``; the ledger row proves the file was picked up
    under the number it claims (filename-stem keying), rather than the table existing only because
    some other file created it."""
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s",
                        (MIGRATION_NAME,)).fetchone() is not None


def test_the_request_table_exists(conn) -> None:
    assert conn.execute(
        "SELECT to_regclass('public.materialization_request')").fetchone()[0] is not None


def test_the_column_shape_is_pinned(conn) -> None:
    """Name and nullability for every column. The nullable set is the design: ``generation_id``,
    ``run_id`` and ``resolved_input_digest`` are unknown at request time — the whole reason the row
    can exist before any work does — while identity, roles, activation state and lifecycle are known
    the moment somebody asks.

    ``considered_revision_id``/``option_id`` (migration **1067**, task B4) are the governed option a
    human approved, and they are nullable for a different reason than the three above: they are not
    "unknown yet" but "not applicable" — the work-item-driven path predates the link and must keep
    working. Stated half-way they are refused, by a CHECK and by the record type; this pin is what
    made adding them a decision rather than a drift."""
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'materialization_request' ORDER BY column_name").fetchall()
    assert dict(rows) == {
        "request_id": "NO",
        "logical_group_name": "NO",
        "requested_by": "NO",
        "authorized_roles": "NO",
        "idempotency_key": "NO",
        "activation_state": "NO",
        "lifecycle_state": "NO",
        "generation_id": "YES",
        "run_id": "YES",
        "resolved_input_digest": "YES",
        "requested_at": "NO",
        "accepted_at": "YES",
        "lease_expires_at": "YES",
        "updated_at": "NO",
        "considered_revision_id": "YES",
        "option_id": "YES",
    }


def test_an_ordinary_request_inserts(conn) -> None:
    """The must-survive control: a table whose constraints refused the ordinary row would make every
    refusal test below pass for the wrong reason."""
    _request(conn)
    assert conn.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 1


# ── 2. the closed lifecycle vocabulary ───────────────────────────────────────────────────────────


def test_lifecycle_state_is_a_CLOSED_vocabulary(conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, lifecycle_state="zombie")


def test_the_lifecycle_CHECK_matches_the_python_enum_EXACTLY(conn) -> None:
    """``==``, never ``>=``: the SQL CHECK and :class:`RequestLifecycle` are two spellings of one
    closed set, and a CHECK admitting a state the transition table cannot classify would store a
    request nothing could advance."""
    assert _check_literals(conn, "materialization_request_lifecycle_is_closed") == {
        state.value for state in RequestLifecycle}


@pytest.mark.parametrize("state", [state.value for state in RequestLifecycle])
def test_every_lifecycle_state_is_accepted(conn, state: str) -> None:
    """The must-survive control for the vocabulary: every member stores. ``accepted`` is seeded WITH
    its lease because the state means claimed-and-leased (see below); the rest keep whatever the
    lifecycle left them."""
    _generation(conn)
    _request(conn, lifecycle_state=state, run_id=RUN, generation_id=GEN,
             accepted_at=None if state == "requested" else NOW,
             lease_expires_at=NOW if state == "accepted" else None)


def test_an_accepted_row_MUST_carry_its_acceptance_and_its_lease(conn) -> None:
    """The converse of the lease-follows-acceptance rule, and the one that matters more: an
    ``accepted`` row with no lease is non-terminal AND invisible to the reconciler's query (which
    requires ``lease_expires_at IS NOT NULL``) — a run nobody works and nobody looks at again. The
    store routes acceptance through the one call that carries a lease duration; this CHECK is what
    stops any other writer opening a second, lease-less door into the state.

    The properly-leased row goes in FIRST for two reasons: it is the must-survive control (a CHECK
    that refused a real acceptance would make the refusals below meaningless), and it opens the
    test's transaction, so the ``conn.transaction()`` blocks are SAVEPOINTs. Outermost, they commit
    on clean exit — which is invisible while the CHECK works, and leaks a row into the session's
    database for every later test the moment it stops working."""
    _request(conn, request_id="req-1053-leased", idempotency_key="idem-1053-leased",
             lifecycle_state="accepted", accepted_at=NOW, lease_expires_at=NOW)
    for missing in ({"accepted_at": NOW, "lease_expires_at": None},
                    {"accepted_at": None, "lease_expires_at": None}):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            _request(conn, lifecycle_state="accepted", **missing)


# ── 3. identity, uniqueness and the two references ───────────────────────────────────────────────


def test_a_duplicate_idempotency_key_is_refused_by_the_database(conn) -> None:
    """Two identical requests must not become two runs. The store returns the existing row rather
    than raising — but the database is what makes that a fact instead of a convention."""
    _request(conn)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _request(conn, request_id="req-1053-b")


def test_the_generation_reference_must_resolve(conn) -> None:
    """A request naming a generation that does not exist would attribute the run to nothing."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _request(conn, generation_id="no-such-generation")


def test_a_request_may_name_its_generation_once_it_exists(conn) -> None:
    _generation(conn)
    _request(conn, generation_id=GEN)
    assert conn.execute("SELECT generation_id FROM materialization_request "
                        "WHERE request_id = 'req-1053'").fetchone()[0] == GEN


def test_run_id_is_deliberately_NOT_a_foreign_key(conn) -> None:
    """Run identity lives in the event stream, and the request must be recordable BEFORE any event
    exists — so a request may carry a run_id for which no event has been appended yet. An FK here
    would make the anchor depend on the thing it anchors."""
    _request(conn, run_id="run-with-no-events-yet", lifecycle_state="running", accepted_at=NOW)
    assert conn.execute("SELECT count(*) FROM materialization_run_event "
                        "WHERE run_id = 'run-with-no-events-yet'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "blank_column",
    ["request_id", "logical_group_name", "requested_by", "idempotency_key"])
def test_identity_text_may_not_be_blank(conn, blank_column: str) -> None:
    """A whitespace-only actor or group name is unattributable, and NOT NULL alone permits it."""
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, **{blank_column: "   "})


@pytest.mark.parametrize("nullable_column", ["run_id", "resolved_input_digest"])
def test_optional_text_is_absent_or_real_never_blank(conn, nullable_column: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, **{nullable_column: " "})


def test_the_roles_snapshot_must_be_a_real_snapshot(conn) -> None:
    """The run is judged against the scope its requester ACTUALLY held. An empty array records that
    no snapshot was taken, not that the requester held nothing — nobody reaches this table without
    passing the trigger's permission check."""
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, authorized_roles=[])
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, authorized_roles=["feature_engineer", None])


def test_activation_state_must_be_a_json_object(conn) -> None:
    """Opaque in CONTENT, pinned in SHAPE: a bare scalar is a caller who passed the flag value where
    the flag state belongs."""
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, activation_state=True)


def test_a_lease_cannot_exist_without_an_acceptance(conn) -> None:
    """The lease is granted BY acceptance; a leased row that was never accepted would let the
    reconciler adopt a request nobody claimed."""
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _request(conn, lease_expires_at=NOW)


# ── 4. what the reconciler needs, and what makes this a coordination record ──────────────────────


def test_the_reconcilers_query_has_a_partial_index(conn) -> None:
    """The reconciler's ONLY real query is "expired lease, not terminal". The index is partial on
    purpose: committed and failed requests are the overwhelming majority and none of them is ever
    a candidate."""
    definition = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = "
        "'materialization_request_expired_lease_idx'").fetchone()
    assert definition is not None, "the reconciler's partial index is missing"
    indexed, _, predicate = definition[0].partition(" WHERE ")
    assert "(lease_expires_at, request_id)" in indexed, \
        "the index must lead with lease_expires_at and carry the tie-break, as the query orders"
    # The predicate itself, not merely "a WHERE exists": an index excluding the WRONG states would
    # hide live requests from the reconciler, which is worse than the full scan a missing predicate
    # costs. Read against the enum, so promoting a state to terminal in one place and not the other
    # fails here.
    assert predicate, "a non-partial index would scan terminal requests too"
    for state in RequestLifecycle:
        assert (f"'{state.value}'" in predicate) is state.is_terminal(), \
            f"{state.value!r} is on the wrong side of the reconciler index predicate: {predicate}"


def test_the_request_table_is_NOT_append_only_and_that_is_the_point(conn) -> None:
    """The plane is immutable evidence; this row is mutable coordination. 1034's guard loop must NOT
    have been extended to it — a row that cannot be updated cannot carry a lease, an acceptance or
    a terminal link, and conflating the two kinds of record is the design error this table exists to
    avoid."""
    _request(conn)
    conn.execute("UPDATE materialization_request SET lifecycle_state = 'accepted', "
                 "accepted_at = now(), lease_expires_at = now() + interval '5 minutes' "
                 "WHERE request_id = 'req-1053'")
    assert conn.execute("SELECT lifecycle_state FROM materialization_request "
                        "WHERE request_id = 'req-1053'").fetchone()[0] == "accepted"
    conn.execute("DELETE FROM materialization_request WHERE request_id = 'req-1053'")
    assert conn.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 0


def test_updated_at_is_stamped_by_the_DATABASE_on_every_update(conn) -> None:
    """``updated_at`` answers "is anyone still working this?", so it cannot depend on each writer
    remembering to set it. The row is seeded with a stale value that only a trigger can move."""
    _request(conn)
    conn.execute("UPDATE materialization_request SET updated_at = '2020-01-01T00:00:00+00:00' "
                 "WHERE request_id = 'req-1053'")
    conn.execute("UPDATE materialization_request SET lifecycle_state = 'accepted', "
                 "accepted_at = now(), lease_expires_at = now() + interval '5 minutes' "
                 "WHERE request_id = 'req-1053'")
    stale, updated = conn.execute(
        "SELECT updated_at = '2020-01-01T00:00:00+00:00', updated_at >= requested_at "
        "FROM materialization_request WHERE request_id = 'req-1053'").fetchone()
    assert not stale, "updated_at was not touched: a writer that forgets it makes the lease a lie"
    assert updated


# ── 5. legacy replay — the only guard against a migration that meets real data ───────────────────


def test_1053_applies_cleanly_over_a_POPULATED_control_plane(conn) -> None:
    """CI applies migrations to a FRESH database, so nothing there can catch a migration that
    aborts (or rewrites) an existing plane. Dropping the table inside this rolled-back transaction
    recreates a genuine pre-1053 database; the plane is then seeded exactly as 1034's own seeders
    do, and only afterwards is 1053 applied.

    The request row inserted first is what makes the DROP load-bearing: if the table survived (a
    renamed table, a drop that quietly matched nothing), the empty-table assertion below would see
    it and this test would stop measuring a pre-migration database. The touch FUNCTION is dropped
    too — dropping a table takes its triggers but leaves the function behind, and a "pre-migration"
    database that still held half of 1053 would let a broken function definition pass unnoticed."""
    _request(conn)
    conn.execute("DROP TABLE IF EXISTS materialization_request CASCADE")
    conn.execute("DROP FUNCTION IF EXISTS materialization_request_touch_updated_at() CASCADE")
    conn.execute(_migration_sql(PLANE_MIGRATION))
    conn.execute(_migration_sql(ORDERING_MIGRATION))
    _seed_legacy_plane(conn)
    before = _snapshot(conn)
    assert all(rows for rows in before.values()), "the pre-migration plane must not be empty"

    conn.execute(_migration_sql(MIGRATION_NAME))

    assert _snapshot(conn) == before, "1053 altered rows that belong to the append-only plane"
    assert conn.execute(
        "SELECT count(*) FROM materialization_request").fetchone()[0] == 0
    _request(conn, generation_id=GEN)


def test_re_applying_1053_over_a_populated_request_table_keeps_its_rows(conn) -> None:
    """The repo applies migrations by filename ledger and the suite re-executes this SQL against a
    populated database — so re-application must be a no-op, not a reset."""
    _request(conn)
    conn.execute(_migration_sql(MIGRATION_NAME))
    assert conn.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 1

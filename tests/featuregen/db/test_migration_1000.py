from __future__ import annotations


def _cols(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)).fetchall()}


def test_1000_adds_nullable_flag_provenance_columns(db):
    cols = _cols(db, "planner_shadow_dispatch")
    assert {"scoped_applicability_flag", "ranking_flag"} <= cols


def test_1000_flag_columns_are_nullable(db):
    rows = db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'planner_shadow_dispatch' "
        "AND column_name IN ('scoped_applicability_flag','ranking_flag')").fetchall()
    assert {c: n for c, n in rows} == {"scoped_applicability_flag": "YES", "ranking_flag": "YES"}


def test_1000_existing_rows_carry_null_provenance_and_a_new_row_can_set_it(db):
    # a legacy-shaped insert (no new columns) leaves them NULL = unprovable = fail-closed exclusion
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, applicability_version,"
        " producer_commit, compiler_versions, compiler_versions_hash, payload_schema_version)"
        " VALUES ('legacy', '{}', 'h', 0, 'p', true, true, 'v', 'c', '{}', 'ch', 'pv')")
    row = db.execute("SELECT scoped_applicability_flag, ranking_flag FROM planner_shadow_dispatch"
                     " WHERE generation_run_id='legacy'").fetchone()
    assert row == (None, None)

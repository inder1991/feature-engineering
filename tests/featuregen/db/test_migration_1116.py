"""Migration 1116: the bridge is a constraint, not a convention (spec §9)."""
import psycopg
import pytest
from tests.featuregen.runs._chain import seed_run_chain


def _insert_draft(conn, draft_id, considered_revision_id):
    conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "definition_revision, formula_identity_hash, state, requested_by, requested_at) "
        "VALUES (%s, %s, 'o1', 'p', 'c', 'a', '', %s, "
        "'REQUESTED', 'u1', '2026-08-23T00:00:00Z')",
        (draft_id, considered_revision_id, f"fih-{draft_id}"))


def _insert_selection(conn, revision_id, considered_revision_id):
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, provenance, "
        "content_hash) VALUES (%s, 'i-1116', 'exploration', 'user_typed', 'ch')",
        (f"{revision_id}-trr",))
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) "
        "VALUES (%s, %s, %s, 'o1', 'd1', 'p', 'bp', 'ch')",
        (revision_id, f"{revision_id}-trr", considered_revision_id))


def test_orphan_formula_draft_is_refused(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_draft(db, "m1116-d", "no-such-revision")


def test_orphan_feature_selection_revision_is_refused(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_selection(db, "m1116-s", "no-such-revision")


def test_a_draft_against_a_real_considered_revision_still_inserts(db):
    """The FK constrains, it does not forbid: the honest path must stay open."""
    c = seed_run_chain(db, run_id="m1116-ok")
    _insert_draft(db, "m1116-d-ok", c["considered_revision_id"])
    row = db.execute("SELECT considered_revision_id FROM formula_draft "
                     "WHERE formula_draft_id='m1116-d-ok'").fetchone()
    assert row == (c["considered_revision_id"],)


def test_a_selection_against_a_real_considered_revision_still_inserts(db):
    c = seed_run_chain(db, run_id="m1116-ok2")
    _insert_selection(db, "m1116-s-ok", c["considered_revision_id"])
    row = db.execute("SELECT considered_revision_id FROM feature_selection_revision "
                     "WHERE revision_id='m1116-s-ok'").fetchone()
    assert row == (c["considered_revision_id"],)

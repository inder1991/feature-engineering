"""The identity writer records a run's chain, or honestly records nothing (spec §6.1)."""
from psycopg.types.json import Jsonb
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.canonical import jcs_sha256
from featuregen.runs.run_identity import record_run_identity

_ENV = mint_test_identity(subject="user:priya", role_claims=("feature_engineer",), tenant="t1")


def _seed_chain_without_snapshot_pin(conn, run_id):
    """The same chain `seed_run_chain` mints, except the considered revision pins NO snapshot.

    1021 leaves both snapshot columns NULLABLE and 1027's lineage trigger explicitly permits a NULL
    `metadata_snapshot_id`, so this shape is legal in the database and the writer must refuse it
    rather than hash a half-absent chain."""
    intent_id, scope_id, recognition_id = f"{run_id}-i", f"{run_id}-s", f"{run_id}-r"
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    conn.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
                 "VALUES (%s, %s, %s, '{}')", (run_id, intent_id, Jsonb({"subject": "u1"})))
    conn.execute(
        "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, status, "
        "input_content_hash, taxonomy_version, applicability_mapping_version, recognizer_model_id, "
        "prompt_version, recipe_registry_version) "
        "VALUES (%s, %s, %s, 'resolved', 'rh', 'v1', 'v1', 'm', 'p1', 'r1')",
        (recognition_id, intent_id, f"{run_id}-ih"))
    conn.execute(
        "INSERT INTO confirmed_generation_scope (scope_id, intent_id, generation_run_id, "
        "recognition_id, expansion, scope_mode, confirmation_source, confirmed_by) "
        "VALUES (%s, %s, %s, %s, 'none', 'scoped', 'user_confirmed', 'u1')",
        (scope_id, intent_id, run_id, recognition_id))
    conn.execute(
        "INSERT INTO contract_generation_input (generation_run_id, intent_id, recognition_id, "
        "confirmed_scope_id, redacted_hypothesis, recognition_input_content_hash, "
        "generation_input_content_hash, created_by) "
        "VALUES (%s, %s, %s, %s, 'h', 'rh', 'gh', %s)",
        (run_id, intent_id, recognition_id, scope_id, Jsonb({"subject": "u1"})))
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, considered_json, considered_content_hash, canonicalization_version) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'cch', 'v1')", (f"{run_id}-ccr", intent_id, run_id))


def test_writes_identity_when_the_chain_is_complete(db):
    seed_run_chain(db, run_id="ri-a")
    h = record_run_identity(db, "ri-a", _ENV)
    assert h is not None
    row = db.execute(
        "SELECT owner_subject, owner_tenant, root_generation_run_id, run_identity_hash "
        "FROM feature_run_identity WHERE generation_run_id='ri-a'").fetchone()
    assert row == ("user:priya", "t1", "ri-a", h)


def test_the_written_row_carries_every_chain_link_and_no_parent(db):
    c = seed_run_chain(db, run_id="ri-a2")
    record_run_identity(db, "ri-a2", _ENV)
    row = db.execute(
        "SELECT workflow_definition_version, intent_id, confirmed_scope_id, "
        "generation_input_content_hash, considered_revision_id, considered_content_hash, "
        "metadata_snapshot_id, metadata_snapshot_content_hash, parent_generation_run_id, "
        "created_by FROM feature_run_identity WHERE generation_run_id='ri-a2'").fetchone()
    assert row == ("V1", c["intent_id"], c["scope_id"], "gh", c["considered_revision_id"], "cch",
                   c["snapshot_id"], "ch", None, "user:priya")


def test_the_hash_payload_is_pinned_to_thirteen_literal_fields(db):
    """Golden vector: the hash is exactly these field NAMES over these VALUES.

    Pins, in one assertion, that the payload carries no timestamp (a clock read would make the hash
    unreproducible), never hashes the identity hash itself (non-self-reference), and keeps the field
    names stable — any rename, addition or removal changes the digest and fails here."""
    c = seed_run_chain(db, run_id="ri-golden")
    h = record_run_identity(db, "ri-golden", _ENV)
    assert h == jcs_sha256({
        "workflow_definition_version": "V1",
        "generation_run_id": "ri-golden",
        "intent_id": c["intent_id"],
        "confirmed_scope_id": c["scope_id"],
        "generation_input_content_hash": "gh",
        "considered_revision_id": c["considered_revision_id"],
        "considered_content_hash": "cch",
        "metadata_snapshot_id": c["snapshot_id"],
        "metadata_snapshot_content_hash": "ch",
        "owner_subject": "user:priya",
        "owner_tenant": "t1",
        "root_generation_run_id": "ri-golden",
        "parent_generation_run_id": None,
    })


def test_returns_none_when_the_chain_is_incomplete(db):
    # run + intent only — no generation input, no considered revision, no snapshot
    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
               "VALUES ('ri-b-i', 'h', 'hypothesis') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
               "VALUES ('ri-b', 'ri-b-i', %s, '{}')", (Jsonb({"subject": "x"}),))
    assert record_run_identity(db, "ri-b", _ENV) is None
    assert db.execute("SELECT count(*) FROM feature_run_identity "
                      "WHERE generation_run_id='ri-b'").fetchone()[0] == 0


def test_returns_none_when_the_considered_revision_pins_no_snapshot(db):
    _seed_chain_without_snapshot_pin(db, "ri-d")
    assert record_run_identity(db, "ri-d", _ENV) is None
    assert db.execute("SELECT count(*) FROM feature_run_identity "
                      "WHERE generation_run_id='ri-d'").fetchone()[0] == 0


def test_idempotent_second_call_keeps_the_first_identity(db):
    seed_run_chain(db, run_id="ri-c")
    h1 = record_run_identity(db, "ri-c", _ENV)
    h2 = record_run_identity(db, "ri-c", _ENV)
    assert h1 == h2
    assert db.execute("SELECT count(*) FROM feature_run_identity "
                      "WHERE generation_run_id='ri-c'").fetchone()[0] == 1


def test_two_runs_of_the_same_shape_get_different_identities(db):
    seed_run_chain(db, run_id="ri-e1")
    seed_run_chain(db, run_id="ri-e2")
    assert record_run_identity(db, "ri-e1", _ENV) != record_run_identity(db, "ri-e2", _ENV)

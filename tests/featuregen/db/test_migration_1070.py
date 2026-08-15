"""1070 — recognition request identity, audited against a POPULATED legacy-shape table.

The standing lesson (`migration-audits-blind-to-legacy-data`): `apply_migrations` always runs on a
FRESH database in CI, so a migration that trips over rows that already exist passes every test and
fails on the one database that matters. Every test here drops back to the pre-1070 shape, SEEDS it
with legacy rows, and then applies the SQL exactly as the runner does.

What is being audited is not only "does it apply" but the COEXISTENCE RULE it commits to: 0974's
`UNIQUE (intent_id, input_hash)` must survive untouched (old code names it in an `ON CONFLICT`
clause and migrations land before the new image), and legacy rows must keep a truthful NULL rather
than an invented request identity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_FILE = "1070_recognition_request_identity.sql"

_CONTENT_HASH = "a" * 64
_REQUEST_HASH = "b" * 64


def _sql(name: str) -> str:
    return (_MIGRATION_DIR / name).read_text(encoding="utf-8")


def _apply(db) -> None:
    db.execute(_sql(_FILE))


def _to_legacy_shape(db) -> None:
    """The exact pre-1070 shape: no request-hash column, no llm_call_ref, no CHECK, no index."""
    db.execute("DROP INDEX IF EXISTS intent_recognition_attempt_request_idx")
    db.execute("ALTER TABLE intent_recognition_attempt "
               "DROP CONSTRAINT IF EXISTS intent_recognition_attempt_request_is_the_key")
    db.execute("ALTER TABLE intent_recognition_attempt "
               "DROP COLUMN IF EXISTS recognition_request_hash, "
               "DROP COLUMN IF EXISTS llm_call_ref")


def _seed_legacy(db, *, intent_id: str = "int_legacy", recognition_id: str = "rcg_legacy",
                 input_hash: str = _CONTENT_HASH) -> None:
    """One attempt written the OLD way: `input_hash` IS the redacted-input content hash."""
    db.execute(
        "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, status, "
        "  candidates, taxonomy_version, applicability_mapping_version, recognizer_model_id, "
        "  prompt_version, recipe_registry_version, input_json, input_content_hash, "
        "  redaction_policy_version) "
        "VALUES (%s, %s, %s, 'technical_failure', '[]'::jsonb, '1.0.0', '2.0.0', "
        "  'claude-sonnet-5', '3', '2.0.0', '{}'::jsonb, %s, 'r1')",
        (recognition_id, intent_id, input_hash, input_hash))


def _columns(db) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'intent_recognition_attempt'").fetchall()}


# ── legacy-shape replay ─────────────────────────────────────────────────────────────────────────

def test_1070_applies_over_a_populated_legacy_table(db) -> None:
    """The failure CI cannot see: the migration meeting rows that predate it."""
    _to_legacy_shape(db)
    _seed_legacy(db)
    assert "recognition_request_hash" not in _columns(db)
    _apply(db)
    assert {"recognition_request_hash", "llm_call_ref"} <= _columns(db)
    row = db.execute(
        "SELECT recognition_request_hash, llm_call_ref, input_hash, input_content_hash "
        "FROM intent_recognition_attempt WHERE recognition_id = 'rcg_legacy'").fetchone()
    # A permanent, truthful NULL: nobody observed this row's request identity, and the table's
    # write-once trigger (1024) means nobody ever can. No backfill, no invented provenance.
    assert row[0] is None and row[1] is None
    assert row[2] == _CONTENT_HASH and row[3] == _CONTENT_HASH


def test_1070_is_idempotent_over_populated_tables(db) -> None:
    _to_legacy_shape(db)
    _seed_legacy(db)
    _apply(db)
    _apply(db)
    assert db.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE recognition_id = 'rcg_legacy'"
    ).fetchone()[0] == 1


def test_the_old_unique_key_survives_the_migration(db) -> None:
    """THE COEXISTENCE RULE. Migrations are applied BEFORE the new image, so for a whole deploy
    window the OLD code runs against this schema — and its insert names `(intent_id, input_hash)` in
    an `ON CONFLICT` clause, which Postgres resolves by INFERENCE against a real constraint. Drop it
    and every recognition 500s for the length of the deploy. This asserts the inference still
    resolves, in the only way that proves it: by running the old statement."""
    _to_legacy_shape(db)
    _seed_legacy(db)
    _apply(db)
    db.execute(
        "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, status, "
        "  candidates, taxonomy_version, applicability_mapping_version, recognizer_model_id, "
        "  prompt_version, recipe_registry_version) "
        "VALUES ('rcg_second', 'int_legacy', %s, 'unscoped', '[]'::jsonb, '1.0.0', '2.0.0', "
        "  'claude-sonnet-5', '3', '2.0.0') "
        "ON CONFLICT (intent_id, input_hash) DO NOTHING", (_CONTENT_HASH,))
    assert db.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = 'int_legacy'"
    ).fetchone()[0] == 1


# ── what the new columns are allowed to say ─────────────────────────────────────────────────────

def test_a_request_hash_that_is_not_the_key_is_refused(db) -> None:
    """The widened key rides IN the old constraint, so a row claiming a request identity it is not
    actually keyed by would be idempotent under one rule and unique under another."""
    _to_legacy_shape(db)
    _seed_legacy(db)
    _apply(db)
    with pytest.raises(Exception):
        with db.transaction():
            db.execute(
                "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, "
                "  status, candidates, taxonomy_version, applicability_mapping_version, "
                "  recognizer_model_id, prompt_version, recipe_registry_version, "
                "  recognition_request_hash) "
                "VALUES ('rcg_liar', 'int_x', %s, 'unscoped', '[]'::jsonb, '1.0.0', '2.0.0', "
                "  'claude-sonnet-5', '3', '2.0.0', %s)", (_CONTENT_HASH, _REQUEST_HASH))


def test_many_legacy_rows_per_intent_still_coexist(db) -> None:
    """The new unique index is PARTIAL for exactly this reason: legacy rows answer no recorded
    request, so `(intent_id, NULL)` must not be a uniqueness claim about them."""
    _to_legacy_shape(db)
    _seed_legacy(db, recognition_id="rcg_l1", input_hash="1" * 64)
    _seed_legacy(db, recognition_id="rcg_l2", input_hash="2" * 64)
    _apply(db)
    assert db.execute(
        "SELECT count(*) FROM intent_recognition_attempt "
        "WHERE intent_id = 'int_legacy' AND recognition_request_hash IS NULL").fetchone()[0] == 2


def test_one_attempt_per_intent_and_request_identity(db) -> None:
    _to_legacy_shape(db)
    _apply(db)
    for recognition_id in ("rcg_r1", "rcg_r2"):
        statement = (
            "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, "
            "  status, candidates, taxonomy_version, applicability_mapping_version, "
            "  recognizer_model_id, prompt_version, recipe_registry_version, "
            "  recognition_request_hash) "
            "VALUES (%s, 'int_r', %s, 'unscoped', '[]'::jsonb, '1.0.0', '2.0.0', "
            "  'claude-sonnet-5', '3', '2.0.0', %s)")
        if recognition_id == "rcg_r1":
            db.execute(statement, (recognition_id, _REQUEST_HASH, _REQUEST_HASH))
        else:
            with pytest.raises(Exception):
                with db.transaction():
                    db.execute(statement, (recognition_id, _REQUEST_HASH, _REQUEST_HASH))


# ── the reservation ─────────────────────────────────────────────────────────────────────────────

def test_this_stream_allocated_exactly_one_number() -> None:
    """Keyed on this stream's own name, never on "nobody may take 1071" — the 1057 lesson."""
    names = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    assert [n for n in names if n.startswith("1070_")] == [_FILE]
    assert [n for n in names if "recognition_request_identity" in n] == [_FILE]

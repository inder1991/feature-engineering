"""1071 — recognition quality, audited against a POPULATED legacy-shape table.

The standing lesson (`migration-audits-blind-to-legacy-data`): `apply_migrations` always runs on a
FRESH database in CI, so a migration that trips over rows that already exist passes every test and
fails on the one database that matters. Every test here drops back to the pre-1071 shape, SEEDS it
with rows written the old way, and then applies the SQL exactly as the runner does.

The rules being audited are the ones a reader of the served payload depends on: the disposition
vocabulary is CLOSED (five values, and a sixth needs a migration that says so), the quality is
written WHOLE or not at all (the reader decides from one column), and a legacy row keeps a truthful
NULL rather than an invented `clean` — 1024's `intent_recognition_attempt_no_mutation` trigger
refuses UPDATE and DELETE on this table, so there is no backfill and there never can be.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_FILE = "1071_recognition_quality.sql"

_CONTENT_HASH = "a" * 64
_REQUEST_HASH = "b" * 64


def _sql(name: str) -> str:
    return (_MIGRATION_DIR / name).read_text(encoding="utf-8")


def _apply(db) -> None:
    db.execute(_sql(_FILE))


def _to_legacy_shape(db) -> None:
    """The exact pre-1071 shape: no quality columns and neither of its CHECKs. 1070's request
    identity STAYS — this migration lands on top of it, and the audit is worth nothing if it runs
    against a shape no deployment will ever have."""
    db.execute("ALTER TABLE intent_recognition_attempt "
               "DROP CONSTRAINT IF EXISTS intent_recognition_attempt_disposition_is_closed")
    db.execute("ALTER TABLE intent_recognition_attempt "
               "DROP CONSTRAINT IF EXISTS intent_recognition_attempt_quality_is_coherent")
    db.execute("ALTER TABLE intent_recognition_attempt "
               "DROP COLUMN IF EXISTS recognition_disposition, "
               "DROP COLUMN IF EXISTS repair_attempt_count, "
               "DROP COLUMN IF EXISTS dropped_candidates")


_BASE_COLUMNS = (
    "recognition_id, intent_id, input_hash, status, candidates, taxonomy_version, "
    "applicability_mapping_version, recognizer_model_id, prompt_version, recipe_registry_version")
_BASE_VALUES = "'1.0.0', '2.0.0', 'claude-sonnet-5', '3', '2.0.0'"


def _seed_legacy(db, *, intent_id: str = "int_legacy", recognition_id: str = "rcg_legacy",
                 input_hash: str = _CONTENT_HASH, status: str = "technical_failure") -> None:
    """One attempt written before this migration: an outcome, and nothing about how it was reached."""
    db.execute(
        f"INSERT INTO intent_recognition_attempt ({_BASE_COLUMNS}, input_json, "
        "  input_content_hash, redaction_policy_version) "
        f"VALUES (%s, %s, %s, %s, '[]'::jsonb, {_BASE_VALUES}, '{{}}'::jsonb, %s, 'r1')",
        (recognition_id, intent_id, input_hash, status, input_hash))


def _columns(db) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'intent_recognition_attempt'").fetchall()}


def _insert_quality(db, *, recognition_id: str, disposition, repairs, drops,
                    intent_id: str = "int_q", input_hash: str = _REQUEST_HASH) -> None:
    db.execute(
        f"INSERT INTO intent_recognition_attempt ({_BASE_COLUMNS}, recognition_disposition, "
        "  repair_attempt_count, dropped_candidates) "
        f"VALUES (%s, %s, %s, 'classified', '[]'::jsonb, {_BASE_VALUES}, %s, %s, %s::jsonb)",
        (recognition_id, intent_id, input_hash, disposition, repairs, drops))


# ── legacy-shape replay ─────────────────────────────────────────────────────────────────────────

def test_1071_applies_over_a_populated_legacy_table(db) -> None:
    """The failure CI cannot see: the migration meeting rows that predate it."""
    _to_legacy_shape(db)
    _seed_legacy(db)
    assert "recognition_disposition" not in _columns(db)
    _apply(db)
    assert {"recognition_disposition", "repair_attempt_count",
            "dropped_candidates"} <= _columns(db)
    row = db.execute(
        "SELECT recognition_disposition, repair_attempt_count, dropped_candidates, status "
        "FROM intent_recognition_attempt WHERE recognition_id = 'rcg_legacy'").fetchone()
    # A permanent, truthful NULL. "The model answered first time" and "nobody recorded whether it
    # did" are different facts, and only one of them is knowable about this row — so it is NOT
    # backfilled to `clean`, and 1024's write-once trigger means nobody could backfill it anyway.
    assert row[0] is None and row[1] is None and row[2] is None
    assert row[3] == "technical_failure"        # the OUTCOME it did record is untouched


def test_1071_is_idempotent_over_populated_tables(db) -> None:
    _to_legacy_shape(db)
    _seed_legacy(db)
    _apply(db)
    _apply(db)
    assert db.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE recognition_id = 'rcg_legacy'"
    ).fetchone()[0] == 1


def test_the_keys_1070_installed_survive_this_migration(db) -> None:
    """1070's coexistence rule is load-bearing for a deploy window (old code names
    `(intent_id, input_hash)` in an `ON CONFLICT` clause), and a later migration on the same table
    is exactly where it would be lost. Proved by running the old statement, not by reading a
    catalog."""
    _to_legacy_shape(db)
    _seed_legacy(db)
    _apply(db)
    db.execute(
        f"INSERT INTO intent_recognition_attempt ({_BASE_COLUMNS}) "
        f"VALUES ('rcg_second', 'int_legacy', %s, 'unscoped', '[]'::jsonb, {_BASE_VALUES}) "
        "ON CONFLICT (intent_id, input_hash) DO NOTHING", (_CONTENT_HASH,))
    assert db.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = 'int_legacy'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = "
        "'intent_recognition_attempt'::regclass AND conname = "
        "'intent_recognition_attempt_request_is_the_key'").fetchone()[0] == 1


# ── what the new columns are allowed to say ─────────────────────────────────────────────────────

def test_every_disposition_the_platform_can_serve_is_accepted(db) -> None:
    """The CHECK and the code's enum are the same five values. A vocabulary that drifted would
    either 500 a real recognition or admit a value the UI has no branch for."""
    from featuregen.overlay.upload.taxonomy.recognition import RecognitionDisposition

    _to_legacy_shape(db)
    _apply(db)
    for n, disposition in enumerate(RecognitionDisposition):
        _insert_quality(db, recognition_id=f"rcg_d{n}", disposition=disposition.value,
                        repairs=0, drops="[]", input_hash=f"{n}" * 64)
    assert db.execute(
        "SELECT count(DISTINCT recognition_disposition) FROM intent_recognition_attempt"
    ).fetchone()[0] == len(RecognitionDisposition)


def test_a_sixth_disposition_is_refused(db) -> None:
    """The vocabulary is closed in the database as well as in the code, so widening it is a
    migration somebody has to write and review — not a string a caller can invent."""
    _to_legacy_shape(db)
    _apply(db)
    with pytest.raises(Exception):
        with db.transaction():
            _insert_quality(db, recognition_id="rcg_x", disposition="mostly_fine", repairs=0,
                            drops="[]")


def test_half_a_quality_is_refused(db) -> None:
    """WRITTEN WHOLE OR NOT AT ALL. The reader decides "does this row have a quality?" from ONE
    column; three columns that could disagree would let it serve a `clean` disposition beside an
    unrecorded drop set — the exact lie this contract exists to stop."""
    _to_legacy_shape(db)
    _apply(db)
    for n, (disposition, repairs, drops) in enumerate((
        ("clean", None, "[]"),          # a disposition with no repair count
        ("clean", 0, None),             # a disposition with no drop record
        (None, 0, "[]"),                # a repair count nobody can attribute
    )):
        with pytest.raises(Exception):
            with db.transaction():
                _insert_quality(db, recognition_id=f"rcg_half{n}", disposition=disposition,
                                repairs=repairs, drops=drops, input_hash=f"{n}c" * 32)


def test_a_negative_repair_count_is_refused(db) -> None:
    """A repair count is arithmetic the platform did over its own ledger; there is no honest way for
    it to be negative, and it is rendered to a human."""
    _to_legacy_shape(db)
    _apply(db)
    with pytest.raises(Exception):
        with db.transaction():
            _insert_quality(db, recognition_id="rcg_neg", disposition="clean", repairs=-1,
                            drops="[]")


def test_many_legacy_rows_keep_their_null_quality_alongside_recorded_ones(db) -> None:
    """The two shapes coexist permanently: NULL is not a state to be migrated out of."""
    _to_legacy_shape(db)
    _seed_legacy(db, recognition_id="rcg_l1", input_hash="1" * 64)
    _seed_legacy(db, recognition_id="rcg_l2", input_hash="2" * 64)
    _apply(db)
    _insert_quality(db, recognition_id="rcg_new", disposition="partially_recovered", repairs=2,
                    drops='[{"index": 1, "reason_code": "MALFORMED_EVIDENCE_SPANS"}]',
                    intent_id="int_legacy", input_hash="3" * 64)
    assert db.execute(
        "SELECT count(*) FROM intent_recognition_attempt "
        "WHERE intent_id = 'int_legacy' AND recognition_disposition IS NULL").fetchone()[0] == 2
    assert db.execute(
        "SELECT dropped_candidates FROM intent_recognition_attempt "
        "WHERE recognition_id = 'rcg_new'").fetchone()[0] == [
            {"index": 1, "reason_code": "MALFORMED_EVIDENCE_SPANS"}]


def test_an_aggregate_refusal_records_a_null_index(db) -> None:
    """`index: null` means the whole RESULT was refused rather than a candidate being at fault —
    the jsonb has to be able to say that, because blaming an aggregate defect on a position would
    name a candidate that may be perfectly well formed."""
    _to_legacy_shape(db)
    _apply(db)
    _insert_quality(db, recognition_id="rcg_agg", disposition="technical_failure", repairs=2,
                    drops='[{"index": null, "reason_code": "MULTIPLE_PRIMARY_CANDIDATES"}]')
    assert db.execute(
        "SELECT dropped_candidates FROM intent_recognition_attempt "
        "WHERE recognition_id = 'rcg_agg'").fetchone()[0] == [
            {"index": None, "reason_code": "MULTIPLE_PRIMARY_CANDIDATES"}]


# ── the reservation ─────────────────────────────────────────────────────────────────────────────

def test_this_stream_allocated_exactly_one_number() -> None:
    """Keyed on this stream's own name, never on "nobody may take 1072" — the 1057 lesson."""
    names = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    assert [n for n in names if n.startswith("1071_")] == [_FILE]
    assert [n for n in names if "recognition_quality" in n] == [_FILE]

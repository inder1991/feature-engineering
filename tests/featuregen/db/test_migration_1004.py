"""Migration 1004 — ingestion_run source-profile provenance (Delivery B item 9).

The run manifest records WHICH source capability profile produced the run: ``source_type`` is the
profile identity (open vocabulary — 'technical_csv' / 'ftr_glossary' / connector-specific, so no
CHECK) and ``profile_version`` is the capability-profile schema version stamp
(``SOURCE_CAPABILITY_PROFILE_VERSION``). Both nullable: a run that failed before profile selection
never knew a profile and records NULL honestly.
"""
from __future__ import annotations


def test_1004_adds_nullable_text_source_profile_columns(db):
    rows = db.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'ingestion_run' "
        "AND column_name IN ('source_type', 'profile_version')").fetchall()
    assert {c: (t, n) for c, t, n in rows} == {
        "source_type": ("text", "YES"), "profile_version": ("text", "YES")}


def test_1004_legacy_shaped_insert_leaves_provenance_null(db):
    # a pre-1004 writer (no new columns) still inserts, and the provenance is honestly NULL —
    # never a fabricated default.
    db.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES ('ingrun_legacy_1004', 'upload', 'deposits', "
        "'user:tester', 'in_progress', now(), now())")
    assert db.execute(
        "SELECT source_type, profile_version FROM ingestion_run "
        "WHERE id = 'ingrun_legacy_1004'").fetchone() == (None, None)

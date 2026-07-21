"""Migration 1016 — asset read-model composite indexes (Delivery F2-audit).

The one genuinely-new hot path is the reverse SUBJECT lookup the F2-audit LLM-audit-summaries
subsection introduces (which dispatches touched THIS ref); 1005 only indexed the forward
``llm_dispatch_subject(dispatch_ref)`` join. This suite asserts the two reverse indexes exist and are
idempotent (CREATE INDEX IF NOT EXISTS).
"""
from __future__ import annotations

from pathlib import Path

import featuregen.db.migrations as _migrations

_EXPECTED_INDEXES = {"llm_dispatch_subject_object_idx", "llm_dispatch_subject_logical_idx"}


def test_1016_reverse_subject_indexes_present(conn) -> None:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'llm_dispatch_subject'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert _EXPECTED_INDEXES <= names, f"missing: {_EXPECTED_INDEXES - names}"


def test_1016_is_idempotent(conn) -> None:
    # Re-applying the migration SQL is a no-op (CREATE INDEX IF NOT EXISTS) — no error, still present.
    sql = (Path(_migrations.__file__).parent / "migrations" / "1016_asset_read_indexes.sql").read_text()
    conn.execute(sql)
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'llm_dispatch_subject'"
    ).fetchall()
    assert _EXPECTED_INDEXES <= {r[0] for r in rows}

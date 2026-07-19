"""Migration 1010 — the asset-detail read-model index (Delivery F0 Task 3).

F0-T2's asset_detail._history_section runs the reverse ingestion_run_object lookup "which runs
observed/changed this ref, newest-first":

    WHERE o.catalog_source = %s AND lower(o.object_ref) = lower(%s)
 ORDER BY o.at DESC, o.ingestion_run_id

1010 adds ingestion_run_object_source_ref_at_idx (catalog_source, lower(object_ref), at DESC) so
this access path resolves via an ordered index scan rather than a seq scan + sort. The middle key is
the EXPRESSION lower(object_ref) — the existing 0998 raw-column index cannot serve the case-folded
predicate, and a raw object_ref key here could not either. The other asset-detail sections
(evidence/decision keyed on logical_ref) are ALREADY covered by 0983/0981, so 1010 adds no
redundant index for them — this suite asserts only the one non-redundant index it creates.

The EXPLAIN test forces enable_seqscan off within the transaction so the planner's choice is not
masked by the tiny test table (Postgres seq-scans a 3-row table regardless of any index); with seq
scans disabled the plan can only use the index if the index actually SERVES the access path, so a
plan naming the index proves usability. A pg_indexes existence/definition check is the deliverable's
primary assertion; the EXPLAIN is the usage verification.
"""
from __future__ import annotations

from datetime import UTC, datetime

_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
_INDEX = "ingestion_run_object_source_ref_at_idx"

# The exact WHERE + ORDER BY access path _history_section drives against ingestion_run_object.
_HISTORY_ACCESS_SQL = (
    "SELECT o.ingestion_run_id, o.relation, o.at FROM ingestion_run_object o "
    "WHERE o.catalog_source = %s AND lower(o.object_ref) = lower(%s) "
    "ORDER BY o.at DESC, o.ingestion_run_id"
)


def _run(conn, run_id: str = "ingrun_1010") -> str:
    conn.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'user:tester', "
        "'in_progress', %s, %s)", (run_id, _NOW, _NOW))
    return run_id


def _object(conn, run_id: str, ref: str, *, relation: str = "observed",
            at: datetime = _NOW) -> None:
    conn.execute(
        "INSERT INTO ingestion_run_object (ingestion_run_id, catalog_source, object_ref, "
        "relation, at) VALUES (%s, 'deposits', %s, %s, %s)", (run_id, ref, relation, at))


def test_1010_history_index_exists_on_right_table(conn) -> None:
    """The new composite index exists on ingestion_run_object."""
    row = conn.execute(
        "SELECT tablename FROM pg_indexes WHERE indexname = %s", (_INDEX,)).fetchone()
    assert row is not None, f"{_INDEX} missing — migration 1010 did not apply"
    assert row[0] == "ingestion_run_object"


def test_1010_history_index_has_expected_columns(conn) -> None:
    """The index keys are (catalog_source, lower(object_ref), at DESC) — the case-folded expression
    key and the DESC at-ordering are BOTH load-bearing for the history access path."""
    indexdef = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (_INDEX,)).fetchone()[0]
    lowered = indexdef.lower()
    assert "catalog_source" in lowered
    assert "lower(object_ref)" in lowered, f"expression key missing: {indexdef}"
    assert "at desc" in lowered, f"descending at-order missing: {indexdef}"


def test_1010_index_is_valid(conn) -> None:
    """The index is READY + VALID (a plain CREATE INDEX builds valid; guards the fallback path)."""
    row = conn.execute(
        "SELECT i.indisvalid, i.indisready FROM pg_class c "
        "JOIN pg_index i ON i.indexrelid = c.oid WHERE c.relname = %s", (_INDEX,)).fetchone()
    assert row == (True, True)


def test_1010_history_query_resolves_via_index(conn) -> None:
    """The reverse-history WHERE + ORDER BY resolves through the new index (usage verification).

    A 3-row test table would otherwise seq-scan whatever indexes exist, so enable_seqscan is
    disabled for the plan: with seq scans off the planner can only pick this index if it truly
    serves (catalog_source, lower(object_ref)) equality + at-ordering — which a raw-column index
    could not. The index name in the plan is the proof of usage."""
    run_id = _run(conn)
    _object(conn, run_id, "public.accounts.id", relation="observed",
            at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC))
    _object(conn, run_id, "public.accounts.id", relation="changed",
            at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC))
    _object(conn, run_id, "public.accounts.balance", relation="observed", at=_NOW)

    conn.execute("SET LOCAL enable_seqscan = off")
    plan = "\n".join(
        r[0] for r in conn.execute(
            "EXPLAIN " + _HISTORY_ACCESS_SQL, ("deposits", "PUBLIC.Accounts.ID")).fetchall())

    assert _INDEX in plan, f"history query did not resolve via {_INDEX}:\n{plan}"

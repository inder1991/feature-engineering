from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.runtime.blob_gc import mark_and_sweep, register_blob

UTC = UTC
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _insert_blob(conn, blob_id, *, classification, referenced=False, created_at, object_key="k"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, referenced, created_at) "
            "VALUES (%s,%s,'sha256:h',%s,%s,%s)",
            (blob_id, object_key, classification, referenced, created_at),
        )


def _status(conn, blob_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, referenced, swept_at FROM blob_index WHERE blob_id=%s", (blob_id,)
        )
        return cur.fetchone()


def test_register_blob_idempotent(conn):
    register_blob(
        conn,
        blob_id="blob_1",
        object_key="k1",
        content_hash="sha256:x",
        classification="pii-erasable",
    )
    register_blob(
        conn,
        blob_id="blob_1",
        object_key="k1",
        content_hash="sha256:x",
        classification="pii-erasable",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), status, referenced FROM blob_index WHERE blob_id='blob_1' GROUP BY status, referenced"
        )
        count, status, referenced = cur.fetchone()
    assert count == 1 and status == "live" and referenced is False


def test_referenced_blob_marked_not_orphaned(
    conn, insert_stub_document, recording_deleter, recording_audit
):
    old = NOW - timedelta(days=1)
    _insert_blob(conn, "blob_ref", classification="pii-erasable", created_at=old, object_key="kref")
    insert_stub_document(conn, doc_id="doc_1", body_ref="blob_ref")
    report = mark_and_sweep(
        conn, now=NOW, grace_seconds=3600, deleter=recording_deleter, auditor=recording_audit
    )
    assert "blob_ref" not in report.marked_orphan
    status, referenced, _ = _status(conn, "blob_ref")
    assert status == "live" and referenced is True


def test_sensitive_orphan_quarantined_not_deleted(conn, recording_deleter, recording_audit):
    old = NOW - timedelta(days=1)
    _insert_blob(conn, "blob_pii", classification="pii-erasable", created_at=old, object_key="kpii")
    report = mark_and_sweep(
        conn, now=NOW, grace_seconds=3600, deleter=recording_deleter, auditor=recording_audit
    )
    assert "blob_pii" in report.quarantined
    assert _status(conn, "blob_pii")[0] == "quarantined"
    assert "kpii" not in recording_deleter.deleted  # sensitive bodies are NOT swept here


def test_nonsensitive_orphan_swept(conn, recording_deleter, recording_audit):
    old = NOW - timedelta(days=1)
    _insert_blob(
        conn, "blob_gov", classification="governance-retained", created_at=old, object_key="kgov"
    )
    report = mark_and_sweep(
        conn, now=NOW, grace_seconds=3600, deleter=recording_deleter, auditor=recording_audit
    )
    assert "blob_gov" in report.swept
    status, _, swept_at = _status(conn, "blob_gov")
    assert status == "swept" and swept_at is not None
    assert "kgov" in recording_deleter.deleted


def test_young_blob_left_live(conn, recording_deleter, recording_audit):
    fresh = NOW - timedelta(seconds=10)
    _insert_blob(conn, "blob_new", classification="governance-retained", created_at=fresh)
    mark_and_sweep(
        conn, now=NOW, grace_seconds=3600, deleter=recording_deleter, auditor=recording_audit
    )
    assert _status(conn, "blob_new")[0] == "live"


def test_gc_run_is_audited(conn, recording_deleter, recording_audit):
    mark_and_sweep(
        conn, now=NOW, grace_seconds=3600, deleter=recording_deleter, auditor=recording_audit
    )
    assert len(recording_audit.reports) == 1
    assert recording_audit.reports[0].ran_at == NOW

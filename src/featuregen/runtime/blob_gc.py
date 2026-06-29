from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from featuregen.contracts import DbConn


@runtime_checkable
class BlobDeleter(Protocol):
    def delete(self, object_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GcReport:
    ran_at: datetime
    marked_orphan: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    swept: tuple[str, ...] = ()


@runtime_checkable
class GcAuditSink(Protocol):
    def record(self, report: GcReport) -> None: ...


def register_blob(
    conn: DbConn,
    *,
    blob_id: str,
    object_key: str,
    content_hash: str,
    classification: str,
    kms_key_id: str | None = None,
    size_bytes: int | None = None,
) -> str:
    """Index a blob written to the object store BEFORE the §5.1 transaction (status='live',
    referenced=false). Idempotent on blob_id. A committed *_ref later flips referenced=true
    (mark phase); a rolled-back step leaves it unreferenced -> an orphan for GC (§5.1)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, "
            "kms_key_id, size_bytes) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (blob_id) DO NOTHING",
            (blob_id, object_key, content_hash, classification, kms_key_id, size_bytes),
        )
    return blob_id


def mark_and_sweep(
    conn: DbConn,
    *,
    now: datetime,
    grace_seconds: int,
    deleter: BlobDeleter,
    auditor: GcAuditSink,
) -> GcReport:
    """Mark-and-sweep unreferenced-blob GC (§5.1). MARK: live blobs that a committed
    document references -> referenced=true; live, unreferenced blobs older than the grace
    window with NO committed documents.body_ref pointing at them -> 'orphan'. QUARANTINE:
    pii-erasable orphans -> 'quarantined' (held for §9 erasure/retention, NOT deleted here).
    SWEEP: remaining (non-sensitive) orphans -> object-store delete + 'swept'. Every run is
    audited via the sink."""
    cutoff = now - timedelta(seconds=grace_seconds)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE blob_index b SET referenced = true "
            "WHERE b.status = 'live' AND b.referenced = false "
            "  AND EXISTS (SELECT 1 FROM documents d WHERE d.body_ref = b.blob_id)"
        )
        cur.execute(
            "UPDATE blob_index b SET status = 'orphan' "
            "WHERE b.status = 'live' AND b.referenced = false AND b.created_at < %s "
            "  AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.body_ref = b.blob_id) "
            "RETURNING b.blob_id",
            (cutoff,),
        )
        marked = [r[0] for r in cur.fetchall()]
        cur.execute(
            "UPDATE blob_index SET status = 'quarantined' "
            "WHERE status = 'orphan' AND classification = 'pii-erasable' RETURNING blob_id"
        )
        quarantined = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT blob_id, object_key FROM blob_index WHERE status = 'orphan'")
        sweepable = cur.fetchall()
        swept = []
        for blob_id, object_key in sweepable:
            deleter.delete(object_key)
            cur.execute(
                "UPDATE blob_index SET status = 'swept', swept_at = %s WHERE blob_id = %s",
                (now, blob_id),
            )
            swept.append(blob_id)
    report = GcReport(
        ran_at=now,
        marked_orphan=tuple(marked),
        quarantined=tuple(quarantined),
        swept=tuple(swept),
    )
    auditor.record(report)
    return report

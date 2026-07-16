"""Governed-join drift detection (advisory): surface a re-upload that RETARGETS or DROPS a
`joins_to` humans already VERIFIED as an approved_join.

DETECTION NEVER CHANGES GOVERNED STATE — the protected invariant. It reads the source's VERIFIED
operational `graph_edge` rows (the `project_confirmed_joins` projection: `authority='operational'`,
`approved_join_status='VERIFIED'`, kind='joins') and the upload's declared `joins_to` map, and
writes ONLY to the advisory `governed_join_divergence` table (migration 0990). The VERIFIED fact
stays VERIFIED and its edge stays operational until a human acts (no auto-demote); the divergence
row is the reviewer's prompt to act.

Per VERIFIED (from_ref, verified_to_ref):
* the upload re-declares the SAME pair (either orientation — Pass C can confirm the reverse of
  the file's authoring) -> RESOLVED: any existing divergence row is deleted;
* the upload declares NO parseable join on the from-column -> 'dropped' (a malformed `joins_to`
  cannot re-affirm a verified join — the propose seam skips it loud with the same parse);
* the upload declares a DIFFERENT target -> 'retargeted' (declared_to_ref = the new target).

UPSERT on UNIQUE (catalog_source, from_ref, verified_to_ref): a re-upload REFRESHES the same
divergence in place — and RE-OPENS it (acknowledged_* reset to NULL) even if a reviewer had
acknowledged an earlier detection, because a fresh upload re-asserting the divergence is new
information.

Called by `ingest_upload` on the governed-joins path only (flag-off byte-for-byte), inside its own
savepoint + except (advisory, fail-soft — a detection fault never aborts an upload). It must run
AFTER the end-of-ingest approved-join re-projection: build_graph wipes every edge mid-ingest, so
graph_edge carries the source's VERIFIED joins again only once `project_confirmed_joins` re-ran.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from featuregen.contracts import DbConn
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import governed_join_proposal
from featuregen.overlay.upload.upload_catalog import _column_object_ref
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


def _declared_join_map(rows: list[CanonicalRow]) -> dict[str, str]:
    """The upload's declared joins as {from public-scope column ref -> declared target ref} —
    built from the SAME `governed_join_proposal` parse the propose seam uses, so the two seams can
    never disagree about what a row declares. A malformed `joins_to` is skipped (the propose seam
    already logs its diagnostic loud)."""
    declared: dict[str, str] = {}
    for r in rows:
        if not r.joins_to:
            continue
        ref = governed_join_proposal(r)
        if ref is None:
            continue   # malformed — skipped-loud by _propose_governed_joins with the diagnostic
        declared[_column_object_ref(r.table, r.column)] = _column_object_ref(
            ref.to_ref.table, ref.to_ref.column)
    return declared


def detect_governed_join_divergences(conn: DbConn, catalog_source: str,
                                     rows: list[CanonicalRow], *,
                                     source_snapshot_id: str | None = None,
                                     now: datetime | None = None) -> list[dict]:
    """Diff the upload's declared joins against the source's VERIFIED operational joins and
    upsert/resolve `governed_join_divergence` rows. Returns the divergences detected THIS run
    (each ``{from_ref, verified_to_ref, declared_to_ref, kind}``). READ-ONLY on graph_edge and
    the fact streams — never mutates governed state."""
    now = now or datetime.now(UTC)   # ingest's `now` is Optional — mirror project_confirmed_joins
    verified = conn.execute(
        "SELECT from_ref, to_ref FROM graph_edge"
        " WHERE catalog_source = %s AND kind = 'joins'"
        " AND approved_join_status = 'VERIFIED' AND authority = 'operational'",
        (catalog_source,)).fetchall()
    if not verified:
        return []
    declared = _declared_join_map(rows)
    detected: list[dict] = []
    for from_ref, verified_to_ref in verified:
        declared_to = declared.get(from_ref)
        if declared_to == verified_to_ref or declared.get(verified_to_ref) == from_ref:
            # Re-affirmed (either orientation): the divergence — if it was ever open — is RESOLVED.
            resolved = conn.execute(
                "DELETE FROM governed_join_divergence WHERE catalog_source = %s"
                " AND from_ref = %s AND verified_to_ref = %s RETURNING 1",
                (catalog_source, from_ref, verified_to_ref)).fetchone()
            if resolved is not None:
                counters.incr("overlay.join_drift.resolved")
                logger.info("governed-join divergence resolved: %s -> %s re-declared in %r",
                            from_ref, verified_to_ref, catalog_source)
            continue
        kind = "dropped" if declared_to is None else "retargeted"
        conn.execute(
            "INSERT INTO governed_join_divergence (catalog_source, from_ref, verified_to_ref,"
            " declared_to_ref, kind, source_snapshot_id, detected_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (catalog_source, from_ref, verified_to_ref) DO UPDATE SET"
            " declared_to_ref = EXCLUDED.declared_to_ref, kind = EXCLUDED.kind,"
            " source_snapshot_id = EXCLUDED.source_snapshot_id,"
            " detected_at = EXCLUDED.detected_at,"
            " acknowledged_at = NULL, acknowledged_by = NULL",
            (catalog_source, from_ref, verified_to_ref, declared_to, kind,
             source_snapshot_id, now))
        counters.incr(f"overlay.join_drift.{kind}")
        logger.warning(
            "governed-join divergence (%s) in %r: VERIFIED %s -> %s is now declared -> %s"
            " — the verified join stays operational until a reviewer acts",
            kind, catalog_source, from_ref, verified_to_ref, declared_to)
        detected.append({"from_ref": from_ref, "verified_to_ref": verified_to_ref,
                         "declared_to_ref": declared_to, "kind": kind})
    return detected


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def list_governed_join_divergences(conn: DbConn, catalog_source: str) -> list[dict]:
    """The source's OPEN (unacknowledged) divergences, newest detection first — the advisory list
    the joins governance surface renders beside the open proposals."""
    rows = conn.execute(
        "SELECT id, from_ref, verified_to_ref, declared_to_ref, kind, detected_at"
        " FROM governed_join_divergence"
        " WHERE catalog_source = %s AND acknowledged_at IS NULL"
        " ORDER BY detected_at DESC, id DESC",
        (catalog_source,)).fetchall()
    return [{"id": r[0], "from_ref": r[1], "verified_to_ref": r[2], "declared_to_ref": r[3],
             "kind": r[4], "detected_at": _iso(r[5])} for r in rows]


def acknowledge_governed_join_divergence(conn: DbConn, divergence_id: int, *, subject: str,
                                         now: datetime | None = None) -> dict | None:
    """Mark a divergence acknowledged ("seen — the verified join stands / is being handled") by
    `subject`. Returns the updated row, or None when no such id exists. Acknowledging hides the
    row from the open list; a FRESH detection re-opens it. Idempotent on repeat (re-stamps)."""
    now = now or datetime.now(UTC)
    row = conn.execute(
        "UPDATE governed_join_divergence SET acknowledged_at = %s, acknowledged_by = %s"
        " WHERE id = %s"
        " RETURNING id, catalog_source, from_ref, verified_to_ref, declared_to_ref, kind,"
        " detected_at, acknowledged_at, acknowledged_by",
        (now, subject, divergence_id)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "catalog_source": row[1], "from_ref": row[2],
            "verified_to_ref": row[3], "declared_to_ref": row[4], "kind": row[5],
            "detected_at": _iso(row[6]), "acknowledged_at": _iso(row[7]),
            "acknowledged_by": row[8]}

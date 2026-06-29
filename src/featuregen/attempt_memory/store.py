from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

ATTEMPT_DISPOSITIONS: tuple[str, ...] = (
    "explored",
    "discarded",
    "rejected",
    "selected",
    "promoted",
)


@dataclass(frozen=True, slots=True)
class AttemptMemoryEntry:
    definition_hash: str
    disposition: str
    score: float | None = None
    reason: str | None = None
    request_id: str | None = None
    feature_id: str | None = None
    crypto_shred_exempt: bool = True


def record_attempt(
    conn: DbConn,
    *,
    definition_hash: str,
    disposition: str,
    score: float | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    feature_id: str | None = None,
) -> None:
    if disposition not in ATTEMPT_DISPOSITIONS:
        raise ValueError(f"disposition {disposition!r} not in {ATTEMPT_DISPOSITIONS}")
    conn.execute(
        """
        INSERT INTO attempt_memory (definition_hash, disposition, score, reason, request_id, feature_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (definition_hash) DO UPDATE SET
            disposition = EXCLUDED.disposition,
            score       = COALESCE(EXCLUDED.score, attempt_memory.score),
            reason      = COALESCE(EXCLUDED.reason, attempt_memory.reason),
            request_id  = COALESCE(EXCLUDED.request_id, attempt_memory.request_id),
            feature_id  = COALESCE(EXCLUDED.feature_id, attempt_memory.feature_id),
            last_seen   = now()
        """,
        (definition_hash, disposition, score, reason, request_id, feature_id),
    )


def lookup_attempt(conn: DbConn, definition_hash: str) -> AttemptMemoryEntry | None:
    row = conn.execute(
        "SELECT definition_hash, disposition, score, reason, request_id, feature_id, crypto_shred_exempt "
        "FROM attempt_memory WHERE definition_hash = %s",
        (definition_hash,),
    ).fetchone()
    if row is None:
        return None
    return AttemptMemoryEntry(
        definition_hash=row[0],
        disposition=row[1],
        score=float(row[2]) if row[2] is not None else None,
        reason=row[3],
        request_id=row[4],
        feature_id=row[5],
        crypto_shred_exempt=bool(row[6]),
    )


def count_candidates_explored(
    conn: DbConn, *, request_id: str | None = None, feature_id: str | None = None
) -> int:
    if request_id is not None:
        row = conn.execute(
            "SELECT count(*) FROM attempt_memory WHERE request_id = %s", (request_id,)
        ).fetchone()
    elif feature_id is not None:
        row = conn.execute(
            "SELECT count(*) FROM attempt_memory WHERE feature_id = %s", (feature_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT count(*) FROM attempt_memory").fetchone()
    return int(row[0])

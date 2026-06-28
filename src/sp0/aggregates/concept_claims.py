from __future__ import annotations

from typing import Optional

from sp0.contracts import DbConn


def claim_concept(conn: DbConn, concept_key: str, request_id: str) -> Optional[str]:
    won = conn.execute(
        "INSERT INTO concept_claims (concept_key, request_id) VALUES (%s, %s) "
        "ON CONFLICT (concept_key) DO NOTHING RETURNING request_id",
        (concept_key, request_id),
    ).fetchone()
    if won is not None:
        return None
    existing = conn.execute(
        "SELECT request_id FROM concept_claims WHERE concept_key = %s",
        (concept_key,),
    ).fetchone()
    return existing[0] if existing else None

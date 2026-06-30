from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    fact_key: str
    table_snapshot_at: object
    row_count: int
    sample_size: int
    profile_version: str
    thresholds_used: dict
    metric_values: dict
    created_by: dict
    created_at: object


def write_evidence(
    conn: DbConn,
    *,
    fact_key: str,
    table_snapshot_at: object,
    row_count: int,
    sample_size: int,
    profile_version: str,
    thresholds_used: Mapping[str, Any],
    metric_values: Mapping[str, Any],
    created_by: Mapping[str, Any],
) -> str:
    """Write one immutable evidence record (§5.1) and return its evidence_id. Append-only: each
    call mints a fresh `eviu_` id and INSERTs a new row — there is no update path. Stores ONLY
    aggregate metrics; callers must never pass raw values / raw MIN/MAX (Global Constraint).
    `created_by` is a Mapping persisted as jsonb (pin 14) — callers pass `identity_to_jsonb(actor)`,
    never a raw IdentityEnvelope."""
    evidence_id = mint_id("eviu")
    conn.execute(
        """
        INSERT INTO overlay_evidence
            (evidence_id, fact_key, table_snapshot_at, row_count, sample_size,
             profile_version, thresholds_used, metric_values, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            evidence_id, fact_key, table_snapshot_at, row_count, sample_size,
            profile_version, Jsonb(dict(thresholds_used)), Jsonb(dict(metric_values)),
            Jsonb(dict(created_by)),
        ),
    )
    return evidence_id


def read_evidence(conn: DbConn, evidence_id: str) -> Evidence:
    """Resolve an `evidence_ref` to its immutable record. Raises KeyError if unknown."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM overlay_evidence WHERE evidence_id = %s", (evidence_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown evidence_id {evidence_id!r}")
    return Evidence(
        evidence_id=row["evidence_id"],
        fact_key=row["fact_key"],
        table_snapshot_at=row["table_snapshot_at"],
        row_count=row["row_count"],
        sample_size=row["sample_size"],
        profile_version=row["profile_version"],
        thresholds_used=row["thresholds_used"],
        metric_values=row["metric_values"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )

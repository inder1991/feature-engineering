from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn


class EvidenceProducer(str, Enum):
    """WHO produced an evidence record (§3.1). ``LEGACY`` labels evidence that predates the axis
    (or a caller whose producer is not yet classified); ``PROFILER`` is the profiling substrate that
    every pre-existing caller writes through — see the Step-0 caller audit."""

    PROFILER = "profiler"
    LLM = "llm"
    SOURCE = "source"
    HUMAN = "human"
    LEGACY = "legacy"


class AssertionStrength(str, Enum):
    """HOW strongly a producer asserts its evidence (§3.1), weakest → strongest. ``PROPOSED`` is a
    candidate awaiting confirmation; ``SUPPORTED`` is measured/observed evidence (e.g. the profiler,
    the default); ``ATTESTED`` is vouched for by a structural source; ``CONFIRMED`` is human-confirmed."""

    PROPOSED = "proposed"
    SUPPORTED = "supported"
    ATTESTED = "attested"
    CONFIRMED = "confirmed"


class EvidenceLifecycle(str, Enum):
    """Where an evidence record sits in its lifecycle (§3.1). New records are ``ACTIVE``; a record
    becomes ``STALE`` when its inputs drift, ``REJECTED`` when a gate turns it down, and
    ``SUPERSEDED`` when a newer record replaces it."""

    ACTIVE = "active"
    STALE = "stale"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


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
    producer: str
    strength: str
    lifecycle: str
    producer_configuration_hash: str | None
    producer_item_ref: str | None
    evidence_spans: tuple[str, ...]


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
    producer: EvidenceProducer = EvidenceProducer.PROFILER,
    strength: AssertionStrength = AssertionStrength.SUPPORTED,
    lifecycle: EvidenceLifecycle = EvidenceLifecycle.ACTIVE,
    producer_configuration_hash: str | None = None,
    producer_item_ref: str | None = None,
    evidence_spans: Sequence[str] = (),
) -> str:
    """Write one immutable evidence record (§5.1) and return its evidence_id. Append-only: each
    call mints a fresh `eviu_` id and INSERTs a new row — there is no update path. Stores ONLY
    aggregate metrics; callers must never pass raw values / raw MIN/MAX (Global Constraint).
    `created_by` is a Mapping persisted as jsonb — callers pass `identity_to_jsonb(actor)`,
    never a raw IdentityEnvelope.

    The producer/strength/lifecycle axis (§3.1) says WHO produced the evidence, HOW strongly it is
    asserted, and where it sits in its lifecycle. Defaults `producer=PROFILER, strength=SUPPORTED,
    lifecycle=ACTIVE` match every pre-existing caller (all write profiling evidence — see the
    Step-0 caller audit); a non-profiler producer must pass its axis explicitly. The two hashes are
    producer-specific (nullable); `evidence_spans` names the fields the evidence draws on."""
    evidence_id = mint_id("eviu")
    conn.execute(
        """
        INSERT INTO overlay_evidence
            (evidence_id, fact_key, table_snapshot_at, row_count, sample_size,
             profile_version, thresholds_used, metric_values, created_by,
             producer, strength, lifecycle, producer_configuration_hash,
             producer_item_ref, evidence_spans)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            evidence_id, fact_key, table_snapshot_at, row_count, sample_size,
            profile_version, Jsonb(dict(thresholds_used)), Jsonb(dict(metric_values)),
            Jsonb(dict(created_by)),
            producer.value, strength.value, lifecycle.value, producer_configuration_hash,
            producer_item_ref, Jsonb(list(evidence_spans)),
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
        producer=row["producer"],
        strength=row["strength"],
        lifecycle=row["lifecycle"],
        producer_configuration_hash=row["producer_configuration_hash"],
        producer_item_ref=row["producer_item_ref"],
        # jsonb round-trips as a list; the Evidence contract is a tuple[str, ...] (immutable record).
        evidence_spans=tuple(row["evidence_spans"]),
    )

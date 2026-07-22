"""P0 shadow-measurement harness — the durable, append-only (WORM) telemetry store (migration 1018).

Design: docs/superpowers/specs/2026-07-22-p0-shadow-measurement-design.md §Persistence. Measures the
false-attest rate of auto-attesting the low-risk bulk, WITHOUT ever writing to any authority store —
this module only writes the three ``attestation_*`` tables. Mirrors the Phase-3B.4 planner shadow store
(``planner/shadow_store.py``, migration 0999): the same ``canonical_json``/``payload_hash`` helpers, the
same append-only (``ON CONFLICT DO NOTHING``) writer discipline, and the same manifest<->observation
reconcile pattern.

Three tables:
  * ``attestation_gold_label``       — human ground truth. PK (logical_ref, field_name); a re-submission
                                        of an existing key is a no-op (never an update).
  * ``attestation_shadow_run``       — one row per run (the dispatch manifest): catalog, gold-set
                                        version, model/signal versions, and the declared ``column_count``
                                        reconcile compares captured observations against.
  * ``attestation_shadow_observation`` — one row per (run, logical_ref, field_name). Stores NO gold
                                        value — correctness is a READ-TIME JOIN to
                                        ``attestation_gold_label``, so an observation is never
                                        contaminated by the label and can be re-scored against a
                                        corrected gold set.

WORM is enforced in the migration by a ``BEFORE UPDATE OR DELETE`` row trigger (blocks every role,
including a superuser) plus a guarded ``REVOKE`` for the production app role (the TRUNCATE control).
This module only ever INSERTs.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb


def canonical_json(obj: Any) -> str:
    """Deterministic, sorted-key JSON — the storage-integrity + hashing serialization."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


# ── row contracts ──
@dataclass(frozen=True, slots=True)
class ShadowRunV1:
    shadow_run_id: str
    catalog_source: str
    gold_version_hash: str
    model_ids: Mapping[str, str]
    signal_versions: Mapping[str, str]
    started_at: datetime
    column_count: int


@dataclass(frozen=True, slots=True)
class ObservationV1:
    shadow_run_id: str
    logical_ref: str
    field_name: str
    proposer_value: str | None
    proposer_producer: str | None
    reclassify_value: str | None
    reclassify_agrees: bool | None
    grounding_checks: Mapping[str, Any]
    grounding_coverage: float
    grounding_conflict: bool
    confidence: float
    risk_tier: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileV1:
    shadow_run_id: str
    expected: int
    present: int

    @property
    def complete(self) -> bool:
        return self.present == self.expected


def write_gold_label(conn, *, catalog_source: str, logical_ref: str, field_name: str,
                     gold_value: str, labeller_ids: list[str], adjudicated_by: str,
                     notes: str | None = None) -> None:
    """Append the human ground-truth row. Idempotent on (logical_ref, field_name) — a re-submission of
    the same key is a silent no-op, never an update (the table is append-only WORM)."""
    payload = {"gold_value": gold_value, "labeller_ids": list(labeller_ids),
              "adjudicated_by": adjudicated_by, "notes": notes}
    conn.execute(
        "INSERT INTO attestation_gold_label "
        "(catalog_source, logical_ref, field_name, gold_value, labeller_ids, adjudicated_by, notes, "
        " payload_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (logical_ref, field_name) DO NOTHING",
        (catalog_source, logical_ref, field_name, gold_value, Jsonb(list(labeller_ids)),
         adjudicated_by, notes, payload_hash(payload)))


def write_shadow_run(conn, rec: ShadowRunV1) -> None:
    """Append the run manifest — idempotent on shadow_run_id."""
    model_ids = dict(rec.model_ids)
    signal_versions = dict(rec.signal_versions)
    payload = {"catalog_source": rec.catalog_source, "gold_version_hash": rec.gold_version_hash,
              "model_ids": model_ids, "signal_versions": signal_versions,
              "column_count": rec.column_count}
    conn.execute(
        "INSERT INTO attestation_shadow_run "
        "(shadow_run_id, catalog_source, gold_version_hash, model_ids, signal_versions, started_at, "
        " column_count, payload_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (shadow_run_id) DO NOTHING",
        (rec.shadow_run_id, rec.catalog_source, rec.gold_version_hash, Jsonb(model_ids),
         Jsonb(signal_versions), rec.started_at, rec.column_count, payload_hash(payload)))


def write_observation(conn, obs: ObservationV1) -> None:
    """Append one per-column observation — idempotent on (shadow_run_id, logical_ref, field_name).
    Carries NO gold value: correctness is computed at read time via a join to
    ``attestation_gold_label``."""
    grounding_checks = dict(obs.grounding_checks)
    payload = {"proposer_value": obs.proposer_value, "proposer_producer": obs.proposer_producer,
              "reclassify_value": obs.reclassify_value, "reclassify_agrees": obs.reclassify_agrees,
              "grounding_checks": grounding_checks, "grounding_coverage": obs.grounding_coverage,
              "grounding_conflict": obs.grounding_conflict, "confidence": obs.confidence,
              "risk_tier": obs.risk_tier}
    conn.execute(
        "INSERT INTO attestation_shadow_observation "
        "(shadow_run_id, logical_ref, field_name, proposer_value, proposer_producer, "
        " reclassify_value, reclassify_agrees, grounding_checks, grounding_coverage, "
        " grounding_conflict, confidence, risk_tier, payload_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (shadow_run_id, logical_ref, field_name) DO NOTHING",
        (obs.shadow_run_id, obs.logical_ref, obs.field_name, obs.proposer_value,
         obs.proposer_producer, obs.reclassify_value, obs.reclassify_agrees, Jsonb(grounding_checks),
         obs.grounding_coverage, obs.grounding_conflict, obs.confidence, obs.risk_tier,
         payload_hash(payload), obs.created_at))


def reconcile(conn, shadow_run_id: str) -> ReconcileV1:
    """The DURABLE capture-integrity signal: the run's declared ``column_count`` vs the distinct
    (logical_ref, field_name) observations actually captured for it."""
    run = conn.execute(
        "SELECT column_count FROM attestation_shadow_run WHERE shadow_run_id = %s",
        (shadow_run_id,)).fetchone()
    if run is None:
        return ReconcileV1(shadow_run_id, expected=0, present=0)
    present = conn.execute(
        "SELECT count(DISTINCT (logical_ref, field_name)) FROM attestation_shadow_observation "
        "WHERE shadow_run_id = %s", (shadow_run_id,)).fetchone()[0]
    return ReconcileV1(shadow_run_id, expected=int(run[0]), present=int(present))

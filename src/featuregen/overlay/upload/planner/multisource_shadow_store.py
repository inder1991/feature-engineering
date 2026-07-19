"""Phase 3C.2b-i-A · Task 10 — the durable, append-only (WORM) multi-source assembly shadow store.

Mirrors the single-source ``shadow_store.py`` (migration 0999) — a dispatch MANIFEST (the expected
intent-id set, written first), one INTENT_RESULT per (run, intent), and, extending 0999, one
CANDIDATE per candidate plan + one OPERAND_OBS per (plan, slot). Migration ``1010``. The store is
planner telemetry — NOT a governed overlay_fact — and is written only when the multi-source assembly
shadow flag is on. Capture integrity is proven by reconciling the manifest against the intent_result
rows (the durable loss signal — never a circular self-report).

Two orthogonal disciplines on every write:
  * ``write_manifest`` / ``write_intent_result`` are IDEMPOTENT on their key. A re-write with the SAME
    payload is a no-op; a re-write with a DIFFERENT payload is a validated CONFLICT — detected by
    reading the stored ``payload_hash`` back and comparing, and raised as ``DivergentDuplicateError``.
    NEVER a silent ``ON CONFLICT DO NOTHING`` on conflicting telemetry.
  * ``write_intent_result`` is TWO-PHASE (mirror 0999 ``write_run_and_plans``): (1) an atomic
    parent+children write with ``capture_status = persisted``; (2) on failure, a fresh minimal-parent
    ``persistence_partial`` row (no children); if THAT fails, re-raise (the caller catches it and
    relies on manifest reconciliation).

The FOUR axes are recorded as SEPARATE columns and are NEVER collapsed: a plan can be assembly-axis
``resolved`` (``semantic_outcome``) while contract-axis ``incomplete`` (``compile_completeness`` —
stale / safety-gapped). Read-only; the WORM ``REVOKE`` in the migration enforces append-only in
production.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

PAYLOAD_SCHEMA_VERSION = "1.0.0"


# ── the store's four orthogonal-axis enums (telemetry vocab, not planner MultiSourceReason) ──
class SemanticOutcome(StrEnum):
    """The ASSEMBLY axis: was a governed plan assembled (``resolved``), or which semantic gate failed?
    ``not_evaluated`` = the semantic axis was never reached (a technical failure preceded assembly)."""
    resolved = "resolved"
    operand_shape_invalid = "operand_shape_invalid"
    unsupported_path_aggregation = "unsupported_path_aggregation"
    ordering_anchor_missing = "ordering_anchor_missing"
    no_governed_path = "no_governed_path"
    realization_endpoint_ungoverned = "realization_endpoint_ungoverned"
    no_common_physical_grain = "no_common_physical_grain"
    ambiguous_physical_grain = "ambiguous_physical_grain"
    aggregation_unsafe_on_path = "aggregation_unsafe_on_path"
    temporal_paths_incompatible = "temporal_paths_incompatible"
    source_binding_ungoverned = "source_binding_ungoverned"
    not_evaluated = "not_evaluated"


class CompileCompleteness(StrEnum):
    """The CONTRACT axis, distilled from ``ContractResolutionStatus``. A plan may be assembly-``resolved``
    yet contract-``incomplete`` (stale / safety gap / unresolved declaration) — that is NOT operationally
    resolved. ``not_applicable`` = no assembled plan to compile."""
    complete = "complete"
    incomplete = "incomplete"
    not_applicable = "not_applicable"


class TechnicalStatus(StrEnum):
    ok = "ok"
    operand_or_slot_not_preserved = "operand_or_slot_not_preserved"
    technical_failure = "technical_failure"
    budget_truncated = "budget_truncated"


class CaptureStatus(StrEnum):
    persisted = "persisted"
    persistence_partial = "persistence_partial"


class DivergentDuplicateError(Exception):
    """A re-insert whose content differs from the already-stored row for the same key — never silently
    overwritten (the tables are append-only)."""


# ── row contracts ──
@dataclass(frozen=True, slots=True)
class ManifestRecordV1:
    run_id: str | None
    expected_intent_ids: tuple[str, ...]
    versions: Mapping[str, str]
    shadow_flag: bool
    producer_commit: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IntentResultRowV1:
    run_id: str | None
    intent_id: str
    semantic_outcome: SemanticOutcome
    compile_completeness: CompileCompleteness
    technical_status: TechnicalStatus
    capture_status: CaptureStatus      # carried for axis completeness; the STORE sets the persisted value
    normalized_intent_hash: str
    selected_plan_id: str | None
    reason_codes: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateRowV1:
    run_id: str | None
    intent_id: str
    plan_id: str
    physical_landing: Mapping[str, Any]
    contract_input_hash: str
    contract_output_hash: str
    read_set_hash: str
    replay_envelope_hash: str
    rank: int
    declaration_evidence: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperandObservationRowV1:
    run_id: str | None
    intent_id: str
    plan_id: str
    slot_id: str
    pin: Mapping[str, Any]
    role: str
    path_strategy: Mapping[str, Any]
    governed_endpoints: Sequence[Any]
    source_binding: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileResultV1:
    run_id: str | None
    expected: int
    present: int
    missing_intent_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_intent_ids and self.expected == self.present


def canonical_json(obj: Any) -> str:
    """Deterministic, sorted-key JSON — the storage-integrity + hashing serialization."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _versions_dict(rec: ManifestRecordV1) -> dict[str, str]:
    return {str(k): str(v) for k, v in sorted(rec.versions.items())}


def _manifest_payload(rec: ManifestRecordV1) -> dict[str, Any]:
    return {
        "run_id": rec.run_id,
        "expected_intent_ids": list(rec.expected_intent_ids),
        "versions": _versions_dict(rec),
        "shadow_flag": rec.shadow_flag,
        "producer_commit": rec.producer_commit,
    }


def _intent_payload(intent: IntentResultRowV1,
                    candidates: Sequence[CandidateRowV1],
                    operands: Sequence[OperandObservationRowV1]) -> dict[str, Any]:
    """The composite telemetry payload for the (run, intent) key. Excludes ``capture_status`` (the store
    determines it via the two-phase protocol) but INCLUDES the three caller-supplied axes + every child
    row, so a divergent axis OR a divergent candidate/operand is a detectable conflict."""
    return {
        "run_id": intent.run_id,
        "intent_id": intent.intent_id,
        "semantic_outcome": intent.semantic_outcome.value,
        "compile_completeness": intent.compile_completeness.value,
        "technical_status": intent.technical_status.value,
        "normalized_intent_hash": intent.normalized_intent_hash,
        "selected_plan_id": intent.selected_plan_id,
        "reason_codes": list(intent.reason_codes),
        "candidates": sorted(
            ({
                "plan_id": c.plan_id,
                "physical_landing": dict(c.physical_landing),
                "contract_input_hash": c.contract_input_hash,
                "contract_output_hash": c.contract_output_hash,
                "read_set_hash": c.read_set_hash,
                "replay_envelope_hash": c.replay_envelope_hash,
                "rank": c.rank,
                "declaration_evidence": dict(c.declaration_evidence),
            } for c in candidates),
            key=lambda d: d["plan_id"],
        ),
        "operands": sorted(
            ({
                "plan_id": o.plan_id,
                "slot_id": o.slot_id,
                "pin": dict(o.pin),
                "role": o.role,
                "path_strategy": dict(o.path_strategy),
                "governed_endpoints": list(o.governed_endpoints),
                "source_binding": dict(o.source_binding),
            } for o in operands),
            key=lambda d: (d["plan_id"], d["slot_id"]),
        ),
    }


def write_manifest(conn, rec: ManifestRecordV1) -> None:
    """Write the run manifest — idempotent on ``run_id``; a divergent re-write raises."""
    versions = _versions_dict(rec)
    phash = payload_hash(_manifest_payload(rec))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_dispatch "
        "(run_id, expected_intent_ids, expected_count, versions, versions_hash, shadow_flag, "
        " producer_commit, payload_hash, payload_schema_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id) DO NOTHING",
        (rec.run_id, Jsonb(list(rec.expected_intent_ids)), len(rec.expected_intent_ids),
         Jsonb(versions), payload_hash(versions), rec.shadow_flag, rec.producer_commit,
         phash, PAYLOAD_SCHEMA_VERSION, rec.created_at))
    row = conn.execute(
        "SELECT payload_hash FROM multisource_assembly_shadow_dispatch WHERE run_id = %s",
        (rec.run_id,)).fetchone()
    if row is not None and row[0] != phash:
        raise DivergentDuplicateError(
            f"manifest {rec.run_id}: payload_hash conflict (stored {row[0]!r} != {phash!r})")


def _insert_intent_result(conn, r: IntentResultRowV1, capture: CaptureStatus, phash: str) -> None:
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_intent_result "
        "(run_id, intent_id, semantic_outcome, compile_completeness, technical_status, capture_status, "
        " normalized_intent_hash, selected_plan_id, reason_codes, payload_hash, payload_schema_version, "
        " created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id, intent_id) DO NOTHING",
        (r.run_id, r.intent_id, r.semantic_outcome.value, r.compile_completeness.value,
         r.technical_status.value, capture.value, r.normalized_intent_hash, r.selected_plan_id,
         Jsonb(list(r.reason_codes)), phash, PAYLOAD_SCHEMA_VERSION, r.created_at))


def _insert_candidate(conn, c: CandidateRowV1) -> None:
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_candidate "
        "(run_id, intent_id, plan_id, physical_landing, contract_input_hash, contract_output_hash, "
        " read_set_hash, replay_envelope_hash, rank, declaration_evidence, payload_schema_version, "
        " created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id, intent_id, plan_id) DO NOTHING",
        (c.run_id, c.intent_id, c.plan_id, Jsonb(dict(c.physical_landing)), c.contract_input_hash,
         c.contract_output_hash, c.read_set_hash, c.replay_envelope_hash, c.rank,
         Jsonb(dict(c.declaration_evidence)), PAYLOAD_SCHEMA_VERSION, c.created_at))


def _insert_operand(conn, o: OperandObservationRowV1) -> None:
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_operand_obs "
        "(run_id, intent_id, plan_id, slot_id, pin, role, path_strategy, governed_endpoints, "
        " source_binding, payload_schema_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id, intent_id, plan_id, slot_id) DO NOTHING",
        (o.run_id, o.intent_id, o.plan_id, o.slot_id, Jsonb(dict(o.pin)), o.role,
         Jsonb(dict(o.path_strategy)), Jsonb(list(o.governed_endpoints)), Jsonb(dict(o.source_binding)),
         PAYLOAD_SCHEMA_VERSION, o.created_at))


def write_intent_result(conn, intent_row: IntentResultRowV1,
                        candidate_rows: Sequence[CandidateRowV1],
                        operand_rows: Sequence[OperandObservationRowV1]) -> CaptureStatus:
    """Persist one (run, intent) result + its candidates + operands.

    Divergent-duplicate FIRST: a stored row for this key with a DIFFERENT payload hash raises; the SAME
    payload hash is idempotent (returns the stored capture status; no children re-write). Otherwise the
    two-phase protocol: (1) atomic parent+children (``persisted``); (2) on failure, a fresh minimal
    parent (``persistence_partial``); if (2) also fails, re-raise (caller relies on reconciliation)."""
    phash = payload_hash(_intent_payload(intent_row, candidate_rows, operand_rows))
    existing = conn.execute(
        "SELECT payload_hash, capture_status FROM multisource_assembly_shadow_intent_result "
        "WHERE run_id = %s AND intent_id = %s",
        (intent_row.run_id, intent_row.intent_id)).fetchone()
    if existing is not None:
        if existing[0] != phash:
            raise DivergentDuplicateError(
                f"intent_result {intent_row.run_id}/{intent_row.intent_id}: payload_hash conflict "
                f"(stored {existing[0]!r} != {phash!r})")
        return CaptureStatus(existing[1])   # same payload — idempotent no-op

    try:
        with conn.transaction():
            _insert_intent_result(conn, intent_row, CaptureStatus.persisted, phash)
            for c in candidate_rows:
                _insert_candidate(conn, c)
            for o in operand_rows:
                _insert_operand(conn, o)
        return CaptureStatus.persisted
    except Exception:
        # Phase 2: a fresh savepoint with ONLY the minimal parent recording the partial capture.
        with conn.transaction():
            _insert_intent_result(conn, intent_row, CaptureStatus.persistence_partial, phash)
        return CaptureStatus.persistence_partial


def reconcile(conn, run_id: str) -> ReconcileResultV1:
    """The DURABLE loss signal: every manifest intent id must have an intent_result row."""
    disp = conn.execute(
        "SELECT expected_intent_ids, expected_count FROM multisource_assembly_shadow_dispatch "
        "WHERE run_id = %s", (run_id,)).fetchone()
    if disp is None:
        return ReconcileResultV1(run_id, expected=0, present=0, missing_intent_ids=())
    expected_ids: list[str] = list(disp[0])
    expected = int(disp[1])
    present_rows = conn.execute(
        "SELECT intent_id FROM multisource_assembly_shadow_intent_result WHERE run_id = %s",
        (run_id,)).fetchall()
    present_ids = {row[0] for row in present_rows}
    missing = tuple(sorted(i for i in expected_ids if i not in present_ids))
    return ReconcileResultV1(run_id, expected=expected, present=len(present_ids),
                             missing_intent_ids=missing)


def read_intent_results(conn, run_id: str) -> list[dict[str, Any]]:
    cols = ("run_id", "intent_id", "semantic_outcome", "compile_completeness", "technical_status",
            "capture_status", "normalized_intent_hash", "selected_plan_id", "reason_codes")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM multisource_assembly_shadow_intent_result WHERE run_id = %s "
        "ORDER BY intent_id", (run_id,)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]


def read_candidates(conn, run_id: str, intent_id: str) -> list[dict[str, Any]]:
    cols = ("run_id", "intent_id", "plan_id", "physical_landing", "contract_input_hash",
            "contract_output_hash", "read_set_hash", "replay_envelope_hash", "rank",
            "declaration_evidence")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM multisource_assembly_shadow_candidate "
        "WHERE run_id = %s AND intent_id = %s ORDER BY rank, plan_id", (run_id, intent_id)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]


def read_operands(conn, run_id: str, intent_id: str, plan_id: str) -> list[dict[str, Any]]:
    cols = ("run_id", "intent_id", "plan_id", "slot_id", "pin", "role", "path_strategy",
            "governed_endpoints", "source_binding")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM multisource_assembly_shadow_operand_obs "
        "WHERE run_id = %s AND intent_id = %s AND plan_id = %s ORDER BY slot_id",
        (run_id, intent_id, plan_id)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]

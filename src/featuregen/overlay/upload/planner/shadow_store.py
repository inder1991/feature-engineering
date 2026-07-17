"""Phase-3B.4 — the durable, append-only (WORM) shadow-telemetry store.

Persists the shadow contract classifier's output as three append-only tables (migration 0998):
a dispatch MANIFEST (the expected eligible set, written first), one recipe-level RUN_RESULT per
(run, recipe), and one PLAN_OBSERVATION per candidate physical plan. The store is planner telemetry
— NOT a governed overlay_fact — and is written only when the shadow-telemetry flag is on. Capture
integrity is proven by reconciling the manifest against the run-result rows.

Write discipline:
  * ``write_dispatch`` — the manifest, written BEFORE scope resolution (so a pre-loop failure is visible).
  * ``write_run_and_plans`` — a TWO-PHASE protocol: an atomic parent+children write; on failure, a fresh
    minimal-parent ``persistence_partial`` row; if THAT fails, re-raise (the caller catches it and relies
    on manifest reconciliation — never a circular self-report).
  * All inserts are idempotent (``ON CONFLICT DO NOTHING``); a divergent duplicate is a validated conflict.
Read-only; the WORM ``REVOKE`` in the migration enforces append-only in production.
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


# ── the store's orthogonal-axis enums (F10-review: telemetry vocab, not planner ReasonCodes) ──
class PlannerOutcome(StrEnum):
    compiled = "compiled"
    no_physical_plan = "no_physical_plan"
    internal_error = "internal_error"
    no_authorized_catalog = "no_authorized_catalog"
    template_not_found = "template_not_found"
    preloop_failure = "preloop_failure"


class CompileStatus(StrEnum):
    complete = "complete"
    incomplete = "incomplete"
    not_applicable = "not_applicable"       # no path-resolved candidate to compile
    compile_disabled = "compile_disabled"   # telemetry on, compile off, >=1 path-resolved candidate (fails Gate 1)


class IncompleteReason(StrEnum):
    budget_count = "budget_count"
    budget_time = "budget_time"
    error = "error"


class CaptureStatus(StrEnum):
    persisted = "persisted"
    persistence_partial = "persistence_partial"


class DivergentDuplicateError(Exception):
    """A re-insert whose content differs from the already-stored row for the same key — never silently
    overwritten (the tables are append-only)."""


# ── row contracts ──
@dataclass(frozen=True, slots=True)
class DispatchRecordV1:
    generation_run_id: str | None
    eligible_recipe_ids: tuple[str, ...]
    recipe_hash: str
    expected_count: int
    invocation_predicate: str
    compile_flag: bool
    telemetry_flag: bool
    applicability_version: str
    producer_commit: str
    compiler_versions: Mapping[str, str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunResultRowV1:
    generation_run_id: str | None
    recipe_id: str
    catalog_scope_id: str | None
    planner_input_hash: str | None
    planner_outcome: PlannerOutcome
    compile_status: CompileStatus
    incomplete_reason: IncompleteReason | None
    path_resolved_eligible: int
    compiled_count: int
    skipped_count: int
    capture_status: CaptureStatus
    selected_contract_physical_plan_id: str | None
    selected_contract_id: str | None
    contract_result_status: str | None
    bounding: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PlanObservationRowV1:
    generation_run_id: str | None
    recipe_id: str
    physical_plan_id: str
    path_resolution_status: str
    is_compiled: bool
    contract_id: str | None
    contract_input_hash: str | None
    contract_resolution_status: str | None
    declaration_status: str | None
    contract_primary_reason_code: str | None
    contract_reason_codes: tuple[str, ...]
    bridge_count: int
    tier: str
    preference_rank: int
    declarations: Mapping[str, Any] | None
    declarations_output_hash: str | None
    replay_stamp: Mapping[str, Any] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileResultV1:
    generation_run_id: str | None
    expected: int
    present: int
    missing_recipe_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_recipe_ids and self.expected == self.present


def canonical_json(obj: Any) -> str:
    """Deterministic, sorted-key JSON — the storage-integrity + hashing serialization."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _versions_dict(rec: DispatchRecordV1) -> dict[str, str]:
    return {str(k): str(v) for k, v in sorted(rec.compiler_versions.items())}


def write_dispatch(conn, rec: DispatchRecordV1) -> None:
    """Write the run manifest — idempotent on generation_run_id; a divergent re-write is a conflict."""
    versions = _versions_dict(rec)
    conn.execute(
        "INSERT INTO planner_shadow_dispatch "
        "(generation_run_id, eligible_recipe_ids, recipe_hash, expected_count, invocation_predicate, "
        " compile_flag, telemetry_flag, applicability_version, producer_commit, compiler_versions, "
        " compiler_versions_hash, payload_schema_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (generation_run_id) DO NOTHING",
        (rec.generation_run_id, list(rec.eligible_recipe_ids), rec.recipe_hash, rec.expected_count,
         rec.invocation_predicate, rec.compile_flag, rec.telemetry_flag, rec.applicability_version,
         rec.producer_commit, Jsonb(versions), payload_hash(versions), PAYLOAD_SCHEMA_VERSION,
         rec.created_at))
    row = conn.execute(
        "SELECT recipe_hash FROM planner_shadow_dispatch WHERE generation_run_id = %s",
        (rec.generation_run_id,)).fetchone()
    if row is not None and row[0] != rec.recipe_hash:
        raise DivergentDuplicateError(
            f"dispatch {rec.generation_run_id}: recipe_hash conflict (stored {row[0]!r} != {rec.recipe_hash!r})")


def _insert_run_result(conn, r: RunResultRowV1, capture: CaptureStatus) -> None:
    conn.execute(
        "INSERT INTO planner_shadow_run_result "
        "(generation_run_id, recipe_id, catalog_scope_id, planner_input_hash, planner_outcome, "
        " compile_status, incomplete_reason, path_resolved_eligible, compiled_count, skipped_count, "
        " capture_status, selected_contract_physical_plan_id, selected_contract_id, contract_result_status, "
        " bounding, payload_schema_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (generation_run_id, recipe_id) DO NOTHING",
        (r.generation_run_id, r.recipe_id, r.catalog_scope_id, r.planner_input_hash,
         r.planner_outcome.value, r.compile_status.value,
         r.incomplete_reason.value if r.incomplete_reason is not None else None,
         r.path_resolved_eligible, r.compiled_count, r.skipped_count, capture.value,
         r.selected_contract_physical_plan_id, r.selected_contract_id, r.contract_result_status,
         Jsonb(dict(r.bounding)), PAYLOAD_SCHEMA_VERSION, r.created_at))


def _insert_observation(conn, o: PlanObservationRowV1) -> None:
    conn.execute(
        "INSERT INTO planner_shadow_plan_observation "
        "(generation_run_id, recipe_id, physical_plan_id, path_resolution_status, is_compiled, "
        " contract_id, contract_input_hash, contract_resolution_status, declaration_status, "
        " contract_primary_reason_code, contract_reason_codes, bridge_count, tier, preference_rank, "
        " declarations, declarations_output_hash, replay_stamp, payload_schema_version, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (generation_run_id, recipe_id, physical_plan_id) DO NOTHING",
        (o.generation_run_id, o.recipe_id, o.physical_plan_id, o.path_resolution_status, o.is_compiled,
         o.contract_id, o.contract_input_hash, o.contract_resolution_status, o.declaration_status,
         o.contract_primary_reason_code, list(o.contract_reason_codes), o.bridge_count, o.tier,
         o.preference_rank, Jsonb(dict(o.declarations)) if o.declarations is not None else None,
         o.declarations_output_hash, Jsonb(dict(o.replay_stamp)) if o.replay_stamp is not None else None,
         PAYLOAD_SCHEMA_VERSION, o.created_at))


def write_run_and_plans(conn, run_result: RunResultRowV1,
                        observations: Sequence[PlanObservationRowV1]) -> CaptureStatus:
    """Two-phase persistence (review F6/F11): (1) atomic parent + children; (2) on failure, a fresh
    minimal-parent ``persistence_partial`` row (no children). If (2) also fails, re-raise — the caller
    catches it and the manifest reconciliation surfaces the loss (no circular self-report)."""
    try:
        with conn.transaction():
            _insert_run_result(conn, run_result, CaptureStatus.persisted)
            for o in observations:
                _insert_observation(conn, o)
        return CaptureStatus.persisted
    except Exception:
        # Phase 2: a fresh savepoint with ONLY the minimal parent recording the partial capture.
        with conn.transaction():
            _insert_run_result(conn, run_result, CaptureStatus.persistence_partial)
        return CaptureStatus.persistence_partial


def reconcile(conn, generation_run_id: str) -> ReconcileResultV1:
    """The DURABLE loss signal: every manifest recipe must have a run-result row."""
    disp = conn.execute(
        "SELECT eligible_recipe_ids, expected_count FROM planner_shadow_dispatch WHERE generation_run_id = %s",
        (generation_run_id,)).fetchone()
    if disp is None:
        return ReconcileResultV1(generation_run_id, expected=0, present=0, missing_recipe_ids=())
    eligible: list[str] = list(disp[0])
    expected = int(disp[1])
    present_rows = conn.execute(
        "SELECT recipe_id FROM planner_shadow_run_result WHERE generation_run_id = %s",
        (generation_run_id,)).fetchall()
    present_ids = {row[0] for row in present_rows}
    missing = tuple(sorted(r for r in eligible if r not in present_ids))
    return ReconcileResultV1(generation_run_id, expected=expected, present=len(present_ids),
                             missing_recipe_ids=missing)


def read_run_results(conn, generation_run_id: str) -> list[dict[str, Any]]:
    cols = ("generation_run_id", "recipe_id", "catalog_scope_id", "planner_input_hash", "planner_outcome",
            "compile_status", "incomplete_reason", "path_resolved_eligible", "compiled_count",
            "skipped_count", "capture_status", "selected_contract_physical_plan_id", "selected_contract_id",
            "contract_result_status", "bounding")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM planner_shadow_run_result WHERE generation_run_id = %s "
        "ORDER BY recipe_id", (generation_run_id,)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]


def read_observations(conn, generation_run_id: str) -> list[dict[str, Any]]:
    cols = ("recipe_id", "physical_plan_id", "path_resolution_status", "is_compiled", "contract_id",
            "contract_input_hash", "contract_resolution_status", "declaration_status", "tier",
            "preference_rank")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM planner_shadow_plan_observation WHERE generation_run_id = %s "
        "ORDER BY recipe_id, physical_plan_id", (generation_run_id,)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]

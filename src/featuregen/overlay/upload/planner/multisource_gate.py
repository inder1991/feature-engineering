"""Phase 3C.2b-i-A · Task 12 — the multi-source assembly GATE (spec §10).

The measurement that decides whether the governed multi-source assembler is trustworthy. It drives the
Task-11 two-connection shadow HARNESS over the Task-12 CORRECTNESS gold TWICE (distinct ``run_id``s),
re-seeding the deterministic gold fixture before each run, then evaluates the persisted telemetry over
the CLEAN (correctness) population against the spec §10 criteria:

  1. POSITIVE coverage is mandatory — ≥ ``MULTISOURCE_GOLD_MIN_SHAPES`` (6) distinct authoritative
     shapes each RESOLVE (a reject-everything assembler FAILS: positives MUST resolve).
  2. ZERO operand substitution/loss — every intent operand (incl. ordered slots) survives on the
     resolved plan exactly once, its pin + per-slot ``path_strategy`` preserved verbatim.
  3. ZERO non-governed crossings/endpoints in a resolve — every operand path carries governed
     endpoints (each a VERIFIED grain fact), asserted on the persisted ``governed_endpoints`` evidence
     (a VERIFIED-only frontier implies governed crossings; the endpoints are asserted directly).
  4. ONE physical grain — every operand's landing endpoint IS the plan's single ``physical_landing``.
  5. Correct per-path aggregation/temporal — the persisted ``path_strategy`` matches the authored one
     (a ``take_latest`` slot carries its ordering anchor).
  6. DETERMINISTIC identity — identical ``selected_plan_id`` / ``replay_envelope_hash`` / contract
     hashes across the two runs (the replay envelope keys on stable authored fact_keys).
  7. COMPLETE reconciliation — every manifest intent id has a result row, both runs.
  8. NO technical failures or truncation in the clean population — the fault-observability controls are
     a SEPARATE partition; a technical/truncation reading here is a FAILURE.

Resolution RATE is DESCRIPTIVE only (negatives resolve to a reject by design). The gate is not vacuous:
:func:`evaluate_gate_over_runs` FAILS when positive coverage is absent (reject-all) or a
technical/truncation reading leaks into the clean population — the Task-12 test proves both.

Read-only over every reused surface (the harness, the Task-10 store readers) — this module only drives
and measures; nothing here edits a reused engine module (§12 behaviour-neutrality)."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.planner.contracts import MULTISOURCE_GOLD_MIN_SHAPES
from featuregen.overlay.upload.planner.multisource_gold import (
    CORRECTNESS_GOLD,
    GOLD_NOW,
    GoldCaseV1,
    seed_gold,
)
from featuregen.overlay.upload.planner.multisource_shadow import (
    run_multisource_assembly_shadow,
)
from featuregen.overlay.upload.planner.multisource_shadow_store import (
    CompileCompleteness,
    SemanticOutcome,
    TechnicalStatus,
    read_candidates,
    read_intent_results,
    read_operands,
    reconcile,
)

_DEFAULT_RUN_IDS = ("mgate_run_a", "mgate_run_b")
_DEFAULT_ROLES = ("feature_engineer",)


@dataclass(frozen=True, slots=True)
class AssemblyGateResultV1:
    """The gate verdict over the CLEAN correctness population across the double run (spec §10).

    ``passed`` iff EVERY criterion held. Each boolean is one spec §10 criterion; ``failures`` carries a
    human-readable reason per breach (empty iff passed). ``positive_shapes_covered`` is the set of
    authoritative shapes that resolved in EVERY run; ``resolution_rate`` is DESCRIPTIVE (negatives
    resolve to a reject by design, so it is never a pass/fail input)."""
    passed: bool
    run_ids: tuple[str, ...]
    positive_shapes_covered: tuple[str, ...]
    positive_coverage_ok: bool
    outcomes_match_expected: bool
    operand_preservation_ok: bool
    governed_endpoints_ok: bool
    one_grain_landing_ok: bool
    aggregation_temporal_ok: bool
    deterministic_identity_ok: bool
    reconciliation_complete: bool
    no_technical_or_truncation: bool
    resolution_rate: float
    failures: tuple[str, ...] = field(default_factory=tuple)


# ── serialization shapes (mirror the harness ``_*_dict`` so expected == persisted) ────────────────
def _expected_strategy(op) -> dict[str, Any]:
    s = op.path_strategy
    return {"aggregation": s.aggregation.value, "output_type": s.output_type,
            "output_additivity": s.output_additivity.value,
            "external_type_required": s.external_type_required,
            "ordering_anchor_concept": s.ordering_anchor_concept}


def _expected_pin(op) -> dict[str, Any]:
    return {"catalog_source": op.catalog_source, "object_ref": op.object_ref,
            "concept": op.authoritative_concept}


def _expected_landing(case: GoldCaseV1) -> dict[str, Any]:
    lg = case.expected_landing
    assert lg is not None
    return {"catalog": lg.catalog, "table_ref": lg.table_ref,
            "grain_key_refs": list(lg.grain_key_refs)}


# ── the pure evaluator (reads persisted telemetry only — no planning, no DB writes) ───────────────
def evaluate_gate_over_runs(telemetry_conn, *, run_ids: Sequence[str],
                            cases: Sequence[GoldCaseV1] = CORRECTNESS_GOLD) -> AssemblyGateResultV1:
    """Evaluate the spec §10 gate over the ALREADY-PERSISTED telemetry for ``run_ids`` (the clean
    correctness population). Pure over the store — it drives no planner and writes nothing, so a test
    can call it against poked telemetry to prove the gate is not vacuous."""
    run_ids = tuple(run_ids)
    cases = tuple(cases)
    failures: list[str] = []

    # index the persisted intent_result rows per run (case_id -> row)
    results_by_run: dict[str, dict[str, dict[str, Any]]] = {
        rid: {row["intent_id"]: row for row in read_intent_results(telemetry_conn, rid)}
        for rid in run_ids}

    # (7) reconciliation complete for every run
    reconciliation_complete = True
    for rid in run_ids:
        rec = reconcile(telemetry_conn, rid)
        if not rec.complete:
            reconciliation_complete = False
            failures.append(f"reconcile incomplete run={rid} missing={rec.missing_intent_ids}")

    outcomes_match = True
    no_technical = True
    preservation_ok = True
    endpoints_ok = True
    one_grain_ok = True
    agg_temporal_ok = True
    resolved_positive = 0
    total = 0

    for rid in run_ids:
        rows = results_by_run[rid]
        for case in cases:
            total += 1
            row = rows.get(case.case_id)
            if row is None:
                outcomes_match = False
                failures.append(f"missing result run={rid} case={case.case_id}")
                continue

            # (core) exact expected disposition on the assembly axis
            if row["semantic_outcome"] != case.expected_outcome.value:
                outcomes_match = False
                failures.append(
                    f"outcome mismatch run={rid} case={case.case_id}: "
                    f"got {row['semantic_outcome']} want {case.expected_outcome.value}")

            # (8) no technical failure / truncation in the CLEAN population
            if row["technical_status"] != TechnicalStatus.ok.value:
                no_technical = False
                failures.append(
                    f"technical/truncation in clean pop run={rid} case={case.case_id}: "
                    f"{row['technical_status']}")

            if not case.is_positive:
                continue
            if row["semantic_outcome"] == SemanticOutcome.resolved.value:
                resolved_positive += 1

            # POSITIVE: contract compile complete + exact landing + preservation + endpoints
            if row["compile_completeness"] != CompileCompleteness.complete.value:
                agg_temporal_ok = False
                failures.append(
                    f"positive not contract-complete run={rid} case={case.case_id}: "
                    f"{row['compile_completeness']}")

            candidates = read_candidates(telemetry_conn, rid, case.case_id)
            if not candidates:
                preservation_ok = False
                failures.append(f"positive has no candidate run={rid} case={case.case_id}")
                continue
            candidate = candidates[0]
            landing = dict(candidate["physical_landing"])
            expected_landing = _expected_landing(case)
            if landing != expected_landing:
                one_grain_ok = False
                failures.append(
                    f"landing mismatch run={rid} case={case.case_id}: "
                    f"got {landing} want {expected_landing}")

            operands = read_operands(telemetry_conn, rid, case.case_id, candidate["plan_id"])
            persisted_slots = {o["slot_id"] for o in operands}
            expected_slots = {op.slot_id for op in case.intent.operands}
            if persisted_slots != expected_slots or len(operands) != len(case.intent.operands):
                preservation_ok = False
                failures.append(
                    f"operand loss/substitution run={rid} case={case.case_id}: "
                    f"got {sorted(persisted_slots)} want {sorted(expected_slots)}")
                continue
            op_by_slot = {op.slot_id: op for op in case.intent.operands}
            for o in operands:
                op = op_by_slot[o["slot_id"]]
                # (2) no substitution: pin preserved verbatim
                if dict(o["pin"]) != _expected_pin(op):
                    preservation_ok = False
                    failures.append(f"pin substituted run={rid} case={case.case_id} slot={o['slot_id']}")
                # (5) correct per-path aggregation/temporal: path_strategy preserved verbatim
                if dict(o["path_strategy"]) != _expected_strategy(op):
                    agg_temporal_ok = False
                    failures.append(
                        f"strategy mismatch run={rid} case={case.case_id} slot={o['slot_id']}: "
                        f"got {dict(o['path_strategy'])} want {_expected_strategy(op)}")
                # (3) governed endpoints present; (4) landing endpoint IS the one physical grain
                endpoints = list(o["governed_endpoints"])
                if not endpoints:
                    endpoints_ok = False
                    failures.append(
                        f"no governed endpoints run={rid} case={case.case_id} slot={o['slot_id']}")
                    continue
                if any(not ep.get("grain_fact_key") for ep in endpoints):
                    endpoints_ok = False
                    failures.append(
                        f"ungoverned endpoint (no grain_fact_key) run={rid} case={case.case_id} "
                        f"slot={o['slot_id']}")
                landing_ep = endpoints[-1]
                if (landing_ep.get("catalog") != landing["catalog"]
                        or landing_ep.get("table_ref") != landing["table_ref"]
                        or list(landing_ep.get("grain_key_refs") or []) != landing["grain_key_refs"]):
                    one_grain_ok = False
                    failures.append(
                        f"operand lands off the plan grain run={rid} case={case.case_id} "
                        f"slot={o['slot_id']}")

    # (6) deterministic identity across the two runs (per case)
    deterministic_ok = True
    if len(run_ids) >= 2:
        base, *others = run_ids
        for case in cases:
            base_row = results_by_run[base].get(case.case_id)
            if base_row is None:
                continue
            base_cands = read_candidates(telemetry_conn, base, case.case_id)
            base_ident = (base_row["selected_plan_id"], _candidate_identity(base_cands))
            for rid in others:
                other_row = results_by_run[rid].get(case.case_id)
                other_cands = read_candidates(telemetry_conn, rid, case.case_id)
                other_ident = (
                    other_row["selected_plan_id"] if other_row else None,
                    _candidate_identity(other_cands))
                if other_ident != base_ident:
                    deterministic_ok = False
                    failures.append(
                        f"non-deterministic identity case={case.case_id}: "
                        f"{base}={base_ident} vs {rid}={other_ident}")

    # (1) positive coverage: distinct shapes resolved in EVERY run
    shapes_per_run: list[set[str]] = []
    for rid in run_ids:
        rows = results_by_run[rid]
        shapes_per_run.append({
            c.shape for c in cases
            if c.is_positive and c.shape is not None
            and rows.get(c.case_id, {}).get("semantic_outcome") == SemanticOutcome.resolved.value})
    covered = set.intersection(*shapes_per_run) if shapes_per_run else set()
    positive_coverage_ok = len(covered) >= MULTISOURCE_GOLD_MIN_SHAPES
    if not positive_coverage_ok:
        failures.append(
            f"positive coverage {len(covered)} < {MULTISOURCE_GOLD_MIN_SHAPES} "
            f"(shapes={sorted(covered)})")

    passed = (positive_coverage_ok and outcomes_match and preservation_ok and endpoints_ok
              and one_grain_ok and agg_temporal_ok and deterministic_ok and reconciliation_complete
              and no_technical)
    return AssemblyGateResultV1(
        passed=passed, run_ids=run_ids, positive_shapes_covered=tuple(sorted(covered)),
        positive_coverage_ok=positive_coverage_ok, outcomes_match_expected=outcomes_match,
        operand_preservation_ok=preservation_ok, governed_endpoints_ok=endpoints_ok,
        one_grain_landing_ok=one_grain_ok, aggregation_temporal_ok=agg_temporal_ok,
        deterministic_identity_ok=deterministic_ok, reconciliation_complete=reconciliation_complete,
        no_technical_or_truncation=no_technical,
        resolution_rate=(resolved_positive / total) if total else 0.0,
        failures=tuple(failures))


def _candidate_identity(candidates: Sequence[dict[str, Any]]) -> tuple[Any, ...]:
    """The freshness-free identity tuple of a case's selected candidate (rank 0) — plan id + the
    contract/read/replay hashes — the quantities that MUST be byte-identical across the two runs of the
    same deterministic gold."""
    if not candidates:
        return ()
    c = candidates[0]
    return (c["plan_id"], c["contract_input_hash"], c["contract_output_hash"],
            c["read_set_hash"], c["replay_envelope_hash"])


# ── the runner (seed → double run → evaluate) ─────────────────────────────────────────────────────
def evaluate_assembly_gate(
        planning_conn, telemetry_conn, adapter: CatalogAdapter, *,
        service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        roles: Iterable[str] = _DEFAULT_ROLES, now: datetime = GOLD_NOW,
        run_ids: Sequence[str] = _DEFAULT_RUN_IDS,
        cases: Sequence[GoldCaseV1] = CORRECTNESS_GOLD,
        seed_fn: Callable[..., None] = seed_gold,
        monotonic: Callable[[], float] = time.monotonic) -> AssemblyGateResultV1:
    """Seed the deterministic gold, drive the two-connection shadow harness over the CORRECTNESS gold
    TWICE (distinct ``run_id``s), and evaluate the spec §10 gate over the persisted clean population.

    The fixture is re-seeded before EACH run because the harness rolls ``planning_conn`` back after
    planning (the two-connection contract); the deterministic fact_keys make the two seedings — and so
    the two runs — fingerprint-identical, which is exactly what criterion (6) verifies. ``telemetry_conn``
    is left UNCOMMITTED (the reads happen on the same connection/transaction); the caller owns durability.

    ``seed_fn`` is injectable so a test can pass a NO-OP seeder to prove the gate is not vacuous (a
    reject-all assembler fails positive coverage)."""
    run_ids = tuple(run_ids)
    intents = {c.case_id: c.intent for c in cases}
    for run_id in run_ids:
        seed_fn(planning_conn, service_actor=service_actor, human_actor=human_actor, now=now)
        run_multisource_assembly_shadow(
            planning_conn=planning_conn, telemetry_conn=telemetry_conn, adapter=adapter,
            intents=intents, run_id=run_id, roles=roles, now=now, monotonic=monotonic)
        # the harness rolled planning_conn back; re-seed for the next run (deterministic fact_keys)
    return evaluate_gate_over_runs(telemetry_conn, run_ids=run_ids, cases=cases)

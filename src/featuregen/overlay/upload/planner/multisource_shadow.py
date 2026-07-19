"""Phase 3C.2b-i-A · Task 11 — the TWO-CONNECTION multi-source assembly shadow HARNESS + CLI entry.

Mirrors the single-source ``shadow.py::run_shadow_planner`` (manifest-first, per-item savepoint, ONE
run-owned mutable ``CompileBudget``, the flag read in the CALLER not the harness) but adds the
two-connection discipline that makes the multi-source shadow durable (design §7, finding #13):

  * ``planning_conn`` sees the gold FIXTURE transaction (VERIFIED grain facts, VERIFIED bridges,
    drift watermarks seeded through the real governance write paths) and is ROLLED BACK after
    planning — so a shadow run never commits synthetic gold.
  * ``telemetry_conn`` is a SEPARATE, durable session onto which the manifest + intent results are
    written; the planning-connection rollback cannot touch it.

A single connection CANNOT do both: the rollback that discards the rollback-only gold would also
discard the telemetry it wrote. That impossibility is the whole reason two connections are required.

Sequence per run (design §7):
  (1) ``write_manifest`` on ``telemetry_conn`` FIRST (the expected intent-id set — the durable
      capture-integrity anchor, written before any planning so a pre-loop failure is visible);
  (2) resolve the scope + build A's ONE ``CompilerContext`` (``build_operand_context``, M15: the
      caller's ``roles`` must cover every operand/anchor/key column) + a run-owned ``CompileBudget``
      on ``planning_conn``; plan each intent inside a per-intent SAVEPOINT (a DB error is isolated to
      that intent -> ``technical_failure``, never poisoning the manifest or the other intents);
  (3) RETAIN every result in memory (frozen dataclasses; conn-free);
  (4) roll back the fixture transaction on ``planning_conn``;
  (5) ``write_intent_result`` per intent on ``telemetry_conn`` (mapping honours the TWO-AXIS rule —
      a ``resolved`` assembly whose ``contract_result_status`` is stale/safety-gapped is
      compile-INCOMPLETE, never a clean resolve);
  (6) ``reconcile`` the manifest against the results.

Read-only over every reused engine surface (``plan_multi_source``, ``build_operand_context``,
``resolve_catalog_scope``, the Task-10 store) — nothing here edits a reused module (§12).
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from featuregen.config import get_settings
from featuregen.overlay.catalog import CatalogAdapter, current_catalog_adapter
from featuregen.overlay.upload.planner.contracts import (
    MULTISOURCE_ASSEMBLY_SHADOW_FLAG,
    MULTISOURCE_ASSEMBLY_VERSION,
    OPERATION_POLICY_VERSION,
    ContractResolutionStatus,
    PhysicalReadSetV1,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.planner.multisource_compile import crossing_audit_by_slot
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
    GovernedSourceBindingV1,
    MultiSourceBoundingMetricsV1,
    MultiSourceDeclarationEvidenceV1,
    MultiSourcePlannerIntentV1,
    MultiSourcePlanningResultV1,
    MultiSourceReason,
    MultiSourceReplayEnvelopeV1,
    OperandSlotV1,
    PathStrategyV1,
    PhysicalLandingV1,
)
from featuregen.overlay.upload.planner.multisource_plan import plan_multi_source
from featuregen.overlay.upload.planner.multisource_reuse import build_operand_context
from featuregen.overlay.upload.planner.multisource_shadow_store import (
    CandidateRowV1,
    CaptureStatus,
    CompileCompleteness,
    IntentResultRowV1,
    ManifestRecordV1,
    OperandObservationRowV1,
    SemanticOutcome,
    TechnicalStatus,
    payload_hash,
    reconcile,
    write_intent_result,
    write_manifest,
)
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

logger = logging.getLogger(__name__)

# The per-RUN compile allowance (design §7/§11 — mirrors shadow.py's MAX_COMPILES_PER_RUN + budget):
# a plan-count bound and a REAL elapsed-time deadline over an injected monotonic clock. A run past
# either bound records ``budget_truncated`` for the un-planned intents rather than compiling further.
# NEVER a verdict input — a truncated (incomplete) run is EXCLUDED from deterministic identity
# comparisons (the telemetry marks it so).
MAX_MULTISOURCE_COMPILES_PER_RUN = 256
MULTISOURCE_COMPILE_BUDGET = timedelta(seconds=30)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The rule/registry versions stamped on every run's dispatch manifest (the cohort the gate windows
# over). Mirrors the store test's shape; sourced from the real version constants.
_MANIFEST_VERSIONS: dict[str, str] = {
    "multisource_assembly": MULTISOURCE_ASSEMBLY_VERSION,
    "operation_policy": OPERATION_POLICY_VERSION,
}

# ── the two-axis vocab maps (planner MultiSourceReason -> store telemetry enums) ──
# The ASSEMBLY axis: a semantic disposition maps straight across; a TECHNICAL / capture disposition
# was reached AFTER (or instead of) the semantic gate, so the semantic axis reads ``not_evaluated``
# and the real disposition lives on the technical axis.
_ASSEMBLY_SEMANTIC: dict[MultiSourceReason, SemanticOutcome] = {
    MultiSourceReason.resolved: SemanticOutcome.resolved,
    MultiSourceReason.operand_shape_invalid: SemanticOutcome.operand_shape_invalid,
    MultiSourceReason.unsupported_path_aggregation: SemanticOutcome.unsupported_path_aggregation,
    MultiSourceReason.ordering_anchor_missing: SemanticOutcome.ordering_anchor_missing,
    MultiSourceReason.no_governed_path: SemanticOutcome.no_governed_path,
    MultiSourceReason.realization_endpoint_ungoverned:
        SemanticOutcome.realization_endpoint_ungoverned,
    MultiSourceReason.no_common_physical_grain: SemanticOutcome.no_common_physical_grain,
    MultiSourceReason.ambiguous_physical_grain: SemanticOutcome.ambiguous_physical_grain,
    MultiSourceReason.aggregation_unsafe_on_path: SemanticOutcome.aggregation_unsafe_on_path,
    MultiSourceReason.temporal_paths_incompatible: SemanticOutcome.temporal_paths_incompatible,
    MultiSourceReason.source_binding_ungoverned: SemanticOutcome.source_binding_ungoverned,
}
_TECHNICAL: dict[MultiSourceReason, TechnicalStatus] = {
    MultiSourceReason.operand_or_slot_not_preserved: TechnicalStatus.operand_or_slot_not_preserved,
    MultiSourceReason.technical_failure: TechnicalStatus.technical_failure,
    MultiSourceReason.budget_truncated: TechnicalStatus.budget_truncated,
}


# ── serialization helpers (identities / enums / provenance ONLY — JSON-safe, no free-form text) ──
def _strategy_dict(s: PathStrategyV1) -> dict[str, Any]:
    return {"aggregation": s.aggregation.value, "output_type": s.output_type,
            "output_additivity": s.output_additivity.value,
            "external_type_required": s.external_type_required,
            "ordering_anchor_concept": s.ordering_anchor_concept}


def _source_binding_dict(sb: GovernedSourceBindingV1) -> dict[str, Any]:
    return {"source_grain_entity": sb.source_grain_entity,
            "source_grain_key_refs": list(sb.source_grain_key_refs),
            "grain_fact_key": sb.grain_fact_key}


def _landing_dict(landing: PhysicalLandingV1) -> dict[str, Any]:
    return {"catalog": landing.catalog, "table_ref": landing.table_ref,
            "grain_key_refs": list(landing.grain_key_refs)}


def _endpoint_dict(e: GovernedEndpointV1) -> dict[str, Any]:
    return {"catalog": e.catalog, "table_ref": e.table_ref,
            "grain_key_refs": list(e.grain_key_refs), "grain_fact_key": e.grain_fact_key}


def _evidence_dict(ev: MultiSourceDeclarationEvidenceV1) -> dict[str, Any]:
    return {"final_verdict": ev.final_verdict.value,
            "final_reason_codes": [r.value for r in ev.final_reason_codes],
            "per_path": [p.slot_id for p in ev.per_path]}


def _pin_dict(slot: OperandSlotV1) -> dict[str, Any]:
    return {"catalog_source": slot.catalog_source, "object_ref": slot.object_ref,
            "concept": slot.authoritative_concept}


def _read_set_hash(read_set: PhysicalReadSetV1) -> str:
    material = ";".join(sorted(f"{c.catalog_source}|{c.object_ref}" for c in read_set.columns))
    return "rsh_" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _normalized_intent_hash(intent: MultiSourcePlannerIntentV1) -> str:
    """A deterministic, plan-independent hash of the INTENT (normalized, operand order invariant) —
    the store's ``normalized_intent_hash`` used to detect a divergent re-run of the same key."""
    fe = intent.final_expression
    payload = {
        "target_entity": intent.target_entity,
        "operation_policy_version": intent.operation_policy_version,
        "final_expression": {
            "operation": fe.operation.value, "ordered_slot_ids": list(fe.ordered_slot_ids),
            "time_slot_id": fe.time_slot_id, "window": fe.window,
            "output_additivity": fe.output_additivity.value},
        "operands": sorted(
            ({"slot_id": op.slot_id, "semantic_role": op.semantic_role.value,
              "catalog_source": op.catalog_source, "object_ref": op.object_ref,
              "authoritative_concept": op.authoritative_concept,
              "path_strategy": _strategy_dict(op.path_strategy),
              "source_binding": _source_binding_dict(op.source_binding)}
             for op in intent.operands),
            key=lambda d: str(d["slot_id"])),
    }
    return "nih_" + payload_hash(payload)


# ── result assembly (synthetic technical / truncation results; deterministic replay envelope) ──
def _zero_bounds() -> MultiSourceBoundingMetricsV1:
    return MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=False, operand_combinations_truncated=False,
        states_truncated=False, landing_ambiguous=False, total_states_expanded=0)


def _unplanned_envelope(intent: MultiSourcePlannerIntentV1) -> MultiSourceReplayEnvelopeV1:
    """The intent-only replay fingerprint for a result that never assembled a plan (technical failure
    / budget truncation) — mirrors ``multisource_plan._build_replay_envelope(intent, plan=None)``:
    deterministic over target_entity + operand pins + source grain key refs + versions; no per-event
    ids, no endpoint/bridge fact_keys (none were revalidated)."""
    operand_pins = tuple(sorted(
        f"{op.catalog_source}|{op.object_ref}|{op.authoritative_concept}" for op in intent.operands))
    source_grain_key_refs = tuple(sorted({
        ref for op in intent.operands for ref in op.source_binding.source_grain_key_refs}))
    material = "|".join((
        intent.target_entity, ";".join(operand_pins), ";".join(source_grain_key_refs),
        ";".join(()), ";".join(()),
        MULTISOURCE_ASSEMBLY_VERSION, OPERATION_POLICY_VERSION, intent.operation_policy_version))
    input_hash = "msr_" + hashlib.sha256(material.encode()).hexdigest()[:24]
    return MultiSourceReplayEnvelopeV1(
        target_entity=intent.target_entity, operand_pins=operand_pins,
        source_grain_key_refs=source_grain_key_refs, governed_endpoint_fact_keys=(),
        bridge_fact_keys=(), input_hash=input_hash)


def _synthetic_result(intent: MultiSourcePlannerIntentV1, run_id: str | None,
                      reason: MultiSourceReason) -> MultiSourcePlanningResultV1:
    """A harness-classified result with NO candidates: a per-intent DB error (``technical_failure``,
    isolated by the savepoint) or a spent run budget (``budget_truncated``)."""
    return MultiSourcePlanningResultV1(
        run_id=run_id, target_entity=intent.target_entity, candidate_plans=(),
        selected_plan_id=None, result_status=reason, primary_reason_code=reason,
        reason_codes=(reason,), bounding=_zero_bounds(),
        replay_envelope=_unplanned_envelope(intent),
        contract_result_status=ContractResolutionStatus.not_compiled,
        selected_contract_plan_id=None, selected_contract_id=None)


# ── the two-axis mapping (design §7; never collapse the axes) ──
def _semantic_outcome(reason: MultiSourceReason) -> SemanticOutcome:
    return _ASSEMBLY_SEMANTIC.get(reason, SemanticOutcome.not_evaluated)


def _technical_status(reason: MultiSourceReason) -> TechnicalStatus:
    return _TECHNICAL.get(reason, TechnicalStatus.ok)


def _compile_completeness(result: MultiSourcePlanningResultV1) -> CompileCompleteness:
    """The CONTRACT axis. ``complete`` ONLY when the contract resolved; a resolved-ASSEMBLY plan
    whose ``contract_result_status`` is stale / safety-gapped / declaration-unresolved is
    compile-INCOMPLETE (NOT a clean resolve); nothing compiled -> ``not_applicable``."""
    crs = result.contract_result_status
    if crs is ContractResolutionStatus.resolved:
        return CompileCompleteness.complete
    if crs is ContractResolutionStatus.not_compiled:
        return CompileCompleteness.not_applicable
    return CompileCompleteness.incomplete


def _map_result(intent_id: str, intent: MultiSourcePlannerIntentV1,
                result: MultiSourcePlanningResultV1, *,
                crossings: Mapping[str, Mapping[str, Any]], now: datetime
                ) -> tuple[IntentResultRowV1, list[CandidateRowV1], list[OperandObservationRowV1]]:
    """Map one intent's ``MultiSourcePlanningResultV1`` to the Task-10 store rows (parent + candidate
    + operand), honouring the two-axis rule. ``capture_status`` is a placeholder — the store's
    two-phase write determines the stored value. ``crossings`` is the pre-collected audit map for this
    intent — ``{plan_id: {slot_id: crossing records}}`` — gathered on the fixture connection BEFORE the
    rollback (I-1, finding #13), since ``confirmed_event_id`` must be re-queried while the bridges live."""
    slots_by_id = {op.slot_id: op for op in intent.operands}
    intent_row = IntentResultRowV1(
        run_id=result.run_id, intent_id=intent_id,
        semantic_outcome=_semantic_outcome(result.result_status),
        compile_completeness=_compile_completeness(result),
        technical_status=_technical_status(result.result_status),
        capture_status=CaptureStatus.persisted,
        normalized_intent_hash=_normalized_intent_hash(intent),
        selected_plan_id=result.selected_plan_id,
        reason_codes=tuple(rc.value for rc in result.reason_codes),
        created_at=now)

    candidate_rows: list[CandidateRowV1] = []
    operand_rows: list[OperandObservationRowV1] = []
    for rank, plan in enumerate(result.candidate_plans):
        candidate_rows.append(CandidateRowV1(
            run_id=result.run_id, intent_id=intent_id, plan_id=plan.plan_id,
            physical_landing=_landing_dict(plan.physical_landing),
            contract_input_hash=plan.contract_input_hash,
            contract_output_hash=plan.contract_output_hash,
            read_set_hash=_read_set_hash(plan.physical_read_set),
            replay_envelope_hash=result.replay_envelope.input_hash,
            rank=rank, declaration_evidence=_evidence_dict(plan.declaration_evidence),
            created_at=now))
        plan_crossings = crossings.get(plan.plan_id, {})
        for path in plan.operand_paths:
            slot = slots_by_id.get(path.slot_id)
            operand_rows.append(OperandObservationRowV1(
                run_id=result.run_id, intent_id=intent_id, plan_id=plan.plan_id,
                slot_id=path.slot_id,
                pin=_pin_dict(slot) if slot is not None else {"object_ref": path.object_ref},
                role=path.semantic_role.value,
                path_strategy=_strategy_dict(path.path_strategy),
                governed_endpoints=[_endpoint_dict(e) for e in path.governed_endpoints],
                source_binding=_source_binding_dict(slot.source_binding) if slot is not None else {},
                crossings=list(plan_crossings.get(path.slot_id, ())),
                created_at=now))
    return intent_row, candidate_rows, operand_rows


# ── the harness (the flag is NOT read here — the CLI entrypoint reads it) ──
def run_multisource_assembly_shadow(
        *, planning_conn, telemetry_conn, adapter: CatalogAdapter,
        intents: Mapping[str, MultiSourcePlannerIntentV1], run_id: str | None,
        roles: Iterable[str], now: datetime,
        monotonic: Callable[[], float] = time.monotonic,
) -> tuple[MultiSourcePlanningResultV1, ...]:
    """Drive ``plan_multi_source`` over the gold ``intents`` with two-connection capture integrity
    (design §7). ``planning_conn`` sees the gold fixture transaction (and is ROLLED BACK here after
    planning); ``telemetry_conn`` durably retains the manifest + results. Returns the per-intent
    results (in sorted-id order), each with ``run_id`` stamped."""
    roles = tuple(roles)
    intent_ids = sorted(intents)

    # (1) manifest FIRST on the DURABLE telemetry connection.
    write_manifest(telemetry_conn, ManifestRecordV1(
        run_id=run_id, expected_intent_ids=tuple(intent_ids), versions=dict(_MANIFEST_VERSIONS),
        shadow_flag=True, producer_commit=get_settings().producer_commit, created_at=now))

    results: dict[str, MultiSourcePlanningResultV1] = {}

    # (2a) resolve the scope + build A's ONE per-run CompilerContext on planning_conn (which sees the
    # gold fixtures). M15: `roles` must cover every operand/anchor/key column, else a structural key's
    # safety resolves not_evaluated and the compiler records unresolved_safety_evaluation. A pre-loop
    # failure is caught: every intent is classified technical_failure so reconciliation stays complete.
    try:
        scope = resolve_catalog_scope(planning_conn, roles=roles, target_entity=None, now=now)
        ctx = build_operand_context(
            planning_conn, catalogs=scope.authorized_catalog_sources, roles=roles, now=now,
            agg_declarations={})
    except Exception:
        logger.exception("multisource shadow pre-loop failure (scope/context) run=%s", run_id)
        for iid in intent_ids:
            results[iid] = _synthetic_result(intents[iid], run_id, MultiSourceReason.technical_failure)
        planning_conn.rollback()
        # No plan assembled (all technical) -> no operand paths -> no crossings to collect.
        _persist_and_reconcile(telemetry_conn, run_id, intents, results, {}, now=now)
        return tuple(results[iid] for iid in intent_ids)

    # (2b) plan each intent inside a per-intent savepoint; ONE mutable run-owned budget across intents.
    budget = CompileBudget(
        remaining=MAX_MULTISOURCE_COMPILES_PER_RUN,
        deadline_monotonic=monotonic() + MULTISOURCE_COMPILE_BUDGET.total_seconds(),
        clock=monotonic)
    for iid in intent_ids:
        intent = intents[iid]
        # Run-budget guard (§11): a spent budget truncates the REMAINING intents (never a hot loop /
        # silent drop). stopped_by_time records WHICH bound fired first (time vs count).
        time_hit = budget.clock() >= budget.deadline_monotonic
        if budget.remaining <= 0 or time_hit:
            if budget.stopped_by_time is None:
                budget.stopped_by_time = time_hit
            results[iid] = _synthetic_result(intent, run_id, MultiSourceReason.budget_truncated)
            continue
        try:
            with planning_conn.transaction():   # per-intent savepoint — isolates a DB error
                result = plan_multi_source(
                    planning_conn, adapter, intent=intent, scope=scope, roles=roles, now=now,
                    ctx=ctx, budget=budget)
            results[iid] = replace(result, run_id=run_id)
        except Exception:   # a raised DB error is CLASSIFIED technical here (never in plan_multi_source)
            logger.exception("multisource shadow planner error intent=%s run=%s", iid, run_id)
            results[iid] = _synthetic_result(intent, run_id, MultiSourceReason.technical_failure)

    # (3) results are retained in `results` (frozen, conn-free). (3b) collect the per-crossing AUDIT
    # records (I-1) on planning_conn while the bridges are STILL visible — confirmed_event_id must be
    # re-queried BEFORE the rollback discards the fixture transaction (finding #13).
    crossings = _collect_crossings(planning_conn, ctx, results, run_id)
    # (4) roll back the fixture transaction.
    planning_conn.rollback()

    # (5) persist each on the durable telemetry connection + (6) reconcile.
    _persist_and_reconcile(telemetry_conn, run_id, intents, results, crossings, now=now)
    return tuple(results[iid] for iid in intent_ids)


def _collect_crossings(
        planning_conn, ctx, results: Mapping[str, MultiSourcePlanningResultV1], run_id: str | None,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    """Gather the per-operand governed-crossing AUDIT records for every candidate plan (I-1), keyed
    ``{intent_id: {plan_id: {slot_id: crossing records}}}``. Runs on ``planning_conn`` while the fixture
    bridges are still live (BEFORE the rollback) so ``confirmed_event_id`` is re-queryable. Each plan's
    re-query is isolated in its OWN savepoint: a failure degrades that plan's telemetry to empty
    crossings (logged) but never poisons the outer transaction or the plan's other rows."""
    crossings: dict[str, dict[str, Mapping[str, Any]]] = {}
    for iid, result in results.items():
        by_plan: dict[str, Mapping[str, Any]] = {}
        for plan in result.candidate_plans:
            try:
                with planning_conn.transaction():
                    by_plan[plan.plan_id] = crossing_audit_by_slot(planning_conn, ctx, plan)
            except Exception:
                logger.exception("multisource shadow crossings audit failed intent=%s plan=%s run=%s",
                                 iid, plan.plan_id, run_id)
                by_plan[plan.plan_id] = {}
        crossings[iid] = by_plan
    return crossings


def _persist_and_reconcile(telemetry_conn, run_id: str | None,
                           intents: Mapping[str, MultiSourcePlannerIntentV1],
                           results: Mapping[str, MultiSourcePlanningResultV1],
                           crossings: Mapping[str, Mapping[str, Mapping[str, Any]]], *,
                           now: datetime) -> None:
    """Map + write every retained result on the DURABLE connection, then reconcile. A per-intent
    store-write failure is caught (never re-propagated) so the manifest is retained and the loss
    surfaces via reconciliation — never a circular self-report. ``crossings`` is the pre-collected
    per-intent audit map (I-1); an intent with no candidate plan contributes none."""
    for iid in sorted(results):
        intent_row, candidate_rows, operand_rows = _map_result(
            iid, intents[iid], results[iid], crossings=crossings.get(iid, {}), now=now)
        try:
            write_intent_result(telemetry_conn, intent_row, candidate_rows, operand_rows)
        except Exception:
            logger.exception("multisource shadow store write failed intent=%s run=%s", iid, run_id)
    rec = reconcile(telemetry_conn, run_id) if run_id is not None else None
    if rec is not None and not rec.complete:
        logger.warning("multisource shadow capture incomplete run=%s missing=%s",
                       run_id, rec.missing_intent_ids)


# ── CLI/admin entrypoint (THE FLAG IS READ HERE, never in the harness — mirror shadow.py) ──
def _flag_on(env: Mapping[str, str] | None) -> bool:
    import os
    source = env if env is not None else os.environ
    return source.get(MULTISOURCE_ASSEMBLY_SHADOW_FLAG, "").strip().lower() in _TRUTHY


def _default_connect():
    dsn = get_settings().dsn
    if dsn is None:
        raise RuntimeError("FEATUREGEN_DSN is not set; cannot open a multi-source shadow connection")
    return psycopg.connect(dsn)


def run_shadow_cli(
        *, intents_provider: Callable[[Any], Mapping[str, MultiSourcePlannerIntentV1]],
        run_id: str, roles: Iterable[str], now: datetime | None = None,
        connect: Callable[[], Any] | None = None, env: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
) -> tuple[MultiSourcePlanningResultV1, ...] | None:
    """The runnable admin/CLI entrypoint.

    THE FLAG IS READ HERE (``FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW``), never inside the harness —
    mirroring ``shadow.py`` where the route, not the pure planner, owns the flag. Flag off -> a NO-OP
    (opens NO connection, returns ``None``).

    THE TWO-CONNECTION CONTRACT (finding #13): a single connection cannot both (a) see the gold
    fixture transaction that is rolled back after planning AND (b) durably retain the telemetry — the
    rollback that discards the gold would discard the telemetry too. So the entrypoint opens TWO
    connections: ``planning_conn`` (onto which ``intents_provider`` seeds the gold fixtures, and which
    the harness rolls back) and ``telemetry_conn`` (durable — COMMITTED here after the run). It
    constructs the sealed-config upload catalog adapter + table refs the governed-endpoint
    revalidation (``resolve_fact``) needs. ``intents_provider`` receives ``planning_conn`` so it can
    seed authored gold in the same fixture transaction and return the ``{intent_id: intent}`` map."""
    if not _flag_on(env):
        logger.info("multisource assembly shadow flag off (%s) — no-op", MULTISOURCE_ASSEMBLY_SHADOW_FLAG)
        return None

    open_conn = connect if connect is not None else _default_connect
    ensure_upload_catalog_adapter()               # sealed-config upload adapter + table refs
    adapter = current_catalog_adapter()
    planning_conn = open_conn()
    telemetry_conn = open_conn()
    try:
        intents = intents_provider(planning_conn)
        results = run_multisource_assembly_shadow(
            planning_conn=planning_conn, telemetry_conn=telemetry_conn, adapter=adapter,
            intents=intents, run_id=run_id, roles=roles,
            now=now if now is not None else datetime.now(UTC), monotonic=monotonic)
        telemetry_conn.commit()                   # durable — the whole point of the second connection
        return results
    finally:
        planning_conn.close()
        telemetry_conn.close()

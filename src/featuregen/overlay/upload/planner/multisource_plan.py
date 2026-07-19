"""Phase 3C.2b-i-A · Task 9 — ``plan_multi_source`` orchestration (spec §5 assembly order, §3.3 result).

The one entry point that ties Tasks 3-8 into a ``MultiSourcePlanningResultV1``. It ORCHESTRATES —
it never reimplements a reused stage:

  1. ``validate_operation_shape`` (Task 3, §4) — a rejected shape returns a result with that
     ``result_status`` and NO candidates.
  2. ``enumerate_operand_paths`` per operand (Task 5, §5 steps 2-3) — a non-resolved operand
     (``no_governed_path`` / ``realization_endpoint_ungoverned`` / ``budget_truncated``) short-circuits
     with that status.
  3. ``converge`` across operands (Task 6, §5 step 4) — ``no_common_physical_grain`` /
     ``ambiguous_physical_grain`` short-circuit.
  4. Per-path checks (Task 7, §5 step 5) — ``check_operand_path`` per path, ``check_time_slot_take_latest``,
     ``check_paths_temporal_consistency`` across paths — over A's OWN context with the per-operand
     aggregation declarations injected (production ``build_compiler_context`` hard-codes ``{}``).
  5. Final join + the PRESERVATION assertion (§5 steps 6-7): every operand + ``semantic_role`` slot
     survives exactly once and the final expression matches the intent, else
     ``operand_or_slot_not_preserved`` (technical).
  6. ``compile_multi_source_contract`` (Task 8, §5 step 8, §6) — the two-axis compile.
  7. Select the best candidate + set ``result_status``. THE TWO-AXIS RESOLVE GATE (Task-8 review): the
     compiler keeps ``resolution_status = resolved`` even on a safety-eval gap or a stale union — the
     failure surfaces ONLY on ``contract_result_status``. So a plan counts as GENUINELY resolved (and is
     selected as a resolved CONTRACT) ONLY when ``contract_result_status == resolved`` AND the assembly
     axis is resolved. Keying resolve on ``resolution_status`` alone would be a fail-open.
  8. Assemble ``MultiSourcePlanningResultV1``: ``candidate_plans``, ``selected_plan_id``, ``bounding``,
     and a ``MultiSourceReplayEnvelopeV1`` keyed on FACT_KEYS (governed endpoint ``grain_fact_key``s +
     bridge ``fact_key``s + versions; NO run_id / timestamp / per-event id).

Fail-closed at every step. A raised DB error is NEVER swallowed here — it PROPAGATES (the shadow
harness classifies it technical). Read-only over the reused engine surfaces; nothing here edits a
reused module (the §12 behaviour-neutrality invariant).
"""
from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.planner.contracts import (
    MULTISOURCE_ASSEMBLY_VERSION,
    OPERATION_POLICY_VERSION,
    CatalogScopeV1,
    ContractResolutionStatus,
    DeclarationStatus,
    PhysicalColumnReadV1,
    PhysicalReadSetV1,
)
from featuregen.overlay.upload.planner.declarations import (
    CompileBudget,
    CompilerContext,
    build_physical_read_set,
)
from featuregen.overlay.upload.planner.multisource_assembly import (
    _OPERAND_NEED_ROLE,
    LandedCombinationV1,
    OperandEnumerationResultV1,
    ResolvedOperandPathV1,
    _operand_recipe_id,
    check_operand_path,
    check_paths_temporal_consistency,
    check_time_slot_take_latest,
    converge,
    enumerate_operand_paths,
)
from featuregen.overlay.upload.planner.multisource_compile import (
    MultiSourceContractSpecV1,
    compile_multi_source_contract,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    PATH_AGG_TO_FUNCTION,
    MultiSourceBindingPlanV1,
    MultiSourceBoundingMetricsV1,
    MultiSourceDeclarationEvidenceV1,
    MultiSourcePlannerIntentV1,
    MultiSourcePlanningResultV1,
    MultiSourceReason,
    MultiSourceReplayEnvelopeV1,
    OperandPathV1,
    OperandSlotV1,
    PhysicalLandingV1,
)
from featuregen.overlay.upload.planner.multisource_operation import validate_operation_shape
from featuregen.overlay.upload.planner.multisource_reuse import build_operand_context

# A generous default per-run compile allowance: A compiles exactly ONE assembled plan per intent, so
# any positive count suffices; the harness owns a tighter budget when it drives a whole gold set.
_DEFAULT_COMPILE_BUDGET_PLANS = 64


# ── bounds helpers ───────────────────────────────────────────────────────────────────────────────
def _zero_bounds() -> MultiSourceBoundingMetricsV1:
    """A zeroed bounds record for a pre-enumeration reject (shape-invalid) — no path work happened."""
    return MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=False, operand_combinations_truncated=False,
        states_truncated=False, landing_ambiguous=False, total_states_expanded=0)


def _merge_enumeration_bounds(
        results: list[OperandEnumerationResultV1]) -> MultiSourceBoundingMetricsV1:
    """Fold every operand's enumeration bounds into the single record convergence extends: OR the
    per-operand truncation flag, SUM the states expanded. Combination/landing bounds are convergence's
    to set (they stay False here)."""
    return MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=any(r.bounds.paths_per_operand_truncated for r in results),
        operand_combinations_truncated=False,
        states_truncated=any(r.bounds.states_truncated for r in results),
        landing_ambiguous=False,
        total_states_expanded=sum(r.bounds.total_states_expanded for r in results))


# ── replay envelope (keyed on FACT_KEYS — deterministic; no run_id/timestamp/event-id) ────────────
def _build_replay_envelope(intent: MultiSourcePlannerIntentV1, *,
                           plan: MultiSourceBindingPlanV1 | None) -> MultiSourceReplayEnvelopeV1:
    """The input fingerprint (spec §3.3, findings #8/#11): target_entity + operand pins + source grain
    key refs + governed endpoint ``grain_fact_key``s + crossed bridge ``fact_key``s + versions — every
    component DETERMINISTIC. No ``run_id``, no timestamp, no per-event id, so a double run over the same
    seeded (stable) fact_keys fingerprints identically. Before a governed plan is assembled the endpoint
    /bridge fact_key sets are empty (no endpoint was revalidated); the hash stays deterministic from the
    intent alone."""
    operand_pins = tuple(sorted(
        f"{op.catalog_source}|{op.object_ref}|{op.authoritative_concept}" for op in intent.operands))
    source_grain_key_refs = tuple(sorted({
        ref for op in intent.operands for ref in op.source_binding.source_grain_key_refs}))
    if plan is not None:
        endpoint_fact_keys = tuple(sorted({
            ep.grain_fact_key for p in plan.operand_paths for ep in p.governed_endpoints}))
        bridge_fact_keys = tuple(sorted({
            seg.bridge_fact_key for p in plan.operand_paths
            for seg in p.binding_plan.path_segments if seg.bridge_fact_key is not None}))
    else:
        endpoint_fact_keys = ()
        bridge_fact_keys = ()
    material = "|".join((
        intent.target_entity,
        ";".join(operand_pins),
        ";".join(source_grain_key_refs),
        ";".join(endpoint_fact_keys),
        ";".join(bridge_fact_keys),
        MULTISOURCE_ASSEMBLY_VERSION, OPERATION_POLICY_VERSION, intent.operation_policy_version))
    input_hash = "msr_" + hashlib.sha256(material.encode()).hexdigest()[:24]
    return MultiSourceReplayEnvelopeV1(
        target_entity=intent.target_entity, operand_pins=operand_pins,
        source_grain_key_refs=source_grain_key_refs, governed_endpoint_fact_keys=endpoint_fact_keys,
        bridge_fact_keys=bridge_fact_keys, input_hash=input_hash)


# ── assembly helpers ───────────────────────────────────────────────────────────────────────────
def _inject_operand_declarations(ctx: CompilerContext,
                                 operands: tuple[OperandSlotV1, ...]) -> CompilerContext:
    """A's OWN context for the per-path checks: the passed context with the per-operand aggregation
    functions injected, keyed EXACTLY as Task-5 enumeration keyed each operand's ``recipe_id``/
    ``need_role`` (``PATH_AGG_TO_FUNCTION``-mapped). Production ``build_compiler_context`` hard-codes an
    EMPTY registry, so without this a declared ``sum``/``take_latest`` would resolve ``undeclared`` and
    ``check_operand_path`` would (wrongly) reject the path. ``avg``/``stddev`` map to ``None`` and stay
    undeclared (resolved from additivity / failed closed downstream — the SAME registry Task 8 injects,
    so the pre-check verdict matches the compile verdict)."""
    injected = dict(ctx.agg_declarations)
    for op in operands:
        fn = PATH_AGG_TO_FUNCTION[op.path_strategy.aggregation]
        if fn is not None:
            injected[(_operand_recipe_id(op), _OPERAND_NEED_ROLE)] = fn
    return replace(ctx, agg_declarations=injected)


def _operand_path(operand: OperandSlotV1, candidate) -> OperandPathV1:
    """One operand's ``OperandPathV1`` from its converged candidate: the frontier's own governed
    ``binding_plan`` (its ``path_segments`` ARE the governed crossings) + the FULL tuple of revalidated
    governed endpoints in path order — source + each intermediate + landing (spec §3.2; every hop
    endpoint's ``grain_fact_key`` keys the replay envelope, not just the landing's)."""
    return OperandPathV1(
        slot_id=operand.slot_id, semantic_role=operand.semantic_role,
        catalog_source=operand.catalog_source, object_ref=operand.object_ref,
        binding_plan=candidate.binding_plan, governed_endpoints=candidate.governed_endpoints,
        path_strategy=operand.path_strategy, pit_treatment="")


def _union_read_set(ctx: CompilerContext,
                    operand_paths: tuple[OperandPathV1, ...]) -> PhysicalReadSetV1:
    """The union of every operand path's reused ``PhysicalReadSetV1`` (spec §5 step 6), deduped on the
    full column identity, first-seen order preserved. The compiler re-derives its own read set for the
    universal-safety self-check; this union is the assembled plan's read inventory of record."""
    seen: dict[PhysicalColumnReadV1, None] = {}
    for path in operand_paths:
        for col in build_physical_read_set(ctx, path.binding_plan).columns:
            seen.setdefault(col, None)
    return PhysicalReadSetV1(columns=tuple(seen))


def _mint_plan_id(landing: PhysicalLandingV1, operand_paths: tuple[OperandPathV1, ...]) -> str:
    """A deterministic multi-source plan id over the landing + the per-operand (slot, governed physical
    plan) pairs (sorted, so id is invariant to input order). Stable across runs of the same seeded
    topology, so ``selected_plan_id`` is reproducible."""
    material = "|".join((
        landing.catalog, landing.table_ref, ",".join(landing.grain_key_refs),
        ">".join(sorted(f"{p.slot_id}~{p.binding_plan.physical_plan_id}" for p in operand_paths))))
    return "msp_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _empty_evidence() -> MultiSourceDeclarationEvidenceV1:
    return MultiSourceDeclarationEvidenceV1(per_path=(), final_verdict=DeclarationStatus.not_compiled)


def _preservation_holds(plan: MultiSourceBindingPlanV1,
                        intent: MultiSourcePlannerIntentV1) -> bool:
    """§5-step-7 preservation: every intent operand + its ``semantic_role`` slot survives on the plan
    EXACTLY once, and the final expression references each surviving operand exactly once (no operand
    dropped, added, or double-counted during assembly). The final expression is copied verbatim from the
    intent, so a mismatch here means the assembled operand set diverged from the intent — a technical
    orchestration fault, not a semantic one."""
    if Counter(op.slot_id for op in intent.operands) != Counter(p.slot_id for p in plan.operand_paths):
        return False
    intent_roles = {op.slot_id: op.semantic_role for op in intent.operands}
    for p in plan.operand_paths:
        if intent_roles.get(p.slot_id) is not p.semantic_role:
            return False
    fe = plan.final_expression
    referenced = list(fe.ordered_slot_ids)
    if fe.time_slot_id is not None:
        referenced.append(fe.time_slot_id)
    slot_ids = {p.slot_id for p in plan.operand_paths}
    if not set(referenced) <= slot_ids:
        return False
    counts = Counter(referenced)
    return all(counts[s] == 1 for s in slot_ids)


# ── result assembly ──────────────────────────────────────────────────────────────────────────────
def _reject(intent: MultiSourcePlannerIntentV1, reason: MultiSourceReason, *,
            bounds: MultiSourceBoundingMetricsV1,
            reason_codes: tuple[MultiSourceReason, ...] = (),
            plan: MultiSourceBindingPlanV1 | None = None) -> MultiSourcePlanningResultV1:
    """A fail-closed reject: the run's ``result_status`` carries ``reason``, NO candidate is selected,
    and the contract axis stays ``not_compiled`` (nothing was compiled to a governed contract)."""
    return MultiSourcePlanningResultV1(
        run_id=None, target_entity=intent.target_entity, candidate_plans=(),
        selected_plan_id=None, result_status=reason, primary_reason_code=reason,
        reason_codes=reason_codes or (reason,), bounding=bounds,
        replay_envelope=_build_replay_envelope(intent, plan=plan),
        contract_result_status=ContractResolutionStatus.not_compiled,
        selected_contract_plan_id=None, selected_contract_id=None)


def _default_budget() -> CompileBudget:
    return CompileBudget(remaining=_DEFAULT_COMPILE_BUDGET_PLANS,
                         deadline_monotonic=float("inf"), clock=time.monotonic)


def plan_multi_source(
        conn, adapter: CatalogAdapter, *, intent: MultiSourcePlannerIntentV1, scope: CatalogScopeV1,
        roles: Iterable[str], now: datetime, ctx: CompilerContext | None = None,
        budget: CompileBudget | None = None) -> MultiSourcePlanningResultV1:
    """Plan one governed multi-source intent (spec §5 order, §3.3 result). Fail-closed at every step; a
    raised DB error PROPAGATES (never swallowed into a technical status here — the harness classifies)."""
    roles = tuple(roles)

    # (1) Shape (Task 3, §4). A rejected shape returns with that reason and NO candidates — no DB read.
    shape_reason = validate_operation_shape(intent)
    if shape_reason is not None:
        return _reject(intent, shape_reason, bounds=_zero_bounds())

    # (2) A's own compiler context over every authorized catalog / role (unless the caller supplied one).
    base_ctx = ctx if ctx is not None else build_operand_context(
        conn, catalogs=scope.authorized_catalog_sources, roles=roles, now=now, agg_declarations={})

    # (2b) Enumerate each operand's governed paths (Task 5). A non-resolved operand short-circuits with
    # its own reason (no_governed_path / realization_endpoint_ungoverned / budget_truncated).
    enum_results: list[OperandEnumerationResultV1] = []
    for operand in intent.operands:
        enum = enumerate_operand_paths(
            conn, adapter, base_ctx, operand=operand, target_entity=intent.target_entity,
            scope=scope, roles=roles, now=now)
        enum_results.append(enum)
        if enum.status is not MultiSourceReason.resolved:
            return _reject(intent, enum.status, bounds=enum.bounds, reason_codes=enum.reason_codes)

    # (3) Converge onto ONE physical landing (Task 6). Fail-closed on no-common / ambiguous grain.
    conv = converge(enum_results, bounds=_merge_enumeration_bounds(enum_results))
    if conv.status is not MultiSourceReason.resolved:
        return _reject(intent, conv.status, bounds=conv.bounds, reason_codes=conv.reason_codes)
    combination: LandedCombinationV1 = conv.landed_combinations[0]

    # (4) Per-path checks (Task 7) over A's own declaration-injected context. `operand_candidates` is in
    # INPUT operand order, so it zips positionally back to `intent.operands`.
    check_ctx = _inject_operand_declarations(base_ctx, intent.operands)
    resolved_paths = [
        ResolvedOperandPathV1(operand=operand, candidate=candidate)
        for operand, candidate in zip(intent.operands, combination.operand_candidates)]
    temporals = []
    for resolved in resolved_paths:
        temporal, _hop_aggregations, path_reason = check_operand_path(check_ctx, resolved)
        if path_reason is not None:
            return _reject(intent, path_reason, bounds=conv.bounds)
        anchor_reason = check_time_slot_take_latest(resolved)
        if anchor_reason is not None:
            return _reject(intent, anchor_reason, bounds=conv.bounds)
        temporals.append(temporal)
    temporal_reason = check_paths_temporal_consistency(temporals)
    if temporal_reason is not None:
        return _reject(intent, temporal_reason, bounds=conv.bounds)

    # (5) Final join + PRESERVATION assertion (§5 steps 6-7).
    operand_paths = tuple(
        _operand_path(operand, candidate)
        for operand, candidate in zip(intent.operands, combination.operand_candidates))
    plan = MultiSourceBindingPlanV1(
        plan_id=_mint_plan_id(combination.landing, operand_paths),
        physical_landing=combination.landing, operand_paths=operand_paths,
        final_expression=intent.final_expression,
        physical_read_set=_union_read_set(check_ctx, operand_paths),
        resolution_status=MultiSourceReason.resolved, reason_codes=(),
        contract_result_status=ContractResolutionStatus.not_compiled, contract_id=None,
        declaration_evidence=_empty_evidence(), contract_input_hash="", contract_output_hash="")
    if not _preservation_holds(plan, intent):
        return _reject(intent, MultiSourceReason.operand_or_slot_not_preserved, bounds=conv.bounds,
                       plan=plan)

    # (6) Compile the assembled plan (Task 8, §5 step 8) — the two-axis compile.
    spec = MultiSourceContractSpecV1(
        operands=intent.operands, output_additivity=intent.final_expression.output_additivity,
        window=intent.final_expression.window, requires_temporal_consistency=True,
        operation_policy_version=intent.operation_policy_version)
    compiled = compile_multi_source_contract(
        conn, check_ctx, plan, spec, budget=budget if budget is not None else _default_budget())
    candidate_plans = (compiled,)
    envelope = _build_replay_envelope(intent, plan=compiled)

    # (7) Select + THE TWO-AXIS RESOLVE GATE. The compiler holds `resolution_status = resolved` even on a
    # stale union / safety-eval gap — the failure lives ONLY on `contract_result_status`. So a genuinely
    # resolved CONTRACT selection requires BOTH axes resolved; keying it on `resolution_status` alone
    # would be a fail-open (a stale/ungoverned plan wrongly presented as a governed contract).
    assembly_resolved = compiled.resolution_status is MultiSourceReason.resolved
    contract_resolved = compiled.contract_result_status is ContractResolutionStatus.resolved

    if not assembly_resolved:
        # A declaration-axis semantic/technical failure the compiler surfaced (e.g. an incoherent
        # final `output_additivity`, or a preservation fault the earlier assertion did not model).
        return MultiSourcePlanningResultV1(
            run_id=None, target_entity=intent.target_entity, candidate_plans=candidate_plans,
            selected_plan_id=None, result_status=compiled.resolution_status,
            primary_reason_code=compiled.resolution_status,
            reason_codes=compiled.reason_codes or (compiled.resolution_status,),
            bounding=conv.bounds, replay_envelope=envelope,
            contract_result_status=compiled.contract_result_status,
            selected_contract_plan_id=None, selected_contract_id=None)

    # (8) Assembly axis resolved. `selected_plan_id` names the assembly selection (mirrors the
    # ingredient axis of BindingPlanningResultV1); the CONTRACT selection is gated on BOTH axes.
    genuinely_resolved = assembly_resolved and contract_resolved
    return MultiSourcePlanningResultV1(
        run_id=None, target_entity=intent.target_entity, candidate_plans=candidate_plans,
        selected_plan_id=compiled.plan_id, result_status=MultiSourceReason.resolved,
        primary_reason_code=None, reason_codes=compiled.reason_codes,
        bounding=conv.bounds, replay_envelope=envelope,
        contract_result_status=compiled.contract_result_status,
        selected_contract_plan_id=compiled.plan_id if genuinely_resolved else None,
        selected_contract_id=compiled.contract_id if genuinely_resolved else None)

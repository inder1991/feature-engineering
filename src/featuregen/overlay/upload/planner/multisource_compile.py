"""Phase 3C.2b-i-A · Task 8 — the multi-source contract compiler + the ONE compile-end union
freshness check (spec §5 step 8, §6).

``compile_multi_source_contract`` is the multi-source analogue of the single-source
``declarations.compile_contract``: it folds the per-path declaration verdicts, the final-combination
verdict, and the compile-end freshness OBSERVATION over an already-assembled
``MultiSourceBindingPlanV1``, and mints a deterministic, freshness-FREE ``contract_id``. It REUSES,
never reimplements:

* **Per-path checks** — the Task-7 ``check_operand_path`` / ``check_time_slot_take_latest`` /
  ``check_paths_temporal_consistency`` over each ``OperandPathV1.binding_plan``, run with A's OWN
  ``CompilerContext``. Production ``build_compiler_context`` hard-codes an EMPTY ``agg_declarations``
  registry (``declarations.py``), so A injects the per-operand aggregation functions here — keyed by
  ``(_operand_recipe_id(op), _OPERAND_NEED_ROLE)`` -> the ``PATH_AGG_TO_FUNCTION``-mapped
  ``AggregationFunction`` — via ``dataclasses.replace`` onto the passed context. The caller must build
  that context with ``roles`` covering every operand/anchor/KEY column (spec §1) so a structural key's
  safety does not silently resolve ``not_evaluated``.

* **Union freshness (CALL, DO NOT edit ``revalidate_freshness``)** — ``union_freshness`` constructs a
  SYNTHETIC single-source ``BindingPlanV1`` whose ``participating_catalogs`` = the UNION of every
  operand path's catalogs, then CALLS the existing ``revalidate_freshness`` on it. Editing
  ``revalidate_freshness`` (it is on the single-source path) would break §12 behaviour-neutrality — so
  A builds a plan and calls it. A path whose OWN catalogs are all fresh can still land in a plan whose
  UNION touches a stale catalog; the union check catches exactly that.

* **Final combination + identity** — the final expression is well-typed at the landing (every
  ``ordered_slot_id``/``time_slot_id`` references a real operand slot) and ``output_additivity`` is
  coherent with the per-path outputs; ``multi_source_contract_id`` mints the identity over landing +
  operand paths + ``path_strategy``s + final expression + versions (mirroring ``make_contract_id``'s
  freshness-free discipline — the freshness observation NEVER enters the id), with
  ``contract_input_hash``/``contract_output_hash`` alongside.

``CompileBudget`` is decremented once per compile (the mutable per-run allowance owned by the harness).
``confirmed_event_id`` is re-queried from ``entity_bridge_edge`` for audit — never widening
``active_bridges`` (finding #8) and never entering any hash (a per-event id is excluded from identity).

Read-only over the reused engine surfaces; nothing here edits ``declarations``/``assembly`` (§12).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from featuregen.overlay.upload.planner.contracts import (
    ADDITIVITY_RULE_VERSION,
    AGGREGATION_RULE_VERSION,
    MULTISOURCE_ASSEMBLY_VERSION,
    OPERATION_POLICY_VERSION,
    PLAN_CONTRACT_VERSION,
    TEMPORAL_RULE_VERSION,
    AdditivityClass,
    BindingPlanV1,
    BindingSafety,
    CandidateRole,
    ContractResolutionStatus,
    DeclarationStatus,
    PathResolutionStatus,
    PlanResolutionStatus,
    PlanTier,
)
from featuregen.overlay.upload.planner.declarations import (
    CompileBudget,
    CompilerContext,
    FreshnessResult,
    revalidate_freshness,
)
from featuregen.overlay.upload.planner.multisource_assembly import (
    _OPERAND_NEED_ROLE,
    OperandPathCandidateV1,
    ResolvedOperandPathV1,
    _operand_recipe_id,
    check_operand_path,
    check_paths_temporal_consistency,
    check_time_slot_take_latest,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    PATH_AGG_TO_FUNCTION,
    FinalOperation,
    GovernedEndpointV1,
    MultiSourceBindingPlanV1,
    MultiSourceDeclarationEvidenceV1,
    MultiSourceReason,
    MultiSourceReplayEnvelopeV1,
    OperandPathV1,
    OperandSlotV1,
    PathAggregation,
    PathDeclarationEvidenceV1,
)

# The final operations whose combination is NON-linear over its operands (a ratio/difference, a
# recency snapshot, a windowed trend): the RESULT can never be additive over fan-out, so declaring it
# ``additive`` is incoherent regardless of the per-path inputs.
_NON_ADDITIVE_OPERATIONS = frozenset({
    FinalOperation.ratio, FinalOperation.difference,
    FinalOperation.recency, FinalOperation.trend})


@dataclass(frozen=True, slots=True)
class MultiSourceContractSpecV1:
    """The injected declaration set A compiles a multi-source plan against (spec §5 step 8, §6).

    Production ``build_compiler_context`` supplies an EMPTY ``agg_declarations`` registry (validate,
    never fabricate — no governed declaration source exists), so A injects the per-operand strategy
    here. Carries the original ``OperandSlotV1``s (keyed by ``slot_id``) — the compiler rebuilds each
    injected ``Template`` byte-identically to enumeration from the operand's ``authoritative_concept``
    + ``source_binding`` + ordering anchor (none of which ``OperandPathV1`` carries) and derives the
    ``(recipe_id, need_role) -> AggregationFunction`` registry via ``PATH_AGG_TO_FUNCTION`` — plus the
    final-combination declaration: the output additivity, the optional window, and whether the paths
    must share ONE as-of treatment at the landing."""
    operands: tuple[OperandSlotV1, ...]
    output_additivity: AdditivityClass
    window: str | None = None
    requires_temporal_consistency: bool = True
    operation_policy_version: str = OPERATION_POLICY_VERSION


# ── identity + hashes (freshness-free, deterministic — mirrors make_contract_id) ─────────────────

def _path_material(path: OperandPathV1) -> str:
    """One operand path's canonical DECLARATION material: the pinned column, the immutable
    ``physical_plan_id`` of its governed ``BindingPlanV1``, its full ``path_strategy``, and its
    governed endpoints' deterministic ``grain_fact_key``s (ref+type — never a per-event id)."""
    s = path.path_strategy
    strategy = (f"{s.aggregation}:{s.output_type}:{s.output_additivity}"
                f":{s.external_type_required}:{s.ordering_anchor_concept or ''}")
    endpoints = ",".join(e.grain_fact_key for e in path.governed_endpoints)
    return (f"{path.slot_id}~{path.semantic_role}~{path.catalog_source}~{path.object_ref}"
            f"~{path.binding_plan.physical_plan_id}~{strategy}~{endpoints}")


def _landing_material(plan: MultiSourceBindingPlanV1) -> str:
    landing = plan.physical_landing
    return f"{landing.catalog}|{landing.table_ref}|{','.join(landing.grain_key_refs)}"


def _final_material(plan: MultiSourceBindingPlanV1) -> str:
    fe = plan.final_expression
    return (f"{fe.operation}:{','.join(fe.ordered_slot_ids)}:{fe.time_slot_id or ''}"
            f":{fe.window or ''}:{fe.output_additivity}")


def _structural_material(plan: MultiSourceBindingPlanV1) -> str:
    """The freshness-FREE structural identity material: landing + operand paths (slot-sorted, so the
    id is invariant to input ordering) + final expression + the compiler rule versions. Shared by the
    contract id and the input hash."""
    paths = ">".join(_path_material(p) for p in sorted(plan.operand_paths, key=lambda p: p.slot_id))
    versions = "|".join((MULTISOURCE_ASSEMBLY_VERSION, OPERATION_POLICY_VERSION,
                         AGGREGATION_RULE_VERSION, ADDITIVITY_RULE_VERSION, TEMPORAL_RULE_VERSION,
                         PLAN_CONTRACT_VERSION))
    return "|".join((_landing_material(plan), paths, _final_material(plan), versions))


def multi_source_contract_id(plan: MultiSourceBindingPlanV1, *,
                             declaration_status: DeclarationStatus) -> str:
    """The multi-source declaration identity (spec §6, mirroring ``make_contract_id``'s F7 discipline):
    minted over WHAT was declared — the declaration verdict + the freshness-FREE structural material
    (landing + operand paths + ``path_strategy``s + final expression + versions). The freshness
    OBSERVATION, the run id, and every per-event id are DELIBERATELY excluded, so identical declarations
    compile to the SAME id — fresh or stale, today or in replay."""
    material = f"{declaration_status.value}|{_structural_material(plan)}"
    return "msc_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _contract_input_hash(plan: MultiSourceBindingPlanV1) -> str:
    """The compiled contract's consumed INPUTS — the freshness-free structural material only (no
    verdict). Stable across a fresh/stale recompile of the same declarations."""
    return "mci_" + hashlib.sha256(_structural_material(plan).encode()).hexdigest()[:16]


def _contract_output_hash(contract_id: str, evidence: MultiSourceDeclarationEvidenceV1,
                          resolution_status: MultiSourceReason) -> str:
    """The compiled contract's freshness-free OUTPUT verdict: the id + the assembly-axis status + the
    per-path/final declaration verdicts. Excludes the freshness observation so it stays stable across a
    stale recompile (finding-#8 identity discipline)."""
    per_path = ";".join(
        f"{e.slot_id}:{','.join(s.validation.value for h in e.hop_aggregations for s in h.ingredient_stages)}"
        f":{e.temporal_declaration.pit_anchor if e.temporal_declaration is not None else ''}"
        for e in evidence.per_path)
    final = f"{evidence.final_verdict.value}:{','.join(r.value for r in evidence.final_reason_codes)}"
    material = f"{contract_id}|{resolution_status.value}|{per_path}|{final}"
    return "mco_" + hashlib.sha256(material.encode()).hexdigest()[:16]


# ── union freshness (CALL revalidate_freshness — never edit it) ──────────────────────────────────

def _union_catalogs(plan: MultiSourceBindingPlanV1) -> tuple[str, ...]:
    """The UNION of every operand path's ``participating_catalogs`` (ordered by first traversal, dedup),
    plus the physical landing catalog. This is the exact catalog set the final assembled contract reads
    from, so freshness must hold across ALL of it — not merely per single-source path."""
    union: list[str] = []
    for path in plan.operand_paths:
        for cat in path.binding_plan.participating_catalogs:
            if cat not in union:
                union.append(cat)
    if plan.physical_landing.catalog not in union:
        union.append(plan.physical_landing.catalog)
    return tuple(union)


def _union_synthetic_plan(plan: MultiSourceBindingPlanV1) -> BindingPlanV1:
    """A minimal synthetic single-source ``BindingPlanV1`` whose ``participating_catalogs`` is the
    union of all operand paths' catalogs — the ONLY field ``revalidate_freshness`` reads off the plan.
    Every other field is inert (no bindings, no segments): the synthetic plan is a carrier for the
    union catalog set, never assembled or stored."""
    catalogs = _union_catalogs(plan)
    return BindingPlanV1(
        physical_plan_id="msc_union", recipe_id="ms:union", target_entity=None,
        tier=PlanTier.tier_1_single_catalog, catalog_source=catalogs[0] if catalogs else "",
        ingredient_bindings=(), path_segments=(),
        resolution_status=PlanResolutionStatus.resolved, primary_reason_code=None, reason_codes=(),
        safety=BindingSafety.safe, preference_rank=0, preference_reasons=(),
        participating_catalogs=catalogs, bridge_count=0,
        path_resolution_status=PathResolutionStatus.source_to_target_resolved,
        candidate_role=CandidateRole.selected)


def union_freshness(conn, ctx: CompilerContext, plan: MultiSourceBindingPlanV1) -> FreshnessResult:
    """The ONE compile-end freshness observation for a multi-source plan (spec §6): CALL the existing
    single-source ``revalidate_freshness`` with a synthetic plan whose ``participating_catalogs`` is the
    UNION of every operand path's catalogs. ``revalidate_freshness`` is NEVER edited (it is on the
    single-source path; editing it would break §12 behaviour-neutrality) — A constructs a plan and calls
    it. A stale/missing/lagging watermark, or a mid-compile catalog/bridge mutation, on ANY union
    catalog fails the check even when each individual path's own catalogs were fresh."""
    return revalidate_freshness(conn, ctx, _union_synthetic_plan(plan))


# ── audit re-query (never widens active_bridges; never enters identity) ──────────────────────────

def confirmed_event_ids_for_audit(
        conn, plan: MultiSourceBindingPlanV1) -> tuple[tuple[str, str | None], ...]:
    """Re-query ``entity_bridge_edge`` for the durable ``confirmed_event_id`` of every VERIFIED bridge
    the plan's operand paths cross (finding #8, spec §3.3). Audit-only: the projection's ``active_bridges``
    deliberately does NOT select ``confirmed_event_id`` (no cardinality/per-event fields), so the store
    re-reads it here instead of widening ``ActiveBridgeV1``. Returns sorted ``(fact_key, confirmed_event_id)``
    pairs; the event ids are audit metadata and NEVER enter any hash (a per-event id is excluded from
    the deterministic contract identity)."""
    fact_keys = sorted({seg.bridge_fact_key
                        for path in plan.operand_paths
                        for seg in path.binding_plan.path_segments
                        if seg.bridge_fact_key is not None})
    if not fact_keys:
        return ()
    rows = conn.execute(
        "SELECT fact_key, confirmed_event_id FROM entity_bridge_edge "
        "WHERE fact_key = ANY(%s) AND status = 'VERIFIED' ORDER BY fact_key",
        (fact_keys,)).fetchall()
    return tuple((row[0], row[1]) for row in rows)


# ── final-combination checks + verdict mapping ───────────────────────────────────────────────────

def _final_well_typed(plan: MultiSourceBindingPlanV1) -> bool:
    """Every ``ordered_slot_id`` and the optional ``time_slot_id`` in the final expression references a
    real operand slot present on the plan (§5 step 7 preservation, at the landing grain)."""
    slot_ids = {p.slot_id for p in plan.operand_paths}
    fe = plan.final_expression
    if not set(fe.ordered_slot_ids) <= slot_ids:
        return False
    return fe.time_slot_id is None or fe.time_slot_id in slot_ids


def _output_additivity_coherent(plan: MultiSourceBindingPlanV1) -> bool:
    """Is the declared final ``output_additivity`` coherent with the per-path outputs (spec §6)?
    Conservative + fail-closed: a non-linear combination (ratio/difference/recency/trend) can NEVER be
    additive, so declaring it ``additive`` is incoherent; a linear combination (identity/count/…) may
    claim ``additive`` ONLY when every per-path output is itself additive. Any other declared class is
    accepted (it claims no more additivity than the inputs support)."""
    fe = plan.final_expression
    if fe.operation in _NON_ADDITIVE_OPERATIONS:
        return fe.output_additivity is not AdditivityClass.additive
    if fe.output_additivity is AdditivityClass.additive:
        return all(p.path_strategy.output_additivity is AdditivityClass.additive
                   for p in plan.operand_paths)
    return True


def _final_combination_reasons(plan: MultiSourceBindingPlanV1) -> tuple[MultiSourceReason, ...]:
    """The final-combination verdict as ``MultiSourceReason``s (empty == sound). A slot the final
    expression drops or references-but-does-not-carry is ``operand_or_slot_not_preserved`` (technical);
    an output additivity claim the per-path outputs cannot support is ``aggregation_unsafe_on_path``
    (the combination's roll-up would be unsound)."""
    reasons: list[MultiSourceReason] = []
    if not _final_well_typed(plan):
        reasons.append(MultiSourceReason.operand_or_slot_not_preserved)
    if not _output_additivity_coherent(plan):
        reasons.append(MultiSourceReason.aggregation_unsafe_on_path)
    return tuple(reasons)


# The MultiSourceReason -> DeclarationStatus mapping for the declaration axis (freshness-free). A
# temporal/ordering failure is a temporal declaration; a dropped/duplicated slot is a connectivity
# failure of the operand into the final expression; every other semantic failure is an aggregation
# declaration failure.
_REASON_DECLARATION_STATUS: dict[MultiSourceReason, DeclarationStatus] = {
    MultiSourceReason.temporal_paths_incompatible: DeclarationStatus.unresolved_temporal_declaration,
    MultiSourceReason.ordering_anchor_missing: DeclarationStatus.unresolved_temporal_declaration,
    MultiSourceReason.operand_or_slot_not_preserved:
        DeclarationStatus.unresolved_ingredient_connectivity,
}


def _declaration_status(primary: MultiSourceReason) -> DeclarationStatus:
    return _REASON_DECLARATION_STATUS.get(
        primary, DeclarationStatus.unresolved_aggregation_declaration)


def _resolved_operand_path(plan: MultiSourceBindingPlanV1, path: OperandPathV1,
                           operand: OperandSlotV1) -> ResolvedOperandPathV1:
    """Rebuild the Task-7 ``ResolvedOperandPathV1`` the per-path checks consume from the stored
    ``OperandPathV1`` + the spec's original ``OperandSlotV1``. Only ``candidate.binding_plan`` is read
    by ``check_operand_path``/``check_time_slot_take_latest``; the landing fields are reconstructed from
    the plan's ``physical_landing`` / the path's landing endpoint for completeness."""
    landing = plan.physical_landing
    endpoint = (path.governed_endpoints[-1] if path.governed_endpoints else GovernedEndpointV1(
        catalog=landing.catalog, table_ref=landing.table_ref,
        grain_key_refs=landing.grain_key_refs, grain_fact_key=""))
    candidate = OperandPathCandidateV1(
        binding_plan=path.binding_plan, landing_catalog=landing.catalog,
        landing_table_ref=landing.table_ref, landing_endpoint=endpoint,
        authority_key=(0, path.binding_plan.bridge_count, 0))
    return ResolvedOperandPathV1(operand=operand, candidate=candidate)


def _inject_declarations(ctx: CompilerContext,
                         spec: MultiSourceContractSpecV1) -> CompilerContext:
    """A's OWN context for the per-path checks: the passed context with the spec's per-operand
    aggregation declarations INJECTED (production ``build_compiler_context`` hard-codes ``{}``). Keyed
    exactly as Task-5 enumeration keyed the operand's ``recipe_id``/``need_role`` so the reused
    ``compile_aggregation`` validates the DECLARED strategy. ``avg``/``stddev`` map to ``None`` (no
    additive/order-safe analog) and are left undeclared — the aggregation matrix then resolves them
    from additivity (``avg``) or fails them closed downstream (``stddev``)."""
    injected = dict(ctx.agg_declarations)
    for operand in spec.operands:
        fn = PATH_AGG_TO_FUNCTION[operand.path_strategy.aggregation]
        if fn is not None:
            injected[(_operand_recipe_id(operand), _OPERAND_NEED_ROLE)] = fn
    return replace(ctx, agg_declarations=injected)


def compile_multi_source_contract(
        conn, ctx: CompilerContext, plan: MultiSourceBindingPlanV1,
        spec: MultiSourceContractSpecV1, *, base_envelope: MultiSourceReplayEnvelopeV1,
        budget: CompileBudget) -> MultiSourceBindingPlanV1:
    """Compile ONE assembled ``MultiSourceBindingPlanV1`` into a governed contract (spec §5 step 8, §6).

    Folds, in precedence order: (1) the Task-7 per-path checks over each ``OperandPathV1.binding_plan``
    (reused — never re-running the compiler here) with the spec's declarations injected into A's own
    context; (2) cross-path temporal consistency; (3) the final-combination well-typedness +
    ``output_additivity`` coherence; then the compile-end (4) UNION freshness OBSERVATION by CALLING
    ``revalidate_freshness`` with a union-catalog synthetic plan. Mints the deterministic, freshness-FREE
    ``contract_id`` (+ input/output hashes), decrements the ``CompileBudget`` once, and re-queries
    ``entity_bridge_edge`` for the audit ``confirmed_event_id``s. RESOLVES (``resolution_status`` =
    ``resolved``, ``contract_result_status`` = ``resolved``) only when the per-path + final + union
    checks all pass; a stale union yields ``unresolved_freshness`` but STILL a minted (identity-bearing)
    contract id, exactly as the single-source compiler does. ``base_envelope`` is the replay fingerprint
    material; its run id / input hash never enter the identity."""
    budget.remaining -= 1   # the per-compile decrement (the mutable per-run allowance, spec §6/C8)

    work_ctx = _inject_declarations(ctx, spec)
    by_slot = {op.slot_id: op for op in spec.operands}

    # (1) per-path checks — REUSE Task 7 over each operand path's governed binding_plan.
    per_path_evidence: list[PathDeclarationEvidenceV1] = []
    per_path_temporals = []
    per_path_reasons: list[MultiSourceReason] = []
    unsupported = False
    for path in plan.operand_paths:
        operand = by_slot.get(path.slot_id)
        if operand is None:
            per_path_reasons.append(MultiSourceReason.operand_or_slot_not_preserved)
            continue
        if operand.path_strategy.aggregation is PathAggregation.stddev:
            unsupported = True   # no additive/order-safe analog — fail closed (spec §4)
        resolved = _resolved_operand_path(plan, path, operand)
        temporal, hop_aggregations, path_reason = check_operand_path(work_ctx, resolved)
        anchor_reason = check_time_slot_take_latest(resolved)
        per_path_temporals.append(temporal)
        per_path_evidence.append(PathDeclarationEvidenceV1(
            slot_id=path.slot_id, hop_aggregations=hop_aggregations, temporal_declaration=temporal))
        if path_reason is not None:
            per_path_reasons.append(path_reason)
        if anchor_reason is not None:
            per_path_reasons.append(anchor_reason)

    # (2) cross-path temporal consistency at the common landing.
    temporal_reason = (check_paths_temporal_consistency(per_path_temporals)
                       if spec.requires_temporal_consistency else None)

    # (3) final-combination checks.
    final_reasons = _final_combination_reasons(plan)

    # audit: re-query the durable confirmed_event_id for every crossed VERIFIED bridge (never widening
    # active_bridges, never hashed) and fail closed if a referenced bridge is no longer VERIFIED.
    referenced_bridges = {seg.bridge_fact_key for path in plan.operand_paths
                          for seg in path.binding_plan.path_segments
                          if seg.bridge_fact_key is not None}
    audited = confirmed_event_ids_for_audit(conn, plan)
    missing_bridges = referenced_bridges - {fk for fk, _ in audited}

    # assemble the freshness-FREE declaration verdict (ordered by precedence).
    semantic_reasons: list[MultiSourceReason] = []
    if unsupported:
        semantic_reasons.append(MultiSourceReason.unsupported_path_aggregation)
    semantic_reasons.extend(per_path_reasons)
    if temporal_reason is not None:
        semantic_reasons.append(temporal_reason)
    semantic_reasons.extend(final_reasons)
    if missing_bridges:
        semantic_reasons.append(MultiSourceReason.technical_failure)

    declaration_ok = not semantic_reasons
    final_verdict = (DeclarationStatus.resolved if not final_reasons
                     else _declaration_status(final_reasons[0]))

    if declaration_ok:
        declaration_status = DeclarationStatus.resolved
        resolution_status = MultiSourceReason.resolved
        # freshness (observation) folds into the CONTRACT axis ONLY — CALL revalidate_freshness.
        freshness = union_freshness(conn, work_ctx, plan)
        contract_result_status = freshness.status
    else:
        declaration_status = _declaration_status(semantic_reasons[0])
        resolution_status = semantic_reasons[0]
        contract_result_status = ContractResolutionStatus(declaration_status.value)

    contract_id = multi_source_contract_id(plan, declaration_status=declaration_status)
    evidence = MultiSourceDeclarationEvidenceV1(
        per_path=tuple(per_path_evidence), final_verdict=final_verdict,
        final_reason_codes=final_reasons)
    reason_codes = tuple(dict.fromkeys(semantic_reasons))   # dedup, precedence order preserved
    return replace(
        plan, resolution_status=resolution_status, reason_codes=reason_codes,
        contract_result_status=contract_result_status, contract_id=contract_id,
        declaration_evidence=evidence, contract_input_hash=_contract_input_hash(plan),
        contract_output_hash=_contract_output_hash(contract_id, evidence, resolution_status))

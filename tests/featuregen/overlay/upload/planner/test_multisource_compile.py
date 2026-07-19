"""Phase 3C.2b-i-A · Task 8 — the multi-source contract compiler + the compile-end union freshness
check (spec §5 step 8, §6).

``compile_multi_source_contract`` folds three things over an already-assembled
``MultiSourceBindingPlanV1``:

* **Per-path checks (REUSE Task 7):** ``check_operand_path``/``check_time_slot_take_latest``/
  ``check_paths_temporal_consistency`` over each ``OperandPathV1.binding_plan`` with A's OWN
  ``CompilerContext`` (the spec's per-operand aggregation declarations injected — production
  ``build_compiler_context`` hard-codes an EMPTY registry).
* **Union freshness (CALL, do NOT edit ``revalidate_freshness``):** ``union_freshness`` builds a
  synthetic single-source ``BindingPlanV1`` whose ``participating_catalogs`` = the UNION of every
  operand path's catalogs and CALLS the existing ``revalidate_freshness``. A path whose OWN catalogs
  are all fresh can still land in a plan whose UNION touches a stale catalog — the union check catches
  it.
* **Final combination + identity:** the final expression is well-typed at the landing +
  ``output_additivity`` is coherent; a deterministic, freshness-FREE ``contract_id`` (mirroring
  ``make_contract_id``'s discipline) over landing + operand paths + ``path_strategy``s + final
  expression + versions, plus ``contract_input_hash``/``contract_output_hash``. ``CompileBudget`` is
  decremented per compile; ``confirmed_event_id`` is re-queried from ``entity_bridge_edge`` for audit.

Fixtures are seeded through the REAL governance write paths exactly as the Task-5/Task-7 suites do
(``build_graph``; ``entity_bridge_edge`` VERIFIED bridges; the ``propose_fact``/``_confirm_grain``
four-eyes grain flow; ``overlay_drift_watermark`` drift watermarks).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload.conftest import _confirm_grain

from featuregen.contracts.envelopes import Command
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import (
    AdditivityClass,
    BindingPathSegmentV1,
    BindingPlanV1,
    BindingSafety,
    CandidateRole,
    CatalogScopeV1,
    ContractResolutionStatus,
    DeclarationStatus,
    PathResolutionStatus,
    PhysicalReadSetV1,
    PlanResolutionStatus,
    PlanTier,
    ReasonCode,
    SegmentKind,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget, revalidate_freshness
from featuregen.overlay.upload.planner.multisource_assembly import (
    converge,
    enumerate_operand_paths,
)
from featuregen.overlay.upload.planner.multisource_compile import (
    MultiSourceContractSpecV1,
    compile_multi_source_contract,
    confirmed_event_ids_for_audit,
    multi_source_contract_id,
    union_freshness,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    FinalOperation,
    GovernedEndpointV1,
    GovernedSourceBindingV1,
    MultiSourceBindingPlanV1,
    MultiSourceDeclarationEvidenceV1,
    MultiSourceReason,
    OperandPathV1,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    PhysicalLandingV1,
    SemanticRole,
)
from featuregen.overlay.upload.planner.multisource_reuse import build_operand_context
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 19, tzinfo=UTC)

# The DETERMINISTIC grain fact_key of the enumerated operand's SOURCE table (core_banking.transactions)
# — the source-endpoint revalidation (spec §2/§3.2) compares the binding's claimed grain key to it.
_SRC_GRAIN_FK = fact_key(table_ref("core_banking", "transactions"), "grain")


# ── seed helpers (the sanctioned Task-5/Task-7 pattern) ────────────────────────────────────────
def _seed(db, source, rows_concepts):
    rows = [r for r, _ in rows_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _seed_verified_bridge(db, fact_key, entity_id, lc, lref, rc, rref):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, confirmed_event_id, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fact_key, entity_id, lc, lref, rc, rref, f"evt-{fact_key}"))


def _seed_verified_grain(db, source, table, columns, *, service_actor, human_actor):
    ref = table_ref(source, table)
    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain",
         "proposed_value": {"columns": columns, "is_unique": True}},
        service_actor, f"propose-grain-{source}-{table}"))
    assert res.accepted, res.denied_reason
    _confirm_grain(db, source, table, columns, actor=human_actor)


def _watermark(db, source, at, head_seq=0):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id,"
        " head_seq) VALUES (%s,%s,'drift_t8',%s) ON CONFLICT (catalog_source) DO UPDATE SET"
        " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq",
        (source, at, head_seq))


def _scope(*catalogs):
    return CatalogScopeV1(
        scope_id="ms-compile", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-19T00:00:00Z",
        catalog_consideration_truncated=False)


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


def _budget(remaining=8):
    return CompileBudget(remaining=remaining, deadline_monotonic=float("inf"), clock=time.monotonic)


def _strategy(aggregation=PathAggregation.sum, output_additivity=AdditivityClass.additive,
              anchor_concept=None):
    return PathStrategyV1(
        aggregation=aggregation, output_type="numeric", output_additivity=output_additivity,
        external_type_required=False, ordering_anchor_concept=anchor_concept)


def _operand(*, slot_id, catalog, object_ref="public.transactions.amount",
             concept="monetary_flow", source_entity="transaction",
             source_key_ref="public.transactions.transaction_id",
             semantic_role=SemanticRole.measure, strategy=None):
    return OperandSlotV1(
        slot_id=slot_id, semantic_role=semantic_role, catalog_source=catalog,
        object_ref=object_ref, authoritative_concept=concept,
        path_strategy=strategy or _strategy(),
        source_binding=GovernedSourceBindingV1(
            source_grain_entity=source_entity, source_grain_key_refs=(source_key_ref,),
            grain_fact_key=_SRC_GRAIN_FK))


# ── real-assembly helpers (enumerate -> converge -> build the MultiSourceBindingPlanV1) ─────────
def _operand_path(operand, candidate):
    return OperandPathV1(
        slot_id=operand.slot_id, semantic_role=operand.semantic_role,
        catalog_source=operand.catalog_source, object_ref=operand.object_ref,
        binding_plan=candidate.binding_plan, governed_endpoints=candidate.governed_endpoints,
        path_strategy=operand.path_strategy, pit_treatment="")


def _empty_evidence():
    return MultiSourceDeclarationEvidenceV1(per_path=(), final_verdict=DeclarationStatus.not_compiled)


def _ms_plan(landing, operand_paths, final_expression):
    return MultiSourceBindingPlanV1(
        plan_id="msp_t8", physical_landing=landing, operand_paths=tuple(operand_paths),
        final_expression=final_expression, physical_read_set=PhysicalReadSetV1(columns=()),
        resolution_status=MultiSourceReason.resolved, reason_codes=(),
        contract_result_status=ContractResolutionStatus.not_compiled, contract_id=None,
        declaration_evidence=_empty_evidence(), contract_input_hash="", contract_output_hash="")


def _assemble_identity(conn, scope, operand):
    """Enumerate ONE operand, converge it onto its own landing, and wrap the result into an
    IDENTITY-combination ``MultiSourceBindingPlanV1`` — the compile input. Rebuilds a fresh ctx each
    call so the two determinism runs are genuinely independent."""
    ctx = build_operand_context(conn, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=_NOW, agg_declarations={})
    enum = enumerate_operand_paths(conn, _adapter(), ctx, operand=operand,
                                   target_entity="customer", scope=scope,
                                   roles=("feature_engineer",), now=_NOW)
    assert enum.candidates, f"expected a governed candidate, got {enum.status}"
    conv = converge([enum], bounds=enum.bounds)
    assert conv.landed_combinations, f"expected convergence, got {conv.status}"
    combo = conv.landed_combinations[0]
    op_path = _operand_path(operand, combo.operand_candidates[0])
    final = FinalExpressionV1(
        operation=FinalOperation.identity, ordered_slot_ids=(operand.slot_id,),
        time_slot_id=None, window=None, output_additivity=AdditivityClass.additive)
    return ctx, _ms_plan(combo.landing, [op_path], final)


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def resolved_topology(db, service_actor, human_actor):
    """The Task-5 bridged topology (core_banking.transactions -> VERIFIED bridge at account ->
    intra-wealth realization -> wealth.customers, VERIFIED grain), with FRESH drift watermarks on
    both participating catalogs so the union freshness check resolves."""
    ensure_upload_catalog_adapter()
    _seed(db, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core_banking", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("core_banking", "transactions", "amount", "numeric"), "monetary_flow"),
    ])
    _seed(db, "wealth", [
        (CanonicalRow("wealth", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("wealth", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("wealth", "customers", "customer_id", "integer", is_grain=True),
         "customer_id"),
    ])
    _seed_verified_bridge(db, "bfk_acct", "account",
                          "core_banking", "public.transactions.account_id",
                          "wealth", "public.accounts.account_id")
    # every hop endpoint governed: source transactions, intermediate wealth.accounts, landing customers
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _watermark(db, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(db, "wealth", _NOW - timedelta(minutes=5))
    return db, _scope("core_banking", "wealth")


# ── union freshness: a path fresh on its OWN catalogs, but the UNION touches a stale one ─────────
def _stale_union_plan(landing_ep):
    """Two hand-built operand paths — one over {core_banking, wealth}, one over {retail, wealth} —
    landing on wealth.customers. Empty ``binding_plan``s (no bindings/segments) pass the per-path
    checks vacuously, so the ONLY thing that can fail is the compile-end union freshness."""
    def _bp(pid, catalogs):
        return BindingPlanV1(
            physical_plan_id=pid, recipe_id="ms:hand", target_entity="customer",
            tier=PlanTier.tier_1_single_catalog, catalog_source=catalogs[0],
            ingredient_bindings=(), path_segments=(),
            resolution_status=PlanResolutionStatus.resolved, primary_reason_code=None,
            reason_codes=(), safety=BindingSafety.safe, preference_rank=0, preference_reasons=(),
            participating_catalogs=tuple(catalogs), bridge_count=0,
            path_resolution_status=PathResolutionStatus.source_to_target_resolved,
            candidate_role=CandidateRole.selected)

    op0 = OperandPathV1(
        slot_id="op_0", semantic_role=SemanticRole.minuend, catalog_source="core_banking",
        object_ref="public.transactions.amount", binding_plan=_bp("bp0", ["core_banking", "wealth"]),
        governed_endpoints=(landing_ep,), path_strategy=_strategy(), pit_treatment="")
    op1 = OperandPathV1(
        slot_id="op_1", semantic_role=SemanticRole.subtrahend, catalog_source="retail",
        object_ref="public.orders.amount", binding_plan=_bp("bp1", ["retail", "wealth"]),
        governed_endpoints=(landing_ep,), path_strategy=_strategy(), pit_treatment="")
    landing = PhysicalLandingV1(catalog="wealth", table_ref="public.customers",
                               grain_key_refs=("public.customers.customer_id",))
    final = FinalExpressionV1(
        operation=FinalOperation.difference, ordered_slot_ids=("op_0", "op_1"),
        time_slot_id=None, window=None, output_additivity=AdditivityClass.non_additive)
    return _ms_plan(landing, [op0, op1], final)


def _stale_union_spec():
    return MultiSourceContractSpecV1(
        operands=(
            _operand(slot_id="op_0", catalog="core_banking", semantic_role=SemanticRole.minuend),
            _operand(slot_id="op_1", catalog="retail", object_ref="public.orders.amount",
                     source_entity="order", source_key_ref="public.orders.order_id",
                     semantic_role=SemanticRole.subtrahend)),
        output_additivity=AdditivityClass.non_additive, window=None,
        requires_temporal_consistency=True)


@pytest.fixture
def stale_union_db(db):
    """Drift watermarks: core_banking + wealth FRESH, retail STALE (2h > 60min SLA). No graph rows
    needed — the union check reads only the watermark table."""
    _watermark(db, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(db, "wealth", _NOW - timedelta(minutes=5))
    _watermark(db, "retail", _NOW - timedelta(hours=2))
    return db


def _union_ctx(conn):
    return build_operand_context(conn, catalogs=["core_banking", "wealth", "retail"],
                                 roles=("feature_engineer",), now=_NOW, agg_declarations={})


def test_path_fresh_on_its_own_catalogs_but_union_hits_stale_watermark(stale_union_db):
    conn = stale_union_db
    ctx = _union_ctx(conn)
    landing_ep = GovernedEndpointV1(
        catalog="wealth", table_ref="public.customers",
        grain_key_refs=("public.customers.customer_id",), grain_fact_key="grain-fk")
    plan = _stale_union_plan(landing_ep)

    # operand 0's OWN catalogs {core_banking, wealth} are both fresh -> individually fresh
    op0_only = revalidate_freshness(conn, ctx, plan.operand_paths[0].binding_plan)
    assert op0_only.status is ContractResolutionStatus.resolved

    # but the UNION {core_banking, wealth, retail} includes the stale retail -> the union check fails
    union = union_freshness(conn, ctx, plan)
    assert union.status is ContractResolutionStatus.unresolved_freshness
    assert ReasonCode.participating_catalog_stale in union.reason_codes


def test_compile_over_stale_union_is_unresolved_freshness_but_still_minted(stale_union_db):
    conn = stale_union_db
    ctx = _union_ctx(conn)
    landing_ep = GovernedEndpointV1(
        catalog="wealth", table_ref="public.customers",
        grain_key_refs=("public.customers.customer_id",), grain_fact_key="grain-fk")
    plan = _stale_union_plan(landing_ep)

    out = compile_multi_source_contract(conn, ctx, plan, _stale_union_spec(), budget=_budget())

    # the paths themselves are sound (assembly axis resolved); ONLY the freshness observation fails
    assert out.resolution_status is MultiSourceReason.resolved
    assert out.contract_result_status is ContractResolutionStatus.unresolved_freshness
    # the DECLARATION identity is freshness-free, so a stale plan still gets a deterministic id
    assert out.contract_id is not None


# ── consistent plan -> resolved, with a deterministic contract_id across two runs ────────────────
def test_consistent_plan_resolves_with_deterministic_contract_id(resolved_topology):
    conn, scope = resolved_topology
    operand = _operand(slot_id="op_0", catalog="core_banking")
    spec = MultiSourceContractSpecV1(
        operands=(operand,), output_additivity=AdditivityClass.additive, window=None,
        requires_temporal_consistency=False)

    # run 1
    ctx1, plan1 = _assemble_identity(conn, scope, operand)
    out1 = compile_multi_source_contract(conn, ctx1, plan1, spec, budget=_budget())

    # run 2 — an independently rebuilt ctx, SAME seeded fact_keys / topology
    ctx2, plan2 = _assemble_identity(conn, scope, operand)
    out2 = compile_multi_source_contract(conn, ctx2, plan2, spec, budget=_budget())

    assert out1.contract_result_status is ContractResolutionStatus.resolved
    assert out2.contract_result_status is ContractResolutionStatus.resolved
    assert out1.resolution_status is MultiSourceReason.resolved
    assert out1.contract_id is not None
    # freshness-free identity: two independent compiles of the same declarations mint the SAME id
    assert out1.contract_id == out2.contract_id
    # the standalone identity function agrees with the compiled id
    assert out1.contract_id == multi_source_contract_id(
        plan1, declaration_status=DeclarationStatus.resolved)
    # evidence is populated (one per-path verdict + a resolved final verdict)
    assert len(out1.declaration_evidence.per_path) == 1
    assert out1.declaration_evidence.final_verdict is DeclarationStatus.resolved
    assert out1.contract_input_hash and out1.contract_output_hash


def test_confirmed_event_id_requeried_from_entity_bridge_edge_for_audit(resolved_topology):
    conn, scope = resolved_topology
    operand = _operand(slot_id="op_0", catalog="core_banking")
    _ctx, plan = _assemble_identity(conn, scope, operand)

    # the operand's governed path crosses via the VERIFIED bridge bfk_acct -> re-queried for audit,
    # carrying the durable confirmed_event_id (NEVER widening active_bridges)
    audit = confirmed_event_ids_for_audit(conn, plan)
    assert ("bfk_acct", "evt-bfk_acct") in audit


# ── CompileBudget decremented by 1 per compile ──────────────────────────────────────────────────
def test_compile_decrements_budget_by_one_per_compile(resolved_topology):
    conn, scope = resolved_topology
    operand = _operand(slot_id="op_0", catalog="core_banking")
    spec = MultiSourceContractSpecV1(
        operands=(operand,), output_additivity=AdditivityClass.additive, window=None,
        requires_temporal_consistency=False)
    budget = _budget(remaining=5)

    ctx, plan = _assemble_identity(conn, scope, operand)
    compile_multi_source_contract(conn, ctx, plan, spec, budget=budget)
    assert budget.remaining == 4

    ctx2, plan2 = _assemble_identity(conn, scope, operand)
    compile_multi_source_contract(conn, ctx2, plan2, spec, budget=budget)
    assert budget.remaining == 3


# ── I-1: a KEY column outside the roles read-scope -> not_evaluated -> NON-resolved (fail closed) ──
def _bridge_segment_plan(pid, *, landing_ep, tier=PlanTier.tier_2_one_bridge):
    """One operand path over an EMPTY binding_plan (per-path checks pass vacuously) carrying a single
    governed-bridge segment over the seeded bridge. build_physical_read_set inventories the bridge's
    BOTH endpoint columns as bridge_key reads — the safety surface the compiler must self-check."""
    bp = BindingPlanV1(
        physical_plan_id=pid, recipe_id="ms:hand", target_entity="customer",
        tier=tier, catalog_source="core_banking", ingredient_bindings=(),
        path_segments=(BindingPathSegmentV1(
            segment_kind=SegmentKind.governed_bridge, catalog_source="core_banking",
            bridge_fact_key="bfk_acct"),),
        resolution_status=PlanResolutionStatus.resolved, primary_reason_code=None, reason_codes=(),
        safety=BindingSafety.safe, preference_rank=0, preference_reasons=(),
        participating_catalogs=("core_banking", "wealth"), bridge_count=1,
        path_resolution_status=PathResolutionStatus.source_to_target_resolved,
        candidate_role=CandidateRole.selected)
    op0 = OperandPathV1(
        slot_id="op_0", semantic_role=SemanticRole.measure, catalog_source="core_banking",
        object_ref="public.transactions.amount", binding_plan=bp,
        governed_endpoints=(landing_ep,), path_strategy=_strategy(), pit_treatment="")
    landing = PhysicalLandingV1(catalog="wealth", table_ref="public.customers",
                               grain_key_refs=("public.customers.customer_id",))
    final = FinalExpressionV1(
        operation=FinalOperation.identity, ordered_slot_ids=("op_0",),
        time_slot_id=None, window=None, output_additivity=AdditivityClass.additive)
    return _ms_plan(landing, [op0], final)


def _single_operand_spec():
    return MultiSourceContractSpecV1(
        operands=(_operand(slot_id="op_0", catalog="core_banking"),),
        output_additivity=AdditivityClass.additive, window=None,
        requires_temporal_consistency=False)


def _bridge_endpoints(db, *, right_sensitivity=""):
    """Seed the bridge endpoint key columns + the VERIFIED crossing + FRESH watermarks. The RIGHT
    endpoint (wealth.accounts.account_id) takes ``right_sensitivity`` so a caller can put it OUTSIDE a
    plain feature_engineer read-scope."""
    _seed(db, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("core_banking", "transactions", "amount", "numeric"), "monetary_flow"),
    ])
    _seed(db, "wealth", [
        (CanonicalRow("wealth", "accounts", "account_id", "integer", is_grain=True,
                      sensitivity=right_sensitivity), "account_id"),
        (CanonicalRow("wealth", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _seed_verified_bridge(db, "bfk_acct", "account",
                          "core_banking", "public.transactions.account_id",
                          "wealth", "public.accounts.account_id")
    _watermark(db, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(db, "wealth", _NOW - timedelta(minutes=5))


_LANDING_EP = GovernedEndpointV1(
    catalog="wealth", table_ref="public.customers",
    grain_key_refs=("public.customers.customer_id",), grain_fact_key="grain-fk")


def test_not_evaluated_key_outside_read_scope_is_unresolved_safety_evaluation(db):
    # the bridge's RIGHT endpoint key is RESTRICTED -> invisible to a plain feature_engineer scope.
    _bridge_endpoints(db, right_sensitivity="restricted")
    plan = _bridge_segment_plan("bp_ne", landing_ep=_LANDING_EP)

    # roles WITHOUT restricted_reader: the key column is never loaded, so its universal safety resolves
    # not_evaluated. Task-7 check_operand_path ignores not_evaluated, so the compiler must self-check
    # and fail closed -> a not_evaluated read can NEVER mint a resolved contract (mirrors single-source).
    ctx = build_operand_context(db, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=_NOW, agg_declarations={})
    out = compile_multi_source_contract(db, ctx, plan, _single_operand_spec(), budget=_budget())
    assert out.contract_result_status is ContractResolutionStatus.unresolved_safety_evaluation
    assert out.contract_result_status is not ContractResolutionStatus.resolved
    assert out.contract_id is not None      # still minted (the identity stays freshness-free)

    # CONTROL: grant restricted_reader -> the SAME plan loads the key safe and RESOLVES, proving the
    # roles read-scope gap (not the plan structure) drove the non-resolution.
    ctx_ok = build_operand_context(db, catalogs=["core_banking", "wealth"],
                                   roles=("feature_engineer", "restricted_reader"), now=_NOW,
                                   agg_declarations={})
    out_ok = compile_multi_source_contract(db, ctx_ok, plan, _single_operand_spec(), budget=_budget())
    assert out_ok.contract_result_status is ContractResolutionStatus.resolved


# ── I-2: a bridge revoked MID-compile flows to freshness, never a technical_failure gate ──────────
def test_bridge_revoked_mid_compile_is_unresolved_freshness_not_technical_failure(db):
    _bridge_endpoints(db)   # both endpoints loadable/safe under feature_engineer
    plan = _bridge_segment_plan("bp_br", landing_ep=_LANDING_EP)

    # ctx captures the bridge fingerprint WITH bfk_acct present ...
    ctx = build_operand_context(db, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=_NOW, agg_declarations={})
    # ... then the crossing is revoked mid-compile.
    db.execute("DELETE FROM entity_bridge_edge WHERE fact_key = 'bfk_acct'")

    out = compile_multi_source_contract(db, ctx, plan, _single_operand_spec(), budget=_budget())

    # the missing/revoked bridge is caught by revalidate_freshness's bridge fingerprint (the freshness
    # OBSERVATION axis) — NOT a compile-time technical_failure gate on the declaration axis.
    assert out.resolution_status is MultiSourceReason.resolved
    assert out.contract_result_status is ContractResolutionStatus.unresolved_freshness
    assert out.contract_id is not None
    assert MultiSourceReason.technical_failure not in out.reason_codes


# ── M-2: §5 step-7 preservation — every operand referenced EXACTLY once by the final expression ────
def _preservation_plan(final_expression):
    """Two operand paths (op_0, op_1) over EMPTY binding_plans (per-path vacuously sound) combined by a
    CALLER-supplied final expression — so the ONLY thing that can fail is §5-step-7 preservation."""
    def _bp(pid, cat):
        return BindingPlanV1(
            physical_plan_id=pid, recipe_id="ms:hand", target_entity="customer",
            tier=PlanTier.tier_1_single_catalog, catalog_source=cat, ingredient_bindings=(),
            path_segments=(), resolution_status=PlanResolutionStatus.resolved,
            primary_reason_code=None, reason_codes=(), safety=BindingSafety.safe, preference_rank=0,
            preference_reasons=(), participating_catalogs=(cat,), bridge_count=0,
            path_resolution_status=PathResolutionStatus.source_to_target_resolved,
            candidate_role=CandidateRole.selected)
    op0 = OperandPathV1(slot_id="op_0", semantic_role=SemanticRole.minuend,
                        catalog_source="core_banking", object_ref="public.transactions.amount",
                        binding_plan=_bp("bp0", "core_banking"), governed_endpoints=(_LANDING_EP,),
                        path_strategy=_strategy(), pit_treatment="")
    op1 = OperandPathV1(slot_id="op_1", semantic_role=SemanticRole.subtrahend,
                        catalog_source="wealth", object_ref="public.balances.amount",
                        binding_plan=_bp("bp1", "wealth"), governed_endpoints=(_LANDING_EP,),
                        path_strategy=_strategy(), pit_treatment="")
    landing = PhysicalLandingV1(catalog="wealth", table_ref="public.customers",
                               grain_key_refs=("public.customers.customer_id",))
    return _ms_plan(landing, [op0, op1], final_expression)


def _preservation_spec():
    return MultiSourceContractSpecV1(
        operands=(
            _operand(slot_id="op_0", catalog="core_banking", semantic_role=SemanticRole.minuend),
            _operand(slot_id="op_1", catalog="wealth", object_ref="public.balances.amount",
                     source_entity="balance", source_key_ref="public.balances.balance_id",
                     semantic_role=SemanticRole.subtrahend)),
        output_additivity=AdditivityClass.non_additive, window=None,
        requires_temporal_consistency=False)


@pytest.mark.parametrize("ordered", [("op_0",), ("op_0", "op_0")],
                         ids=["dropped_operand", "duplicate_operand"])
def test_final_expression_must_preserve_every_operand_exactly_once(db, ordered):
    final = FinalExpressionV1(
        operation=FinalOperation.difference, ordered_slot_ids=ordered,
        time_slot_id=None, window=None, output_additivity=AdditivityClass.non_additive)
    plan = _preservation_plan(final)
    ctx = build_operand_context(db, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=_NOW, agg_declarations={})
    out = compile_multi_source_contract(db, ctx, plan, _preservation_spec(), budget=_budget())
    # a dropped OR duplicated operand slot is a §5-step-7 preservation failure (technical declaration).
    assert MultiSourceReason.operand_or_slot_not_preserved in out.reason_codes
    assert out.resolution_status is MultiSourceReason.operand_or_slot_not_preserved
    assert out.contract_result_status is not ContractResolutionStatus.resolved

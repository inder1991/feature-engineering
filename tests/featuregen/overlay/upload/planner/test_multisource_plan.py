"""Phase 3C.2b-i-A · Task 9 — ``plan_multi_source`` orchestration (spec §5 order, §3.3 result).

``plan_multi_source`` ties Tasks 3-8 into a ``MultiSourcePlanningResultV1``:

  shape (Task 3) -> enumerate per operand (Task 5) -> converge (Task 6) -> per-path checks (Task 7)
  -> final join + preservation -> compile (Task 8) -> select + the TWO-AXIS resolve gate -> assemble.

The suite drives the WHOLE pipeline over a REAL seeded topology (the sanctioned Task-5/Task-8 seed
pattern: ``build_graph``; VERIFIED ``entity_bridge_edge``; the ``propose_fact``/``_confirm_grain``
four-eyes grain flow; ``overlay_drift_watermark`` drift watermarks), so nothing is stubbed:

  * a valid RATIO resolves — one selected candidate, preservation holds, ``selected_plan_id`` set;
  * a shape-invalid intent -> ``operand_shape_invalid``, NO candidates (no DB read);
  * an operand whose governed landing has no grain fact -> ``realization_endpoint_ungoverned``;
  * THE TWO-AXIS GATE: a plan that compiles to ``resolution_status=resolved`` but
    ``contract_result_status=unresolved_freshness`` must NOT be selected as a resolved contract — the
    non-resolution surfaces on the contract axis (keying resolve on ``resolution_status`` alone is a
    fail-open);
  * a raised DB error PROPAGATES (never swallowed into a technical status here).
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
    OPERATION_POLICY_VERSION,
    AdditivityClass,
    CatalogScopeV1,
    ContractResolutionStatus,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    FinalOperation,
    GovernedSourceBindingV1,
    MultiSourcePlannerIntentV1,
    MultiSourceReason,
    MultiSourceReplayEnvelopeV1,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    SemanticRole,
)
from featuregen.overlay.upload.planner.multisource_plan import plan_multi_source
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 19, tzinfo=UTC)

# The DETERMINISTIC grain fact_key of the operands' SOURCE table (core_banking.transactions) — the
# source-endpoint revalidation (spec §2/§3.2) compares the binding's claimed grain key against it.
_SRC_GRAIN_FK = fact_key(table_ref("core_banking", "transactions"), "grain")


# ── seed helpers (the sanctioned Task-5/Task-8 pattern) ────────────────────────────────────────
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
        " head_seq) VALUES (%s,%s,'drift_t9',%s) ON CONFLICT (catalog_source) DO UPDATE SET"
        " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq",
        (source, at, head_seq))


def _scope(*catalogs):
    return CatalogScopeV1(
        scope_id="ms-plan", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-19T00:00:00Z",
        catalog_consideration_truncated=False)


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


# ── intent builders ────────────────────────────────────────────────────────────────────────────
def _strategy(aggregation=PathAggregation.sum, output_additivity=AdditivityClass.additive,
              anchor_concept=None):
    return PathStrategyV1(
        aggregation=aggregation, output_type="numeric", output_additivity=output_additivity,
        external_type_required=False, ordering_anchor_concept=anchor_concept)


def _operand(*, slot_id, catalog="core_banking", object_ref="public.transactions.amount",
             concept="monetary_flow", source_entity="transaction",
             source_key_ref="public.transactions.transaction_id",
             semantic_role=SemanticRole.numerator, strategy=None):
    return OperandSlotV1(
        slot_id=slot_id, semantic_role=semantic_role, catalog_source=catalog,
        object_ref=object_ref, authoritative_concept=concept,
        path_strategy=strategy or _strategy(),
        source_binding=GovernedSourceBindingV1(
            source_grain_entity=source_entity, source_grain_key_refs=(source_key_ref,),
            grain_fact_key=_SRC_GRAIN_FK))


def _ratio_intent():
    """A valid RATIO of two governed operands (numerator + denominator), each summed to the common
    wealth.customers landing — the canonical positive shape."""
    return MultiSourcePlannerIntentV1(
        target_entity="customer",
        operands=(
            _operand(slot_id="op_num", semantic_role=SemanticRole.numerator),
            _operand(slot_id="op_den", semantic_role=SemanticRole.denominator)),
        final_expression=FinalExpressionV1(
            operation=FinalOperation.ratio, ordered_slot_ids=("op_num", "op_den"),
            time_slot_id=None, window=None, output_additivity=AdditivityClass.non_additive),
        operation_policy_version=OPERATION_POLICY_VERSION)


def _shape_invalid_intent():
    """RATIO requires numerator + denominator; a lone numerator is a role-multiset violation ->
    ``operand_shape_invalid`` (Task 3, before any DB read)."""
    return MultiSourcePlannerIntentV1(
        target_entity="customer",
        operands=(_operand(slot_id="op_num", semantic_role=SemanticRole.numerator),),
        final_expression=FinalExpressionV1(
            operation=FinalOperation.ratio, ordered_slot_ids=("op_num",),
            time_slot_id=None, window=None, output_additivity=AdditivityClass.non_additive),
        operation_policy_version=OPERATION_POLICY_VERSION)


# ── topology fixtures ────────────────────────────────────────────────────────────────────────────
def _seed_bridged_topology(db):
    """core_banking.transactions -> VERIFIED bridge at account -> intra-wealth realization ->
    wealth.customers. Returns nothing; callers add the grain fact + watermarks."""
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


def _seed_all_hop_grains(db, service_actor, human_actor):
    """VERIFIED grain on EVERY hop endpoint of the bridged topology (spec §2/§3.2): the source
    transactions, the intermediate wealth.accounts, and the landing wealth.customers."""
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)


@pytest.fixture
def resolved_topology(db, service_actor, human_actor):
    """The bridged topology WITH a VERIFIED grain fact on EVERY hop endpoint (source + intermediate +
    landing) and FRESH drift watermarks on both participating catalogs — so the whole pipeline
    resolves (both axes)."""
    _seed_bridged_topology(db)
    _seed_all_hop_grains(db, service_actor, human_actor)
    _watermark(db, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(db, "wealth", _NOW - timedelta(minutes=5))
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def stale_landing_topology(db, service_actor, human_actor):
    """The resolved topology but the LANDING catalog's drift watermark is STALE (6h >> SLA). Assembly
    still resolves (staleness bites only the compile-end UNION freshness), so the plan compiles with
    ``resolution_status=resolved`` but ``contract_result_status=unresolved_freshness``."""
    _seed_bridged_topology(db)
    _seed_all_hop_grains(db, service_actor, human_actor)
    _watermark(db, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(db, "wealth", _NOW - timedelta(hours=6))
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def ungoverned_endpoint_topology(db, service_actor, human_actor):
    """The bridged topology with the source + intermediate governed but NO grain fact on the
    wealth.customers landing — a governed path resolves, its source binding is governed, but the
    LANDING endpoint is not proven by a VERIFIED grain fact -> ``realization_endpoint_ungoverned``."""
    _seed_bridged_topology(db)
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    # deliberately NO grain fact on wealth.customers (the landing)
    _watermark(db, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(db, "wealth", _NOW - timedelta(minutes=5))
    return db, _scope("core_banking", "wealth")


# ── tests ──────────────────────────────────────────────────────────────────────────────────────
def test_valid_ratio_resolves_one_selected_candidate(resolved_topology):
    conn, scope = resolved_topology
    result = plan_multi_source(conn, _adapter(), intent=_ratio_intent(), scope=scope,
                               roles=("feature_engineer",), now=_NOW)

    # assembly axis resolved, exactly one compiled candidate, and it is the selection
    assert result.result_status is MultiSourceReason.resolved
    assert len(result.candidate_plans) == 1
    assert result.selected_plan_id == result.candidate_plans[0].plan_id
    assert result.selected_plan_id is not None

    # BOTH operand slots survive on the selected plan, exactly once, with their intent roles
    plan = result.candidate_plans[0]
    assert {p.slot_id for p in plan.operand_paths} == {"op_num", "op_den"}
    assert {p.slot_id: p.semantic_role for p in plan.operand_paths} == {
        "op_num": SemanticRole.numerator, "op_den": SemanticRole.denominator}

    # the contract axis resolved too -> a genuinely-resolved contract selection
    assert result.contract_result_status is ContractResolutionStatus.resolved
    assert result.selected_contract_plan_id == plan.plan_id
    assert result.selected_contract_id == plan.contract_id
    assert result.selected_contract_id is not None

    # the replay envelope is keyed on FACT_KEYS (governed endpoint grain fact_keys + bridge fact_keys)
    env = result.replay_envelope
    assert isinstance(env, MultiSourceReplayEnvelopeV1)
    assert "bfk_acct" in env.bridge_fact_keys
    assert env.governed_endpoint_fact_keys           # the landing endpoint's grain fact_key
    assert env.input_hash


def test_replay_envelope_is_deterministic_across_runs(resolved_topology):
    """The envelope keys on stable fact_keys (no run_id/timestamp/event-id), so a double run over the
    same seeded topology fingerprints IDENTICALLY."""
    conn, scope = resolved_topology
    r1 = plan_multi_source(conn, _adapter(), intent=_ratio_intent(), scope=scope,
                           roles=("feature_engineer",), now=_NOW)
    r2 = plan_multi_source(conn, _adapter(), intent=_ratio_intent(), scope=scope,
                           roles=("feature_engineer",), now=_NOW)
    assert r1.replay_envelope.input_hash == r2.replay_envelope.input_hash
    assert r1.selected_plan_id == r2.selected_plan_id
    assert r1.selected_contract_id == r2.selected_contract_id


def test_shape_invalid_intent_returns_operand_shape_invalid_no_candidates(db):
    ensure_upload_catalog_adapter()
    result = plan_multi_source(db, _adapter(), intent=_shape_invalid_intent(),
                               scope=_scope("core_banking", "wealth"),
                               roles=("feature_engineer",), now=_NOW)
    assert result.result_status is MultiSourceReason.operand_shape_invalid
    assert result.primary_reason_code is MultiSourceReason.operand_shape_invalid
    assert result.candidate_plans == ()
    assert result.selected_plan_id is None
    assert result.contract_result_status is ContractResolutionStatus.not_compiled


def test_ungoverned_landing_endpoint_is_realization_endpoint_ungoverned(ungoverned_endpoint_topology):
    conn, scope = ungoverned_endpoint_topology
    result = plan_multi_source(conn, _adapter(), intent=_ratio_intent(), scope=scope,
                               roles=("feature_engineer",), now=_NOW)
    # a governed path resolves, but the wealth.customers landing has no VERIFIED grain fact
    assert result.result_status is MultiSourceReason.realization_endpoint_ungoverned
    assert result.candidate_plans == ()
    assert result.selected_plan_id is None


def test_contract_axis_gate_stale_union_is_not_a_resolved_selection(stale_landing_topology):
    """THE TWO-AXIS RESOLVE GATE. The plan compiles to ``resolution_status=resolved`` (the assembly axis
    is sound) but ``contract_result_status=unresolved_freshness`` (the landing catalog is stale). The run
    must NOT present it as a resolved CONTRACT: ``selected_contract_id``/``selected_contract_plan_id``
    are withheld, and the non-resolution surfaces on the contract axis. Keying the contract selection on
    ``resolution_status`` alone would be a fail-open — this test would fail under that bug."""
    conn, scope = stale_landing_topology
    result = plan_multi_source(conn, _adapter(), intent=_ratio_intent(), scope=scope,
                               roles=("feature_engineer",), now=_NOW)

    # the compiled candidate: assembly axis resolved, contract axis NOT (freshness observation failed)
    assert len(result.candidate_plans) == 1
    compiled = result.candidate_plans[0]
    assert compiled.resolution_status is MultiSourceReason.resolved
    assert compiled.contract_result_status is ContractResolutionStatus.unresolved_freshness

    # the run surfaces the assembly resolution but WITHHOLDS the resolved-contract selection (the gate)
    assert result.result_status is MultiSourceReason.resolved
    assert result.contract_result_status is ContractResolutionStatus.unresolved_freshness
    assert result.contract_result_status is not ContractResolutionStatus.resolved
    assert result.selected_contract_id is None
    assert result.selected_contract_plan_id is None


def test_explicit_budget_is_decremented_per_compile(resolved_topology):
    conn, scope = resolved_topology
    budget = CompileBudget(remaining=5, deadline_monotonic=float("inf"), clock=time.monotonic)
    result = plan_multi_source(conn, _adapter(), intent=_ratio_intent(), scope=scope,
                               roles=("feature_engineer",), now=_NOW, budget=budget)
    assert result.result_status is MultiSourceReason.resolved
    assert budget.remaining == 4        # exactly one compile happened


# ── a raised DB error propagates (the harness classifies it technical — never swallowed here) ─────
class _Boom(Exception):
    pass


class _BoomConn:
    """Any DB access raises — proving ``plan_multi_source`` has NO blanket try/except that would
    convert a raised DB error into a ``technical_failure`` status (the harness must classify it)."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise _Boom(f"db down: {name}")
        return _raise


def test_raised_db_error_propagates_not_swallowed():
    # the shape is valid, so control reaches the first DB read (context build) — which booms and must
    # propagate out of plan_multi_source untouched.
    with pytest.raises(_Boom):
        plan_multi_source(_BoomConn(), _adapter(), intent=_ratio_intent(),
                          scope=_scope("core_banking", "wealth"),
                          roles=("feature_engineer",), now=_NOW)

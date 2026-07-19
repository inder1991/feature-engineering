"""Phase 3C.2b-i-A · Task 1 — SPIKE: prove the reuse premise against the REAL frontier.

The whole phase rests on ONE premise: an INJECTED single-need ``Template`` + a hand-built
``_Position`` can be driven through the EXISTING cross-catalog frontier
(``semantic_rollup_paths`` -> ``assemble_paths``) to a RESOLVED cross-catalog ``BindingPlanV1``,
and then through the EXISTING per-path compiler (``check_connectivity`` -> ``compile_temporal`` ->
``compile_aggregation``) using A's OWN ``CompilerContext`` (``agg_declarations`` POPULATED, not the
empty registry ``build_compiler_context`` returns) to yield a ``HopAggregationV1``. If any link
cannot be made to pass against the real code, the phase's reuse model is wrong.

The fixtures are seeded through the real paths: ``build_graph`` (the ingest graph builder) for two
catalogs, a VERIFIED ``entity_bridge`` in ``entity_bridge_edge`` (the projection ``active_bridges``
reads — seeded the way the whole assembly suite seeds bridges: ``tests/.../test_assembly.py``), and
a VERIFIED ``grain`` fact on the landing table via the real ``propose_fact``/``confirm_fact``
governance write path. The ``feature_engineer`` role + untagged columns keep every operand/anchor
column in read-scope.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload.conftest import _confirm_grain

from featuregen.contracts.envelopes import Command
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.assembly import _Position
from featuregen.overlay.upload.planner.contracts import (
    AggregationFunction,
    AggregationValidation,
    BindingQuality,
    BindingSafety,
    CatalogScopeV1,
    IngredientBindingV1,
    PlanResolutionStatus,
)
from featuregen.overlay.upload.planner.declarations import (
    check_connectivity,
    compile_aggregation,
    compile_temporal,
)
from featuregen.overlay.upload.planner.multisource_reuse import (
    build_operand_context,
    injected_operand_template,
    run_operand_rollup,
)
from featuregen.overlay.upload.upload_catalog import (
    ensure_upload_catalog_adapter,
    table_ref,
)

_NOW = datetime(2026, 7, 19, tzinfo=UTC)


# ── seed helpers (the sanctioned assembly-suite pattern) ──────────────────────────────────────
def _seed(db, source, rows_concepts):
    """Seed a catalog's physical graph through the REAL ingest graph builder — concept tags carry
    the entity links the frontier's grain/key derivation reads (never hand-set columns)."""
    rows = [r for r, _ in rows_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _seed_verified_bridge(db, fact_key, entity_id, lc, lref, rc, rref):
    """A VERIFIED cross-catalog bridge in the projection ``active_bridges`` reads. This is exactly
    how the whole assembly test suite seeds a governed crossing (``entity_bridge_edge`` IS the
    projection ``project_verified_bridge`` writes)."""
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fact_key, entity_id, lc, lref, rc, rref))


def _seed_verified_grain(db, source, table, columns, *, service_actor, human_actor):
    """A VERIFIED ``grain`` fact on the landing table via the REAL governance write path:
    ``propose_fact`` (service) opens the platform-admin gate task, ``_confirm_grain`` (human)
    confirms + drains the projection so ``resolve_fact`` reads VERIFIED."""
    ref = table_ref(source, table)
    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain",
         "proposed_value": {"columns": columns, "is_unique": True}},
        service_actor, f"propose-grain-{source}-{table}"))
    assert res.accepted, res.denied_reason
    _confirm_grain(db, source, table, columns, actor=human_actor)


def _scope(*catalogs):
    return CatalogScopeV1(
        scope_id="ms-spike", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-19T00:00:00Z",
        catalog_consideration_truncated=False)


def _agg_decls_for(recipe_id, need_role, func):
    return {(recipe_id, need_role): AggregationFunction(func)}


def _binding(recipe_id, need_role, catalog, object_ref, *, concept, join_role, temporal_role=""):
    return IngredientBindingV1(
        recipe_id=recipe_id, need_role=need_role, concept=concept, required_grains=(),
        join_role=join_role, temporal_role=temporal_role, bound_catalog_source=catalog,
        bound_object_ref=object_ref, actual_source_grain=None,
        binding_quality=BindingQuality.grain_and_role_fit, safety=BindingSafety.safe,
        reason_codes=())


def _binding_for(need_role, catalog, object_ref):
    return (_binding("ms:op_0", need_role, catalog, object_ref,
                     concept="monetary_flow", join_role="measure"),)


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def two_catalog_bridged_fixture(db, service_actor, human_actor):
    """core_banking.transactions (transaction grain, a monetary_flow measure) reaches entity
    ``customer`` ONLY by CROSSING to wealth via a VERIFIED ``entity_bridge`` at ``account``, then an
    intra-wealth realization ``account -> customer``. Landing = wealth.customers (VERIFIED grain)."""
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
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    return db, _scope("core_banking", "wealth"), _NOW


@pytest.fixture
def two_catalog_take_latest_fixture(db, service_actor, human_actor):
    """A take_latest operand: core_banking.transactions carries a semi_additive monetary_stock +
    an as_of_date anchor. hop0 (transaction->account) realizes INTRA core_banking (the fan-in where
    the stock is aggregated, with the anchor on the many-side rows before it), then a same-entity
    REPOSITION bridge crosses ``account`` to wealth, and hop1 (account->customer) realizes intra
    wealth. Landing = wealth.customers (VERIFIED grain)."""
    ensure_upload_catalog_adapter()
    _seed(db, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core_banking", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core_banking", "transactions", "balance", "numeric"), "monetary_stock"),
        (CanonicalRow("core_banking", "transactions", "as_of", "date", as_of=True), "as_of_date"),
        (CanonicalRow("core_banking", "accounts", "account_id", "integer", is_grain=True),
         "account_id"),
    ])
    _seed(db, "wealth", [
        (CanonicalRow("wealth", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("wealth", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("wealth", "customers", "customer_id", "integer", is_grain=True),
         "customer_id"),
    ])
    # same-entity reposition crossing at 'account': core_banking.accounts <-> wealth.accounts
    _seed_verified_bridge(db, "bfk_rep_acct", "account",
                          "core_banking", "public.accounts.account_id",
                          "wealth", "public.accounts.account_id")
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    return db, _scope("core_banking", "wealth"), _NOW


# ── the SPIKE ─────────────────────────────────────────────────────────────────────────────────
def test_injected_operand_template_rolls_up_and_compiles(two_catalog_bridged_fixture):
    conn, scope, now = two_catalog_bridged_fixture
    tmpl = injected_operand_template(recipe_id="ms:op_0", need_role="measure_0",
                                     concept="monetary_flow", source_entity="transaction")
    ctx = build_operand_context(conn, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=now,
                                agg_declarations=_agg_decls_for("ms:op_0", "measure_0", "sum"))
    plan = run_operand_rollup(
        conn, ctx,
        source_position=_Position("transaction", "core_banking", "public.transactions"),
        target_entity="customer", template=tmpl, scope=scope,
        ingredient_bindings=_binding_for("measure_0", "core_banking", "public.transactions.amount"))
    assert plan is not None
    assert plan.resolution_status is PlanResolutionStatus.resolved
    # the plan crosses catalogs via the VERIFIED bridge (a real cross-catalog roll-up, not intra)
    assert plan.participating_catalogs == ("core_banking", "wealth")

    conn_res = check_connectivity(ctx, plan)
    assert conn_res.connected
    temporal = compile_temporal(ctx, plan, tmpl)
    hops = compile_aggregation(ctx, plan, tmpl, temporal, conn_res.placement)
    assert hops  # a fan-in hop produced an aggregation declaration
    stages = [s for h in hops for s in h.ingredient_stages]
    measure_stages = [s for s in stages if s.need_role == "measure_0"]
    assert measure_stages  # the injected measure was staged at a fan-in hop
    # A's OWN populated agg_declarations were consulted (not the empty production registry)
    assert measure_stages[0].declared_function is AggregationFunction.sum
    assert measure_stages[0].validation is AggregationValidation.sound


def test_injected_take_latest_operand_finds_anchor_and_validates(two_catalog_take_latest_fixture):
    conn, scope, now = two_catalog_take_latest_fixture
    tmpl = injected_operand_template(recipe_id="ms:op_1", need_role="stock_0",
                                     concept="monetary_stock", source_entity="transaction",
                                     anchor_concept="as_of_date")
    ctx = build_operand_context(conn, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=now,
                                agg_declarations=_agg_decls_for("ms:op_1", "stock_0", "take_latest"))
    bindings = (
        _binding("ms:op_1", "stock_0", "core_banking", "public.transactions.balance",
                 concept="monetary_stock", join_role="measure"),
        _binding("ms:op_1", "stock_0_anchor", "core_banking", "public.transactions.as_of",
                 concept="as_of_date", join_role="time", temporal_role="as_of_time"),
    )
    plan = run_operand_rollup(
        conn, ctx,
        source_position=_Position("transaction", "core_banking", "public.transactions"),
        target_entity="customer", template=tmpl, scope=scope, ingredient_bindings=bindings)
    assert plan is not None
    assert plan.resolution_status is PlanResolutionStatus.resolved

    conn_res = check_connectivity(ctx, plan)
    temporal = compile_temporal(ctx, plan, tmpl)
    # the injected second need made compile_temporal FIND the anchor (F17: injected template)
    assert temporal.pit_anchor == "as_of_time"
    assert temporal.anchor_binding == "public.transactions.as_of"

    hops = compile_aggregation(ctx, plan, tmpl, temporal, conn_res.placement)
    stages = [s for h in hops for s in h.ingredient_stages if s.need_role == "stock_0"]
    assert stages, "the take_latest measure was staged at a fan-in hop"
    # _take_latest validation PASSED: the anchor was proven at row grain before the fan-in hop
    assert stages[0].declared_function is AggregationFunction.take_latest
    assert stages[0].validation is AggregationValidation.sound


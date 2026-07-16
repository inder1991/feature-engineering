"""Phase-3B.3c C2 — CompilerContext (immutable, conn-free) + ingredient connectivity with placement.

The context is constructed DIRECTLY here from the real loaders (derive_catalog_realizations /
_load_columns / active_bridges) — the per-run batching builder is C8. check_connectivity is pure
over the context: no test hands it a connection."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from featuregen.overlay.config import OverlayConfig
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.bridge_projection import active_bridges
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import (
    derive_catalog_realizations,
    realization_fingerprint,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.declarations import (
    CompileBudget,
    CompilerContext,
    PathPositionV1,
    check_connectivity,
)
from featuregen.overlay.upload.templates import _load_columns

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _overlay_config() -> OverlayConfig:
    return OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=True)


def _seed(db, source, rows_and_concepts):
    rows = [r for r, _ in rows_and_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): cn for r, cn in rows_and_concepts})


def _txn_core(db):
    """core: transactions + card_swipes are BOTH transaction-grain; only transactions joins up to
    accounts (account grain) — the transaction_to_account roll-up the path realizes."""
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "transactions", "amount", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "card_swipes", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "card_swipes", "fee_amount", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
    ])


def _ctx(db, *catalogs: str) -> CompilerContext:
    """The C2 test constructor — batch-loads via the REAL loaders, then drops the connection.
    (The per-run production builder `build_compiler_context` is C8.)"""
    return CompilerContext(
        realizations_by_catalog={
            s: derive_catalog_realizations(db, s).realizations for s in catalogs},
        active_bridges=active_bridges(db),
        columns_by_catalog={
            s: {col.object_ref: col for col in _load_columns(db, s, ())} for s in catalogs},
        catalog_fingerprint_at_start={s: realization_fingerprint(db, s) for s in catalogs},
        bridge_fingerprint_at_start="",
        catalog_stamps={
            s: c.CatalogStateStampV1(s, 0, _NOW.isoformat()) for s in catalogs},
        config=_overlay_config(),
        roles=(),
        now=_NOW,
        agg_declarations={})


def _binding(role, obj_ref, *, join_role=str(JoinRole.MEASURE), catalog="core"):
    return c.IngredientBindingV1(
        recipe_id="r1", need_role=role, concept="monetary_flow", required_grains=(),
        join_role=join_role, temporal_role=str(TemporalRole.NONE),
        bound_catalog_source=catalog, bound_object_ref=obj_ref, actual_source_grain=None,
        binding_quality=c.BindingQuality.exact_concept, safety=c.BindingSafety.safe,
        reason_codes=())


def _plan(bindings, segments):
    return c.make_binding_plan(
        recipe_id="r1", target_entity="account", catalog_source="core",
        ingredient_bindings=tuple(bindings), path_segments=tuple(segments),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.unranked)


def _rollup_segments(realization_id):
    """The assembler's per-hop emission shape: semantic_rollup (no refs) + the realizer."""
    return (
        c.BindingPathSegmentV1(c.SegmentKind.semantic_rollup, "core",
                               from_entity="transaction", to_entity="account",
                               cardinality="many_to_one"),
        c.BindingPathSegmentV1(c.SegmentKind.intra_catalog_realization, "core",
                               realization_ref=realization_id),
    )


def test_ingredient_on_off_path_transaction_table_is_disconnected(db):
    # two ingredients on DIFFERENT transaction-grain tables; the path rolls up only transactions,
    # so the card_swipes-bound role is honestly disconnected — never silently joined.
    _txn_core(db)
    ctx = _ctx(db, "core")
    (r,) = ctx.realizations_by_catalog["core"]
    plan = _plan(
        bindings=(
            _binding("source_key", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY)),
            _binding("amount", "public.transactions.amount"),
            _binding("fee", "public.card_swipes.fee_amount"),   # transaction-grain, NOT on the path
        ),
        segments=_rollup_segments(r.realization_id))
    out = check_connectivity(ctx, plan)
    assert out.connected is False
    assert out.disconnected_roles == ("fee",)
    assert "amount" in out.placement and "fee" not in out.placement


def test_all_ingredients_on_path_are_connected_with_placement(db):
    _txn_core(db)
    ctx = _ctx(db, "core")
    (r,) = ctx.realizations_by_catalog["core"]
    plan = _plan(
        bindings=(
            _binding("source_key", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY)),
            _binding("amount", "public.transactions.amount"),   # co-located with the source key
            _binding("balance", "public.accounts.balance"),     # the roll-up hop's to-table
        ),
        segments=_rollup_segments(r.realization_id))
    out = check_connectivity(ctx, plan)
    assert out.connected is True and out.disconnected_roles == ()
    # the source-key table is the pre-first-hop position (segment_index 0); the realizer segment
    # sits at path_segments[1] and its to-table holds `balance`.
    assert out.placement["source_key"] == PathPositionV1(0, "core", "public.transactions")
    assert out.placement["amount"] == PathPositionV1(0, "core", "public.transactions")
    assert out.placement["balance"] == PathPositionV1(1, "core", "public.accounts")


def test_bridge_endpoint_tables_count_as_path_tables(db):
    # core.customer_master <-> crm.customers bridged at `customer` (VERIFIED projected edge, as
    # bridge_projection writes it); an ingredient on the FAR endpoint table is connected and
    # placed at the governed_bridge segment.
    _seed(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ])
    _seed(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("crm", "customers", "churn_flag", "text"), "categorical"),
    ])
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref,"
        " right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        ("bridge:customer:c2", "customer", "core", "public.customer_master.customer_id",
         "crm", "public.customers.customer_id"))
    ctx = _ctx(db, "core", "crm")
    assert len(ctx.active_bridges) == 1     # loaded through the real active_bridges reader
    plan = _plan(
        bindings=(
            _binding("source_key", "public.customer_master.customer_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY)),
            _binding("crm_flag", "public.customers.churn_flag", catalog="crm"),
        ),
        segments=(
            c.BindingPathSegmentV1(c.SegmentKind.semantic_rollup, "core",
                                   from_entity="customer", to_entity="customer"),
            c.BindingPathSegmentV1(c.SegmentKind.governed_bridge, "crm",
                                   from_entity="customer", to_entity="customer",
                                   bridge_fact_key="bridge:customer:c2"),
        ))
    out = check_connectivity(ctx, plan)
    assert out.connected is True and out.disconnected_roles == ()
    assert out.placement["crm_flag"] == PathPositionV1(1, "crm", "public.customers")
    # the NEAR endpoint is the source-key table — the pre-first-hop position wins over the
    # bridge segment that also touches it.
    assert out.placement["source_key"] == PathPositionV1(0, "core", "public.customer_master")


def test_unresolvable_segment_refs_fail_closed(db):
    # a segment whose realization/bridge ref is unknown to the context contributes NO tables —
    # ingredients relying on it report disconnected, never silently pass.
    _txn_core(db)
    ctx = _ctx(db, "core")
    plan = _plan(
        bindings=(
            _binding("source_key", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY)),
            _binding("balance", "public.accounts.balance"),
        ),
        segments=_rollup_segments("core:not.a.real->realization"))
    out = check_connectivity(ctx, plan)
    assert out.connected is False and out.disconnected_roles == ("balance",)


def test_compiler_context_is_genuinely_immutable(db):
    _txn_core(db)
    ctx = _ctx(db, "core")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.roles = ("x",)  # type: ignore[misc]
    with pytest.raises(TypeError):
        ctx.realizations_by_catalog["evil"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.columns_by_catalog["core"]["evil"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.agg_declarations[("r1", "amount")] = c.AggregationFunction.sum  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.catalog_fingerprint_at_start["core"] = "tampered"  # type: ignore[index]


def test_compile_budget_is_plain_mutable():
    # run-owned (C8): decrement + deadline comparison must work on a plain dataclass
    b = CompileBudget(remaining=2, deadline=_NOW)
    b.remaining -= 1
    assert b.remaining == 1 and b.deadline == _NOW

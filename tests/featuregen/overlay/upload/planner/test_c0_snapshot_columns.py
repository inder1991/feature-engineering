"""Delivery H3b — the C0-snapshot column adapter for the planner (Risk-4: physical_plan_id MUST stay
byte-identical).

The planner's candidate discovery is re-sourced from a FROZEN ``_load_columns`` capture instead of a
live ``graph_node`` read on the feature-generation path. These tests PIN that:
  * the switch is byte-NEUTRAL on plan identity — the HASH-STABILITY gate: every ``physical_plan_id``,
    ``contract_id`` and selected id produced via the C0 snapshot equals the LIVE-``_load_columns`` result
    over the SAME ``graph_node`` state (single-catalog, multi-column, and a grain/as_of case);
  * the adapter reproduces the ``_load_columns`` column set + attributes exactly;
  * the capture is FROZEN — a ``graph_node`` mutation after capture never leaks into plan discovery;
  * a caller with no snapshot keeps the live read unchanged.
If a hash-stability assertion ever fails, the adapter has diverged from ``_load_columns`` — FIX the
adapter, never the expected ids.
"""
import time
from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_metadata_snapshot import (
    ColumnSnapshot,
    capture_column_snapshot,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import PlanResolutionStatus
from featuregen.overlay.upload.planner.declarations import (
    CompileBudget,
    build_compiler_context,
)
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.planner.shadow import COMPILE_BUDGET, MAX_COMPILES_PER_RUN
from featuregen.overlay.upload.templates import Need, Template, _load_columns

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────────
def _watermark(db, source):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES (%s, %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, _NOW, _NOW))


def _catalog(db, source, table="accounts"):
    """A customer-grain catalog: grain column + a semi-additive monetary measure."""
    catalog = [
        (CanonicalRow(source, table, "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(source, table, "balance", "numeric",
                      additivity="semi_additive", currency="USD"), "monetary_stock")]
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    _watermark(db, source)


def _catalog_with_as_of(db, source):
    """A grain/as_of/measure catalog — exercises the is_grain + is_as_of column-attribute paths."""
    catalog = [
        (CanonicalRow(source, "positions", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(source, "positions", "mkt_value", "numeric",
                      additivity="semi_additive", currency="USD"), "monetary_stock"),
        (CanonicalRow(source, "positions", "as_of_dt", "date", as_of=True), "as_of_date")]
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    _watermark(db, source)


def _tmpl(stock_grains: tuple[str, ...] = ()):
    return Template(id="t_bal", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock",
                                allowed_source_grains=stock_grains),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M",
                    use_cases=(), pit="trailing")


def _tmpl_as_of():
    return Template(id="t_asof", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock"),
                           Need(role="asof", concept="as_of_date"),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M",
                    use_cases=(), pit="trailing")


def _fresh_budget():
    return CompileBudget(remaining=MAX_COMPILES_PER_RUN,
                         deadline_monotonic=time.monotonic() + COMPILE_BUDGET.total_seconds(),
                         clock=time.monotonic)


def _run(db, tmpl, scope, *, column_source):
    """Run the FULL planner with the compile pass ON (so contract_ids are minted), sourcing columns
    either LIVE (column_source=None) or from the C0 snapshot adapter."""
    ctx = build_compiler_context(db, scope, (), _NOW, column_source=column_source)
    return plan_bindings(db, template=tmpl, target_entity="customer", scope=scope,
                         roles=(), now=_NOW, compile_ctx=ctx, budget=_fresh_budget())


def _identity(result):
    """The plan-identity surface the hash must pin: every candidate's physical_plan_id + contract_id,
    plus the result-level selected ids. Sorted so column ORDER (irrelevant to the sorted refs) can
    never make two equal runs compare unequal."""
    return {
        "physical_plan_ids": tuple(sorted(p.physical_plan_id for p in result.candidate_plans)),
        "physical_and_contract": tuple(sorted(
            (p.physical_plan_id, p.contract_id) for p in result.candidate_plans)),
        "selected_plan_id": result.selected_plan_id,
        "selected_contract_physical_plan_id": result.selected_contract_physical_plan_id,
        "selected_contract_id": result.selected_contract_id,
    }


# ── the HASH-STABILITY gate ───────────────────────────────────────────────────────────────────────────
def test_hash_stability_single_catalog_multi_column(db):
    """THE gate: a multi-column single-catalog recipe yields byte-identical physical_plan_ids +
    contract_ids whether the planner reads columns LIVE or from the C0 snapshot adapter."""
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)

    live = _run(db, _tmpl(), scope, column_source=None)
    snap_src = capture_column_snapshot(db, scope.authorized_catalog_sources, ())
    snap = _run(db, _tmpl(), scope, column_source=snap_src)

    # Not a vacuous pass: the plan actually resolved over >1 bound column.
    assert live.result_status is PlanResolutionStatus.resolved
    assert live.selected_plan_id is not None
    sel = next(p for p in live.candidate_plans if p.physical_plan_id == live.selected_plan_id)
    assert len(sel.ingredient_bindings) >= 2
    # BYTE-IDENTICAL plan identity, live vs snapshot.
    assert _identity(live) == _identity(snap)


def test_hash_stability_grain_and_as_of_case(db):
    """The column-attribute-sensitive path: a grain + as_of + measure recipe. is_grain / is_as_of drive
    the bindings, so a snapshot that dropped or altered either attribute would move an id — it must
    not."""
    _catalog_with_as_of(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)

    live = _run(db, _tmpl_as_of(), scope, column_source=None)
    snap_src = capture_column_snapshot(db, scope.authorized_catalog_sources, ())
    snap = _run(db, _tmpl_as_of(), scope, column_source=snap_src)

    assert live.result_status is PlanResolutionStatus.resolved
    sel = next(p for p in live.candidate_plans if p.physical_plan_id == live.selected_plan_id)
    bound = {b.bound_object_ref for b in sel.ingredient_bindings}
    assert "public.positions.as_of_dt" in bound and "public.positions.customer_id" in bound
    assert _identity(live) == _identity(snap)


def test_hash_stability_multi_catalog(db):
    """Two catalogs in scope (grain-constrained): both catalogs' frozen column sets feed candidate
    discovery. Every candidate plan across both catalogs keeps its live physical_plan_id/contract_id."""
    _catalog(db, "core")
    _catalog(db, "wm", table="holdings")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert set(scope.authorized_catalog_sources) == {"core", "wm"}

    live = _run(db, _tmpl(stock_grains=("customer",)), scope, column_source=None)
    snap_src = capture_column_snapshot(db, scope.authorized_catalog_sources, ())
    snap = _run(db, _tmpl(stock_grains=("customer",)), scope, column_source=snap_src)

    assert live.result_status is PlanResolutionStatus.resolved
    assert len(_identity(live)["physical_plan_ids"]) >= 2   # candidate plans from both catalogs
    assert _identity(live) == _identity(snap)


# ── adapter fidelity ──────────────────────────────────────────────────────────────────────────────────
def test_adapter_reproduces_load_columns_exactly(db):
    """The adapter returns the SAME ``_Col`` set + attributes as live ``_load_columns`` for the same
    state (order-normalized direct equality on the frozen dataclass rows)."""
    _catalog_with_as_of(db, "core")
    live_cols = _load_columns(db, "core", ())
    snap = capture_column_snapshot(db, ["core"], ())

    assert sorted(snap.columns("core", ()), key=lambda c: c.object_ref) == \
        sorted(live_cols, key=lambda c: c.object_ref)
    # attribute-level spot check: the binding-relevant fields survive the capture verbatim.
    by_ref = {c.object_ref: c for c in snap.columns("core", ())}
    grain = by_ref["public.positions.customer_id"]
    asof = by_ref["public.positions.as_of_dt"]
    stock = by_ref["public.positions.mkt_value"]
    assert grain.is_grain and grain.concept == "customer_id"
    assert asof.is_as_of and asof.concept == "as_of_date"
    assert stock.concept == "monetary_stock" and stock.additivity == "semi_additive"


def test_build_compiler_context_snapshot_matches_live_columns(db):
    """``build_compiler_context`` fills ``columns_by_catalog`` byte-identically whether sourced live or
    from the snapshot — the exact map candidate discovery consumes."""
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    snap = capture_column_snapshot(db, scope.authorized_catalog_sources, ())

    live_ctx = build_compiler_context(db, scope, (), _NOW)
    snap_ctx = build_compiler_context(db, scope, (), _NOW, column_source=snap)
    assert dict(live_ctx.columns_by_catalog["core"]) == dict(snap_ctx.columns_by_catalog["core"])


# ── freshness / additivity / fail-closed ──────────────────────────────────────────────────────────────
def test_non_snapshot_caller_uses_live_load_columns(db):
    """No ``column_source`` => the live ``_load_columns`` read is unchanged (gold/shadow/direct callers).
    A mutation is reflected immediately, proving the default path never froze anything."""
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    before = build_compiler_context(db, scope, (), _NOW)
    assert "public.accounts.balance" in before.columns_by_catalog["core"]

    _catalog_with_as_of(db, "core")   # live graph_node mutation (rebuild): now positions.*, not accounts.*
    scope2 = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    after = build_compiler_context(db, scope2, (), _NOW)
    assert "public.positions.as_of_dt" in after.columns_by_catalog["core"]     # live sees the new column
    assert "public.accounts.balance" not in after.columns_by_catalog["core"]   # and drops the old one


def test_snapshot_is_frozen_against_later_mutation(db):
    """The capture is IMMUTABLE: a ``graph_node`` mutation AFTER capture is invisible to the snapshot
    (proving the planner reads the snapshot, not a live query), while a live read sees it."""
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    snap = capture_column_snapshot(db, ["core"], ())
    frozen_refs = {c.object_ref for c in snap.columns("core", ())}
    assert frozen_refs == {"public.accounts.customer_id", "public.accounts.balance"}

    _catalog_with_as_of(db, "core")   # rebuild core's graph AFTER the capture
    live_refs = {c.object_ref for c in _load_columns(db, "core", ())}
    assert "public.positions.as_of_dt" in live_refs                 # live moved on
    assert snap.columns("core", ()) and \
        {c.object_ref for c in snap.columns("core", ())} == frozen_refs   # snapshot did NOT

    # …and the frozen source flows through build_compiler_context unchanged.
    ctx = build_compiler_context(db, scope, (), _NOW, column_source=snap)
    assert set(ctx.columns_by_catalog["core"]) == frozen_refs


def test_column_snapshot_fails_closed_on_roles_mismatch(db):
    """Read scope is baked in at capture: serving a DIFFERENT ``roles`` set (a different sensitivity
    filter) would be a leak / hash divergence, so the adapter refuses it."""
    _catalog(db, "core")
    snap = capture_column_snapshot(db, ["core"], ())         # captured for roles=()
    assert isinstance(snap, ColumnSnapshot)
    with pytest.raises(ValueError, match="read scope must match"):
        snap.columns("core", ("pii_reader",))

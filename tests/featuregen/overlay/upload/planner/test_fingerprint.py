from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from featuregen.overlay.config import OverlayConfig
from featuregen.overlay.upload.bridge_projection import active_bridges
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import (
    derive_catalog_realizations,
    realization_fingerprint,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.declarations import CompilerContext, bridge_fingerprint
from featuregen.overlay.upload.planner.fingerprint import (
    compiler_input_fingerprint,
    contract_input_hash,
    declarations_output_hash,
    planner_input_hash,
)
from featuregen.overlay.upload.templates import Need, Template, _load_columns

_NOW = datetime(2026, 7, 17, tzinfo=UTC)
_CFG = OverlayConfig(
    ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
    ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
    drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
    profiler_require_restricted_role=True)


def _seed_core(db):
    rows = [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True), "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "transactions", "amount", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
    ]
    build_graph(db, "core", [r for r, _ in rows], concepts={content_hash(r): cn for r, cn in rows})


def _ctx(db, roles=(), agg=None, columns_override=None) -> CompilerContext:
    cols = columns_override if columns_override is not None else {
        "core": {col.object_ref: col for col in _load_columns(db, "core", roles)}}
    return CompilerContext(
        realizations_by_catalog={"core": derive_catalog_realizations(db, "core").realizations},
        active_bridges=active_bridges(db),
        columns_by_catalog=cols,
        catalog_fingerprint_at_start={"core": realization_fingerprint(db, "core")},
        bridge_fingerprint_at_start=bridge_fingerprint(db),
        catalog_stamps={},
        config=_CFG, roles=tuple(roles), now=_NOW,
        agg_declarations=dict(agg or {}))


class _Scope:
    authorized_catalog_sources = ("core",)


def _template() -> Template:
    return Template(id="fp_t", family="fam", intent="intent",
                    needs=(Need(role="amt", concept="monetary_flow"),),
                    params={"window": (90, 60, 30)}, aggregation="sum", additivity="additive",
                    explain="M", use_cases=(), pit="trailing")


def _plan(catalog="core", ref="public.transactions.amount") -> c.BindingPlanV1:
    b = c.IngredientBindingV1(
        recipe_id="fp_t", need_role="amt", concept="monetary_flow", required_grains=(),
        join_role="measure", temporal_role="none", bound_catalog_source=catalog, bound_object_ref=ref,
        actual_source_grain="transaction", binding_quality=c.BindingQuality.exact_concept,
        safety=c.BindingSafety.safe, reason_codes=())
    return c.make_binding_plan(
        recipe_id="fp_t", target_entity="account", catalog_source=catalog, ingredient_bindings=(b,),
        path_segments=(c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, catalog),),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)


def test_all_hashes_deterministic(db):
    _seed_core(db)
    ctx, tmpl, plan = _ctx(db), _template(), _plan()
    assert compiler_input_fingerprint(ctx, "core") == compiler_input_fingerprint(ctx, "core")
    assert planner_input_hash(ctx, tmpl, _Scope()) == planner_input_hash(ctx, tmpl, _Scope())
    assert contract_input_hash(ctx, plan, tmpl) == contract_input_hash(ctx, plan, tmpl)
    assert declarations_output_hash(plan) == declarations_output_hash(plan)


def test_compiler_input_fingerprint_changes_on_additivity(db):
    _seed_core(db)
    ctx = _ctx(db)
    base = compiler_input_fingerprint(ctx, "core")
    # mutate a bound column's additivity in a copied snapshot (the classifier reads this — F3)
    cols = {ref: col for ref, col in ctx.columns_by_catalog["core"].items()}
    amt = cols["public.transactions.amount"]
    cols["public.transactions.amount"] = dataclasses.replace(amt, additivity="non_additive")
    ctx2 = _ctx(db, columns_override={"core": cols})
    assert compiler_input_fingerprint(ctx2, "core") != base


def test_planner_hash_changes_but_selected_contract_hash_unchanged_when_unrelated_column_added(db):
    # F5: a NEW candidate column changes the SELECTION universe (planner_input_hash) but not a plan
    # whose read-set doesn't include it (contract_input_hash).
    _seed_core(db)
    ctx, tmpl, plan = _ctx(db), _template(), _plan()
    ph, ch = planner_input_hash(ctx, tmpl, _Scope()), contract_input_hash(ctx, plan, tmpl)
    cols = {ref: col for ref, col in ctx.columns_by_catalog["core"].items()}
    extra = dataclasses.replace(cols["public.transactions.amount"],
                                object_ref="public.transactions.tip", concept="monetary_flow")
    cols["public.transactions.tip"] = extra
    ctx2 = _ctx(db, columns_override={"core": cols})
    assert planner_input_hash(ctx2, tmpl, _Scope()) != ph      # universe changed
    assert contract_input_hash(ctx2, plan, tmpl) == ch          # the plan's read-set did NOT


def test_output_change_leaves_inputs_but_moves_output_hash(db):
    # F4: an OUTPUT change under FIXED inputs => input hash unchanged, output hash changed.
    _seed_core(db)
    ctx, tmpl, plan = _ctx(db), _template(), _plan()
    in_h, out_h = contract_input_hash(ctx, plan, tmpl), declarations_output_hash(plan)
    plan2 = dataclasses.replace(plan, declaration_status=c.DeclarationStatus.unresolved_aggregation_declaration)
    assert contract_input_hash(ctx, plan2, tmpl) == in_h        # inputs untouched
    assert declarations_output_hash(plan2) != out_h             # output moved


def test_agg_declaration_change_moves_contract_input_hash(db):
    # F13: agg_declarations is a verdict input.
    _seed_core(db)
    tmpl, plan = _template(), _plan()
    base = contract_input_hash(_ctx(db), plan, tmpl)
    ctx_agg = _ctx(db, agg={("fp_t", "amt"): c.AggregationFunction.weighted_average})
    assert contract_input_hash(ctx_agg, plan, tmpl) != base

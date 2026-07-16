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
from featuregen.overlay.upload.need_metadata import RESOLVED_NEED_METADATA
from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.declarations import (
    CompileBudget,
    CompilerContext,
    PathPositionV1,
    check_composition,
    check_connectivity,
    compile_aggregation,
    compile_temporal,
    hop_physical_cardinality,
    resolve_additivity,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality
from featuregen.overlay.upload.templates import Need, Template, _load_columns

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


def _ctx(db, *catalogs: str,
         agg: dict[tuple[str, str], c.AggregationFunction] | None = None) -> CompilerContext:
    """The C2 test constructor — batch-loads via the REAL loaders, then drops the connection.
    (The per-run production builder `build_compiler_context` is C8.) ``agg`` injects the declared
    aggregation-function registry (F5) — EMPTY in production, populated only by tests."""
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
        agg_declarations=agg or {})


def _binding(role, obj_ref, *, join_role=str(JoinRole.MEASURE), catalog="core",
             concept="monetary_flow", temporal=str(TemporalRole.NONE)):
    return c.IngredientBindingV1(
        recipe_id="r1", need_role=role, concept=concept, required_grains=(),
        join_role=join_role, temporal_role=temporal,
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


def _rollup_segments(realization_id, *, from_entity="transaction", to_entity="account"):
    """The assembler's per-hop emission shape: semantic_rollup (no refs) + the realizer."""
    return (
        c.BindingPathSegmentV1(c.SegmentKind.semantic_rollup, "core",
                               from_entity=from_entity, to_entity=to_entity,
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


# ─── C3: temporal declaration on representative params ───────────────────────────────────────
# compile_temporal is PURE — template + plan material only; the empty conn-free context below
# proves no test (and no code path) needs a loaded catalog, let alone a connection.

def _empty_ctx() -> CompilerContext:
    return CompilerContext(
        realizations_by_catalog={}, active_bridges=(), columns_by_catalog={},
        catalog_fingerprint_at_start={}, bridge_fingerprint_at_start="", catalog_stamps={},
        config=_overlay_config(), roles=(), now=_NOW, agg_declarations={})


def _template(tid, needs, params, *, additivity="semi_additive") -> Template:
    """A DIRECT Template(...) construction — deliberately NOT registered in ALL_TEMPLATES, so a
    static-registry lookup (RESOLVED_NEED_METADATA[tid]) would KeyError. Real concepts, so
    derive_need_metadata resolves against the governed registry (F17)."""
    return Template(
        id=tid, family="c3_test_family", intent="C3 injected recipe (not in the static registry)",
        needs=needs, params=params, aggregation="sum", additivity=additivity, explain="H",
        use_cases=("test",), pit="as of t, trailing window (t − w, t]")


def test_temporal_custom_template_asof_unbound_is_anchor_missing():
    # an as-of roll-up whose as-of need has NO bound ingredient: the anchor ROLE is declared
    # (pit_anchor set) but nothing supplies it -> temporal_anchor_missing, anchor_binding=None.
    t = _template(
        "c3_asof_rollup",
        needs=(Need("stock_col", "monetary_stock"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={})
    assert t.id not in RESOLVED_NEED_METADATA      # proves derive_need_metadata must be used
    plan = _plan(
        bindings=(
            _binding("entity", "public.accounts.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("stock_col", "public.accounts.balance", concept="monetary_stock"),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    assert out.pit_anchor == str(TemporalRole.AS_OF_TIME)
    assert out.anchor_binding is None
    assert out.reason_codes == (c.ReasonCode.temporal_anchor_missing,)
    # no window param -> pure point-in-time: no window, no time-axis aggregation
    assert out.window is None and out.time_axis_aggregating is False


def test_temporal_window_param_binds_representatively():
    t = _template(
        "c3_windowed_trend",
        needs=(Need("stock_col", "monetary_stock"), Need("asof", "as_of_date"),
               Need("entity", "customer_id")),
        params={"window": (90, 60, 30), "measure": ("normalized", "slope")})
    plan = _plan(
        bindings=(
            _binding("entity", "public.accounts.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("stock_col", "public.accounts.balance", concept="monetary_stock"),
            _binding("asof", "public.accounts.as_of_date", join_role=str(JoinRole.TIME),
                     concept="as_of_date", temporal=str(TemporalRole.AS_OF_TIME)),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    # FIRST allowed value of each param is the representative; typed window, never a string
    assert out.window == c.WindowSpecV1(length=90, unit="days", boundary="trailing",
                                        inclusive=True)
    assert out.param_binding.is_representative is True
    assert out.param_binding.values == (("measure", "normalized"), ("window", "90"))
    assert out.time_axis_aggregating is True       # a trailing window rolls the measure over time
    assert out.pit_anchor == str(TemporalRole.AS_OF_TIME)
    assert out.anchor_binding == "public.accounts.as_of_date"
    assert out.reason_codes == ()


def test_temporal_window_min_param_is_a_minute_window():
    # the corpus's real-time family windows in MINUTES (window_min), never trailing days
    t = _template(
        "c3_realtime_velocity",
        needs=(Need("flow_col", "monetary_flow"), Need("event_ts", "event_timestamp"),
               Need("entity", "customer_id")),
        params={"window_min": (60, 15, 1440)})
    plan = _plan(
        bindings=(
            _binding("entity", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("event_ts", "public.transactions.event_ts", join_role=str(JoinRole.TIME),
                     concept="event_timestamp", temporal=str(TemporalRole.EVENT_TIME)),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    assert out.window == c.WindowSpecV1(length=60, unit="minutes", boundary="trailing",
                                        inclusive=True)
    assert out.time_axis_aggregating is True
    assert out.pit_anchor == str(TemporalRole.EVENT_TIME)
    assert out.anchor_binding == "public.transactions.event_ts"


def test_temporal_bitemporal_interval_is_valid_not_ambiguous():
    # valid_from + valid_to TOGETHER describe one validity interval (F17) — never flagged
    # ambiguous merely because two temporal roles are present. Neither is a primary PIT anchor.
    t = _template(
        "c3_bitemporal_attrs",
        needs=(Need("attr", "monetary_stock"),
               Need("vf", "effective_date", temporal_role=TemporalRole.VALID_FROM),
               Need("vt", "effective_date", temporal_role=TemporalRole.VALID_TO),
               Need("entity", "customer_id")),
        params={})
    plan = _plan(
        bindings=(
            _binding("entity", "public.accounts.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("vf", "public.accounts.valid_from", join_role=str(JoinRole.TIME),
                     concept="effective_date", temporal=str(TemporalRole.VALID_FROM)),
            _binding("vt", "public.accounts.valid_to", join_role=str(JoinRole.TIME),
                     concept="effective_date", temporal=str(TemporalRole.VALID_TO)),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    assert c.ReasonCode.temporal_anchor_ambiguous not in out.reason_codes
    assert out.pit_anchor is None and out.anchor_binding is None
    assert out.reason_codes == ()      # a valid interval is not a missing anchor either


def test_temporal_two_distinct_event_anchors_are_ambiguous():
    # two DISTINCT event-time needs bound to DIFFERENT columns genuinely compete — no honest
    # single PIT anchor exists.
    t = _template(
        "c3_two_event_axes",
        needs=(Need("e1", "event_timestamp"), Need("e2", "event_timestamp"),
               Need("entity", "customer_id")),
        params={})
    plan = _plan(
        bindings=(
            _binding("entity", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("e1", "public.transactions.trade_ts", join_role=str(JoinRole.TIME),
                     concept="event_timestamp", temporal=str(TemporalRole.EVENT_TIME)),
            _binding("e2", "public.transactions.settle_ts", join_role=str(JoinRole.TIME),
                     concept="event_timestamp", temporal=str(TemporalRole.EVENT_TIME)),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    assert out.reason_codes == (c.ReasonCode.temporal_anchor_ambiguous,)
    assert out.pit_anchor is None and out.anchor_binding is None


def test_temporal_asof_takes_precedence_over_event_time():
    # as_of + event coexist legitimately across the corpus (e.g. margin_call_intensity: the
    # as-of is the evaluation date, the event axis is the measured one) — the as-of WINS as the
    # primary PIT anchor; coexistence is NOT ambiguity.
    t = _template(
        "c3_asof_plus_event",
        needs=(Need("flow_col", "monetary_flow"), Need("asof", "as_of_date"),
               Need("event_ts", "event_timestamp"), Need("entity", "customer_id")),
        params={"window": (30, 90)})
    plan = _plan(
        bindings=(
            _binding("entity", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("asof", "public.transactions.as_of_date", join_role=str(JoinRole.TIME),
                     concept="as_of_date", temporal=str(TemporalRole.AS_OF_TIME)),
            _binding("event_ts", "public.transactions.event_ts", join_role=str(JoinRole.TIME),
                     concept="event_timestamp", temporal=str(TemporalRole.EVENT_TIME)),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    assert out.pit_anchor == str(TemporalRole.AS_OF_TIME)
    assert out.anchor_binding == "public.transactions.as_of_date"
    assert out.reason_codes == ()
    assert out.window == c.WindowSpecV1(30, "days", "trailing", True)


def test_temporal_same_role_anchors_on_one_column_are_one_anchor():
    # two same-role needs BOUND TO THE SAME COLUMN are one anchor, not a competition
    t = _template(
        "c3_two_needs_one_axis",
        needs=(Need("e1", "event_timestamp"), Need("e2", "event_timestamp"),
               Need("entity", "customer_id")),
        params={})
    plan = _plan(
        bindings=(
            _binding("entity", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("e1", "public.transactions.event_ts", join_role=str(JoinRole.TIME),
                     concept="event_timestamp", temporal=str(TemporalRole.EVENT_TIME)),
            _binding("e2", "public.transactions.event_ts", join_role=str(JoinRole.TIME),
                     concept="event_timestamp", temporal=str(TemporalRole.EVENT_TIME)),
        ),
        segments=())
    out = compile_temporal(_empty_ctx(), plan, t)
    assert out.pit_anchor == str(TemporalRole.EVENT_TIME)
    assert out.anchor_binding == "public.transactions.event_ts"
    assert out.reason_codes == ()


# ─── C4: per-ingredient aggregation + additivity + physical/bridge cardinality ────────────────
# compile_aggregation VALIDATES, never fabricates: the only auto-derivations are the two SUM rules
# (additive fan-in; semi_additive entity-axis single-PIT), both expressed as validation=sound with
# declared_function=None — SUM is never written into the DECLARED slot. Every other function must
# come from the injected ctx.agg_declarations registry (empty in production).

def _acct_core(db):
    """core: accounts (account grain) rolls up N:1 to customers (customer grain). The measure
    columns cover every additivity class: concept-additive (amount), concept-semi-additive
    (balance), concept-non-additive (rate), an uploaded override that CONFLICTS with its concept
    (forced), an unrecognized uploaded value (weird), and a non-aggregating categorical (product)."""
    _seed(db, "core", [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("core", "accounts", "amount", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
        (CanonicalRow("core", "accounts", "rate", "numeric"), "monetary_rate"),
        (CanonicalRow("core", "accounts", "rate_weight", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "accounts", "forced", "numeric", additivity="non_additive"),
         "monetary_flow"),
        (CanonicalRow("core", "accounts", "weird", "numeric", additivity="entangled"),
         "monetary_flow"),
        (CanonicalRow("core", "accounts", "product", "text"), "product_type"),
        (CanonicalRow("core", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])


_T_C4 = _template(
    "c4_agg_probe",
    needs=(Need("m", "monetary_flow"), Need("entity", "customer_id")), params={})


def _temporal(*, aggregating: bool) -> c.TemporalDeclarationV1:
    """The C3 output shape C4 consumes: a single-PIT read (False) vs a trailing-window
    time-axis-aggregating recipe (True)."""
    return c.TemporalDeclarationV1(
        pit_anchor=str(TemporalRole.AS_OF_TIME), anchor_binding=None,
        window=c.WindowSpecV1(90, "days", "trailing", True) if aggregating else None,
        param_binding=c.ParamBindingV1(values=(), is_representative=True),
        time_axis_aggregating=aggregating, reason_codes=())


def _c4_plan(ctx, *measure_bindings):
    """A source-key-anchored account→customer roll-up plan over _acct_core's one realization."""
    (r,) = ctx.realizations_by_catalog["core"]
    return _plan(
        bindings=(_binding("source_key", "public.accounts.customer_id",
                           join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
                  *measure_bindings),
        segments=_rollup_segments(r.realization_id, from_entity="account", to_entity="customer"))


def _c4_compile(ctx, plan, *, aggregating=False):
    return compile_aggregation(ctx, plan, _T_C4, _temporal(aggregating=aggregating),
                               check_connectivity(ctx, plan).placement)


def test_additive_fan_in_undeclared_is_sound_sum_default(db):
    # §4 bank example 1: transaction_amount (additive), fan-in, undeclared → sound (SUM default).
    _acct_core(db)
    ctx = _ctx(db, "core")
    plan = _c4_plan(ctx, _binding("amount", "public.accounts.amount"))
    (hop,) = _c4_compile(ctx, plan)
    assert (hop.semantic_hop_index, hop.segment_index) == (0, 1)
    assert (hop.from_entity, hop.to_entity) == ("account", "customer")
    assert hop.physical_cardinality is Cardinality.MANY_TO_ONE
    assert hop.cardinality_source == "realization"
    assert hop.grouping_keys == ("public.customers.customer_id",)    # the realization's to-key
    assert (hop.execution_catalog, hop.execution_table) == ("core", "public.customers")
    (stage,) = hop.ingredient_stages     # the source key is NEVER an aggregation stage
    assert stage.need_role == "amount"
    assert stage.additivity is c.AdditivityClass.additive
    assert stage.axis is c.AggregationAxisKind.entity
    assert stage.physical_cardinality is Cardinality.MANY_TO_ONE
    assert stage.validation is c.AggregationValidation.sound
    # SUM is the VERSIONED auto-derivation (AGGREGATION_RULE_VERSION) — never fabricated into
    # the declared slot: an absent declaration stays honestly None.
    assert stage.declared_function is None
    assert stage.reason_codes == () and stage.missing_inputs == ()


def test_non_additive_declared_sum_is_incompatible(db):
    # §4 bank example 2: interest_rate (non_additive), fan-in, registry-declared SUM → incompatible.
    _acct_core(db)
    ctx = _ctx(db, "core", agg={("r1", "rate"): c.AggregationFunction.sum})
    plan = _c4_plan(ctx, _binding("rate", "public.accounts.rate", concept="monetary_rate"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.additivity is c.AdditivityClass.non_additive
    assert stage.declared_function is c.AggregationFunction.sum
    assert stage.validation is c.AggregationValidation.incompatible
    assert stage.reason_codes == (c.ReasonCode.aggregation_incompatible_with_additivity,)


def test_non_additive_undeclared_is_strategy_missing(db):
    # §4 bank example 3: interest_rate (non_additive), fan-in, undeclared → undeclared.
    _acct_core(db)
    ctx = _ctx(db, "core")
    plan = _c4_plan(ctx, _binding("rate", "public.accounts.rate", concept="monetary_rate"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.validation is c.AggregationValidation.undeclared
    assert stage.declared_function is None
    assert stage.reason_codes == (c.ReasonCode.aggregation_strategy_missing,)


def test_non_additive_weighted_average_with_weight_unbound_is_inputs_missing(db):
    # §4 bank example 4: declared weighted_average, weight NOT bound → inputs_missing with the
    # missing role recorded.
    _acct_core(db)
    ctx = _ctx(db, "core", agg={("r1", "rate"): c.AggregationFunction.weighted_average})
    plan = _c4_plan(ctx, _binding("rate", "public.accounts.rate", concept="monetary_rate"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.validation is c.AggregationValidation.inputs_missing
    assert stage.reason_codes == (c.ReasonCode.aggregation_weight_missing,)
    assert stage.missing_inputs == ("rate_weight",)


def test_non_additive_weighted_average_with_weight_bound_is_sound(db):
    # the converse: the declared weight role ("<role>_weight") IS bound → the input check passes.
    # Stages stay sorted by need_role (rate, rate_weight) — determinism.
    _acct_core(db)
    ctx = _ctx(db, "core", agg={("r1", "rate"): c.AggregationFunction.weighted_average})
    plan = _c4_plan(ctx,
                    _binding("rate", "public.accounts.rate", concept="monetary_rate"),
                    _binding("rate_weight", "public.accounts.rate_weight"))
    (hop,) = _c4_compile(ctx, plan)
    assert [s.need_role for s in hop.ingredient_stages] == ["rate", "rate_weight"]
    rate_stage = hop.ingredient_stages[0]
    assert rate_stage.validation is c.AggregationValidation.sound
    assert rate_stage.declared_function is c.AggregationFunction.weighted_average
    assert rate_stage.missing_inputs == () and rate_stage.reason_codes == ()


def test_non_additive_ratio_recompute_components_missing(db):
    # declared ratio_recompute with neither component bound → inputs_missing, BOTH roles recorded.
    _acct_core(db)
    ctx = _ctx(db, "core", agg={("r1", "rate"): c.AggregationFunction.ratio_recompute})
    plan = _c4_plan(ctx, _binding("rate", "public.accounts.rate", concept="monetary_rate"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.validation is c.AggregationValidation.inputs_missing
    assert stage.reason_codes == (c.ReasonCode.aggregation_components_missing,)
    assert stage.missing_inputs == ("rate_numerator", "rate_denominator")


def test_semi_additive_single_pit_entity_rollup_is_sound(db):
    # §4 bank example 5: balance (semi_additive), entity roll-up at a single PIT → sound (SUM).
    _acct_core(db)
    ctx = _ctx(db, "core")
    plan = _c4_plan(ctx, _binding("balance", "public.accounts.balance", concept="monetary_stock"))
    (hop,) = _c4_compile(ctx, plan, aggregating=False)
    (stage,) = hop.ingredient_stages
    assert stage.additivity is c.AdditivityClass.semi_additive
    assert stage.validation is c.AggregationValidation.sound
    assert stage.declared_function is None      # the second versioned auto-derivation
    assert stage.reason_codes == ()


def test_semi_additive_across_window_is_temporal_strategy_missing(db):
    # §4 bank example 6: balance (semi_additive) summed across a 90-day window with NO declared
    # temporal strategy → undeclared.
    _acct_core(db)
    ctx = _ctx(db, "core")
    plan = _c4_plan(ctx, _binding("balance", "public.accounts.balance", concept="monetary_stock"))
    (hop,) = _c4_compile(ctx, plan, aggregating=True)
    (stage,) = hop.ingredient_stages
    assert stage.validation is c.AggregationValidation.undeclared
    assert stage.reason_codes == (c.ReasonCode.semi_additive_temporal_strategy_missing,)


def test_semi_additive_across_window_declared_strategy_validates(db):
    # a DECLARED temporal strategy resolves the across-window case: take_latest is sound; a
    # declared SUM of a stock over time stays incompatible (the classic balance-summing error).
    _acct_core(db)
    ctx = _ctx(db, "core", agg={("r1", "balance"): c.AggregationFunction.take_latest})
    plan = _c4_plan(ctx, _binding("balance", "public.accounts.balance", concept="monetary_stock"))
    (hop,) = _c4_compile(ctx, plan, aggregating=True)
    (stage,) = hop.ingredient_stages
    assert stage.validation is c.AggregationValidation.sound
    assert stage.declared_function is c.AggregationFunction.take_latest

    ctx_sum = _ctx(db, "core", agg={("r1", "balance"): c.AggregationFunction.sum})
    (hop2,) = _c4_compile(ctx_sum, _c4_plan(
        ctx_sum, _binding("balance", "public.accounts.balance", concept="monetary_stock")),
        aggregating=True)
    (stage2,) = hop2.ingredient_stages
    assert stage2.validation is c.AggregationValidation.incompatible
    assert stage2.reason_codes == (c.ReasonCode.aggregation_incompatible_with_additivity,)


def test_additivity_precedence_uploaded_beats_concept(db):
    # §4.1 precedence: an uploaded column additivity outranks the concept's; an UNSTATED upload
    # (stored NULL) falls through to the concept.
    _acct_core(db)
    ctx = _ctx(db, "core")
    prov = resolve_additivity(ctx, _binding("forced", "public.accounts.forced"))
    assert prov.selected is c.AdditivityClass.non_additive
    assert prov.source is c.AdditivitySource.uploaded_column
    assert prov.conflict is True
    assert prov.uploaded_value == "non_additive" and prov.concept_value == "additive"   # F15: BOTH raw values kept
    prov2 = resolve_additivity(ctx, _binding("amount", "public.accounts.amount"))
    assert prov2.selected is c.AdditivityClass.additive
    assert prov2.source is c.AdditivitySource.concept
    assert prov2.conflict is False and prov2.uploaded_value is None


def test_additivity_unknown_when_neither_source_asserts(db):
    # no resolvable column AND no registry concept → honest unknown/unknown, never a guess.
    _acct_core(db)
    ctx = _ctx(db, "core")
    prov = resolve_additivity(
        ctx, _binding("ghost", "public.accounts.missing", concept="not_a_concept"))
    assert prov.selected is c.AdditivityClass.unknown
    assert prov.source is c.AdditivitySource.unknown
    assert prov.uploaded_value is None and prov.concept_value is None
    assert prov.conflict is False


def test_conflict_carries_reason_and_both_values_on_the_stage(db):
    # uploaded non_additive vs concept additive: the uploaded value WINS the validation (undeclared
    # non-additive → strategy missing) and the conflict is auditable on the stage — the reason code
    # plus both raw values in provenance, never silently resolved.
    _acct_core(db)
    ctx = _ctx(db, "core")
    plan = _c4_plan(ctx, _binding("forced", "public.accounts.forced"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.additivity is c.AdditivityClass.non_additive
    assert stage.validation is c.AggregationValidation.undeclared
    assert stage.reason_codes == (c.ReasonCode.aggregation_strategy_missing,
                                  c.ReasonCode.additivity_source_conflict)
    assert stage.provenance.conflict is True
    assert (stage.provenance.uploaded_value, stage.provenance.concept_value) \
        == ("non_additive", "additive")


def test_unknown_additivity_is_never_treated_as_additive(db):
    # an unrecognized uploaded value resolves to unknown — which NEVER silently aggregates, even
    # when a function IS declared (compatibility can't be validated against unknown). The
    # unparseable upload also genuinely disagrees with the concept → conflict is flagged.
    _acct_core(db)
    ctx = _ctx(db, "core", agg={("r1", "weird"): c.AggregationFunction.sum})
    plan = _c4_plan(ctx, _binding("weird", "public.accounts.weird"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.additivity is c.AdditivityClass.unknown
    assert stage.provenance.source is c.AdditivitySource.uploaded_column
    assert stage.validation is c.AggregationValidation.undeclared
    assert stage.reason_codes == (c.ReasonCode.aggregation_strategy_missing,
                                  c.ReasonCode.additivity_source_conflict)


def test_non_aggregating_measure_on_fan_in_is_axis_unsupported(db):
    # additivity n/a (a categorical) sitting on a fan-in hop → incompatible: the measure does not
    # aggregate on ANY axis.
    _acct_core(db)
    ctx = _ctx(db, "core")
    plan = _c4_plan(ctx, _binding("product", "public.accounts.product", concept="product_type"))
    (hop,) = _c4_compile(ctx, plan)
    (stage,) = hop.ingredient_stages
    assert stage.additivity is c.AdditivityClass.not_applicable
    assert stage.provenance.source is c.AdditivitySource.concept
    assert stage.validation is c.AggregationValidation.incompatible
    assert stage.reason_codes == (c.ReasonCode.aggregation_axis_unsupported,)


def test_realized_hop_uses_realization_cardinality_not_the_segment_string(db):
    # F4/F8: the REALIZATION's declared_cardinality is the physical authority — the semantic
    # segment's cardinality string (here "many_to_one") is never consulted. A 1:1 realization
    # means NO fan-in: the same plan compiles to zero aggregation hops.
    _acct_core(db)
    ctx = _ctx(db, "core")
    (r,) = ctx.realizations_by_catalog["core"]
    seg = c.BindingPathSegmentV1(c.SegmentKind.intra_catalog_realization, "core",
                                 realization_ref=r.realization_id)
    assert hop_physical_cardinality(ctx, seg) == (
        Cardinality.MANY_TO_ONE, "realization", ("public.customers.customer_id",))

    r11 = dataclasses.replace(r, declared_cardinality=Cardinality.ONE_TO_ONE)
    ctx11 = dataclasses.replace(ctx, realizations_by_catalog={"core": (r11,)})
    assert hop_physical_cardinality(ctx11, seg)[0] is Cardinality.ONE_TO_ONE
    plan = _c4_plan(ctx11, _binding("amount", "public.accounts.amount"))
    assert _c4_compile(ctx11, plan) == ()


def _bridge_core_crm(db):
    """core.accounts holds a customer-keyed FK bridged (VERIFIED) to crm.customers — the
    E2-key-FK → E2-grain-table construction of a governed roll-up bridge."""
    _seed(db, "core", [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "customer_id", "integer"), "customer_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
    ])
    _seed(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("crm", "customers", "spend", "numeric"), "monetary_flow"),
    ])
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref,"
        " right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        ("bridge:customer:c4", "customer", "core", "public.accounts.customer_id",
         "crm", "public.customers.customer_id"))


def test_bridge_rollup_hop_is_many_to_one_by_construction(db):
    # a bridge-rollup hop has NO realization — its fan-in is many_to_one BY CONSTRUCTION (an
    # E2-key FK column linked to an E2-grain far table); the far (target-grain) endpoint is the
    # GROUP-BY key and the execution site.
    _bridge_core_crm(db)
    ctx = _ctx(db, "core", "crm")
    plan = _plan(
        bindings=(
            _binding("source_key", "public.accounts.customer_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("balance", "public.accounts.balance", concept="monetary_stock"),
        ),
        segments=(
            c.BindingPathSegmentV1(c.SegmentKind.semantic_rollup, "core",
                                   from_entity="account", to_entity="customer",
                                   cardinality="many_to_one"),
            c.BindingPathSegmentV1(c.SegmentKind.governed_bridge, "crm",
                                   from_entity="account", to_entity="customer",
                                   bridge_fact_key="bridge:customer:c4"),
        ))
    assert hop_physical_cardinality(ctx, plan.path_segments[1]) == (
        Cardinality.MANY_TO_ONE, "bridge_construction", ("public.customers.customer_id",))
    (hop,) = _c4_compile(ctx, plan)
    assert (hop.semantic_hop_index, hop.segment_index) == (0, 1)
    assert hop.physical_cardinality is Cardinality.MANY_TO_ONE
    assert hop.cardinality_source == "bridge_construction"
    assert hop.grouping_keys == ("public.customers.customer_id",)
    assert (hop.execution_catalog, hop.execution_table) == ("crm", "public.customers")
    (stage,) = hop.ingredient_stages
    assert stage.need_role == "balance"
    assert stage.validation is c.AggregationValidation.sound    # semi_additive, single-PIT

    # an unknown fact key resolves NOTHING — fail closed, never many_to_one on faith
    ghost = c.BindingPathSegmentV1(c.SegmentKind.governed_bridge, "crm",
                                   from_entity="account", to_entity="customer",
                                   bridge_fact_key="bridge:customer:ghost")
    assert hop_physical_cardinality(ctx, ghost) == (None, "unavailable", ())


def test_reposition_bridge_is_not_an_aggregation_hop(db):
    # a same-entity governed_bridge (reposition) crosses on the GRAIN key — 1:1 by construction,
    # no fan-in, never an aggregation hop.
    _bridge_core_crm(db)
    ctx = _ctx(db, "core", "crm")
    plan = _plan(
        bindings=(
            _binding("source_key", "public.accounts.customer_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id"),
            _binding("spend", "public.customers.spend", catalog="crm"),
        ),
        segments=(
            c.BindingPathSegmentV1(c.SegmentKind.governed_bridge, "crm",
                                   from_entity="customer", to_entity="customer",
                                   bridge_fact_key="bridge:customer:c4"),
        ))
    assert _c4_compile(ctx, plan) == ()


def test_unrealized_hop_fails_closed_as_cardinality_unavailable(db):
    # matrix row 1: no physical evidence (a bare semantic_rollup, or a realizer whose ref the
    # context cannot resolve) → the hop compiles with cardinality None and every stage honestly
    # undeclared/physical_cardinality_unavailable — never a guessed fan-in.
    _acct_core(db)
    ctx = _ctx(db, "core")
    bare = c.BindingPathSegmentV1(c.SegmentKind.semantic_rollup, "core",
                                  from_entity="account", to_entity="customer",
                                  cardinality="many_to_one")
    assert hop_physical_cardinality(ctx, bare) == (None, "unavailable", ())
    src = _binding("source_key", "public.accounts.customer_id",
                   join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="customer_id")
    bal = _binding("balance", "public.accounts.balance", concept="monetary_stock")
    (hop,) = _c4_compile(ctx, _plan(bindings=(src, bal), segments=(bare,)))
    assert hop.physical_cardinality is None
    assert hop.cardinality_source == "unavailable"
    assert hop.grouping_keys == () and hop.execution_table == ""
    (stage,) = hop.ingredient_stages
    assert stage.validation is c.AggregationValidation.undeclared
    assert stage.reason_codes == (c.ReasonCode.physical_cardinality_unavailable,)

    (hop2,) = _c4_compile(ctx, _plan(
        bindings=(src, bal),
        segments=_rollup_segments("core:no.such->ref", from_entity="account",
                                  to_entity="customer")))
    assert hop2.physical_cardinality is None and hop2.cardinality_source == "unavailable"
    (stage2,) = hop2.ingredient_stages
    assert stage2.reason_codes == (c.ReasonCode.physical_cardinality_unavailable,)


def _chain_core(db):
    """core: transactions → accounts → customers, both roll-ups N:1 — the two-fan-in-hop chain."""
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "transactions", "amount", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
        (CanonicalRow("core", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])


def test_measures_assigned_once_to_first_fan_in_hop_and_hops_ordered(db):
    # each measure is staged EXACTLY ONCE, at the FIRST fan-in hop at/after its placement
    # position: amount (placed at the source table, position 0) and balance (placed at hop 1's
    # to-table, position 1) both stage at the first hop; the second fan-in hop carries no stages
    # here — whether hop 1's OUTPUT may be re-aggregated at hop 2 is C5's composition guard, not a
    # duplicate stage. Hops are ordered by segment_index; stages by need_role.
    _chain_core(db)
    ctx = _ctx(db, "core")
    by_from = {r.from_object_ref: r for r in ctx.realizations_by_catalog["core"]}
    r_txn, r_acct = by_from["public.transactions"], by_from["public.accounts"]
    plan = _plan(
        bindings=(
            _binding("source_key", "public.transactions.account_id",
                     join_role=str(JoinRole.SOURCE_ENTITY_KEY), concept="account_id"),
            _binding("balance", "public.accounts.balance", concept="monetary_stock"),
            _binding("amount", "public.transactions.amount"),
        ),
        segments=(*_rollup_segments(r_txn.realization_id),
                  *_rollup_segments(r_acct.realization_id, from_entity="account",
                                    to_entity="customer")))
    hops = _c4_compile(ctx, plan)
    assert [(h.semantic_hop_index, h.segment_index) for h in hops] == [(0, 1), (1, 3)]
    assert [s.need_role for s in hops[0].ingredient_stages] == ["amount", "balance"]
    assert hops[1].ingredient_stages == ()
    assert (hops[0].from_entity, hops[0].to_entity) == ("transaction", "account")
    assert (hops[1].from_entity, hops[1].to_entity) == ("account", "customer")


# ─── C5: cross-hop composition guard ──────────────────────────────────────────────────────────
# check_composition is PURE over C4's output tuples + the recipe's declared OUTPUT additivity —
# hops are built directly here (unit-level, no DB). Conservative + fail-closed (spec §4.2): the
# recipe has no output algebra, so only PROVABLY sound chains pass (additive SUM∘SUM with the
# grouping key surviving every hop boundary); everything else is composition-unsupported.

def _c5_stage(role, *, additivity=c.AdditivityClass.additive, declared=None,
              validation=c.AggregationValidation.sound, table="public.accounts"):
    return c.IngredientAggregationV1(
        need_role=role, bound_object_ref=f"{table}.{role}", additivity=additivity,
        provenance=c.AdditivityProvenanceV1(
            uploaded_value=None, concept_value=str(additivity), selected=additivity,
            source=c.AdditivitySource.concept, conflict=False),
        physical_cardinality=Cardinality.MANY_TO_ONE, axis=c.AggregationAxisKind.entity,
        declared_function=declared, validation=validation, missing_inputs=(), reason_codes=())


def _c5_hop(sem_idx, seg_idx, *, from_entity, to_entity, table, key, catalog="core",
            source="realization", stages=()):
    return c.HopAggregationV1(
        semantic_hop_index=sem_idx, segment_index=seg_idx,
        from_entity=from_entity, to_entity=to_entity,
        execution_catalog=catalog, execution_table=table,
        physical_cardinality=Cardinality.MANY_TO_ONE, cardinality_source=source,
        grouping_keys=(key,), ingredient_stages=tuple(stages))


def _c5_two_hop(stage0=(), stage1=()):
    """The C4 chain shape (transactions → accounts → customers, both N:1, one catalog):
    two fan-in hops whose entity axis chains and whose grouping keys survive by construction."""
    return (
        _c5_hop(0, 1, from_entity="transaction", to_entity="account",
                table="public.accounts", key="public.accounts.account_id", stages=stage0),
        _c5_hop(1, 3, from_entity="account", to_entity="customer",
                table="public.customers", key="public.customers.customer_id", stages=stage1),
    )


def test_composition_sum_of_sum_additive_across_two_hops_composes():
    # the ONE provably sound chain: additive measures aggregated by SUM at hop 1 — the versioned
    # auto-rule (declared None) AND an explicitly declared SUM — re-aggregated by SUM at hop 2,
    # grouping surviving the intra-catalog chain, additive declared OUTPUT.
    hops = _c5_two_hop(stage0=(
        _c5_stage("amount"),
        _c5_stage("fees", declared=c.AggregationFunction.sum),
    ))
    out = check_composition(hops, c.AdditivityClass.additive)
    assert out.composable is True and out.reason_codes == ()


def test_composition_average_intermediate_reaggregated_is_unsupported():
    # the average-of-average case: hop 1's declared averaging function (the corpus enum's
    # weighted_average; spec §4.2 names average_over_period as the same class) is individually
    # SOUND at C4 with its weight bound — but its OUTPUT is a non-additive intermediate with no
    # surviving weight, re-aggregated at hop 2 → not provable, fail closed. Two failing stages
    # dedup to the ONE canonical code.
    hops = _c5_two_hop(stage0=(
        _c5_stage("rate", additivity=c.AdditivityClass.non_additive,
                  declared=c.AggregationFunction.weighted_average),
        _c5_stage("fee_rate", additivity=c.AdditivityClass.non_additive,
                  declared=c.AggregationFunction.weighted_average),
    ))
    out = check_composition(hops, c.AdditivityClass.non_additive)
    assert out.composable is False
    assert out.reason_codes == (c.ReasonCode.aggregation_composition_unsupported,)


def test_composition_semi_additive_summed_across_two_hops_is_unsupported():
    # a balance rolled up at hop 1 (the single-PIT auto-SUM — individually sound) yields an
    # account-grain aggregate whose re-aggregability at hop 2 cannot be proven from a
    # semi-additive input → unsupported; a declared take_latest intermediate (equally sound
    # per-ingredient) is equally unprovable downstream. Sound stages can still fail composition.
    hops = _c5_two_hop(stage0=(
        _c5_stage("balance", additivity=c.AdditivityClass.semi_additive),))
    out = check_composition(hops, c.AdditivityClass.semi_additive)
    assert out.composable is False
    assert out.reason_codes == (c.ReasonCode.aggregation_composition_unsupported,)
    latest = _c5_two_hop(stage0=(
        _c5_stage("balance", additivity=c.AdditivityClass.semi_additive,
                  declared=c.AggregationFunction.take_latest),))
    assert check_composition(latest, c.AdditivityClass.semi_additive).composable is False


def test_composition_grouping_lost_across_bridge_is_unsupported():
    # additive SUM∘SUM, but hop 2 executes in ANOTHER catalog (a bridge crossing): hop evidence
    # carries no from-side keys for the later hop, so hop 1's grouping key cannot be confirmed
    # to survive → fail closed even though every stage is individually sound.
    hops = (
        _c5_hop(0, 1, from_entity="transaction", to_entity="account",
                table="public.accounts", key="public.accounts.account_id",
                stages=(_c5_stage("amount"),)),
        _c5_hop(1, 3, from_entity="account", to_entity="customer", catalog="crm",
                table="public.customers", key="public.customers.customer_id",
                source="bridge_construction"),
    )
    out = check_composition(hops, c.AdditivityClass.additive)
    assert out.composable is False
    assert out.reason_codes == (c.ReasonCode.aggregation_composition_unsupported,)


def test_composition_single_fan_in_hop_is_trivially_composable():
    # SUM(interest)/SUM(principal) at ONE hop is a valid weighted rate — a per-ingredient C4
    # concern, NOT a composition failure: with nothing downstream to compose, even the
    # non-additive-output ratio recipe passes the guard. Zero hops likewise.
    hop = _c5_hop(0, 1, from_entity="transaction", to_entity="account",
                  table="public.accounts", key="public.accounts.account_id",
                  stages=(_c5_stage("interest"), _c5_stage("principal")))
    out = check_composition((hop,), c.AdditivityClass.non_additive)
    assert out.composable is True and out.reason_codes == ()
    assert check_composition((), c.AdditivityClass.non_additive).composable is True


def test_composition_non_additive_output_over_pure_sum_chain_is_unsupported():
    # F13 output cross-check: the same chain that composes purely by SUM must DECLARE an
    # additive output — a non-additive/semi-additive/unknown OUTPUT over a pure-SUM chain is a
    # ratio/rate the recipe intends but never declared as algebra → not provably the intended
    # output, fail closed.
    hops = _c5_two_hop(stage0=(_c5_stage("amount"),))
    assert check_composition(hops, c.AdditivityClass.additive).composable is True
    out = check_composition(hops, c.AdditivityClass.non_additive)
    assert out.composable is False
    assert out.reason_codes == (c.ReasonCode.aggregation_composition_unsupported,)
    assert check_composition(hops, c.AdditivityClass.semi_additive).composable is False
    assert check_composition(hops, c.AdditivityClass.unknown).composable is False


def test_composition_last_hop_stage_flows_nowhere_and_broken_chains_fail_closed():
    # a measure staged at the LAST fan-in hop has no downstream re-aggregation: even a declared
    # weighted_average passes (its own hop is C4's per-ingredient concern) — and with no
    # cross-hop composition the F13 output cross-check has nothing to check.
    hops = _c5_two_hop(stage1=(
        _c5_stage("rate", additivity=c.AdditivityClass.non_additive,
                  declared=c.AggregationFunction.weighted_average),))
    assert check_composition(hops, c.AdditivityClass.non_additive).composable is True
    # an additive SUM chain whose LATER hop lost its physical evidence (cardinality unavailable →
    # no grouping keys, no execution table) is unprovable — fail closed
    broken = (
        _c5_two_hop(stage0=(_c5_stage("amount"),))[0],
        c.HopAggregationV1(
            semantic_hop_index=1, segment_index=3, from_entity="account", to_entity="customer",
            execution_catalog="core", execution_table="", physical_cardinality=None,
            cardinality_source="unavailable", grouping_keys=(), ingredient_stages=()),
    )
    assert check_composition(broken, c.AdditivityClass.additive).composable is False
    # an entity-axis discontinuity (a skipped hop between the two fan-ins) is never provable
    gap = (
        _c5_two_hop(stage0=(_c5_stage("amount"),))[0],
        _c5_hop(1, 5, from_entity="branch", to_entity="customer",
                table="public.customers", key="public.customers.customer_id"),
    )
    assert check_composition(gap, c.AdditivityClass.additive).composable is False

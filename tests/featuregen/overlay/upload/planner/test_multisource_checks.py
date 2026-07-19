"""Phase 3C.2b-i-A · Task 7 — per-path checks via reuse (aggregation + temporal, spec §5 step 5).

A validates each converged operand path by REUSING the single-source compiler unchanged: it drives
``check_connectivity(ctx, plan).placement`` -> ``compile_temporal(ctx, plan, template)`` ->
``compile_aggregation(ctx, plan, template, temporal, placement)`` over the operand's own governed
``BindingPlanV1`` (the Task-5 ``OperandPathCandidateV1``), with A's OWN ``CompilerContext`` (a
POPULATED ``agg_declarations``). Three checks live here:

* ``check_operand_path`` — any UNSOUND aggregation stage (e.g. a ``sum`` over a fan-in of a
  non-additive measure) maps to ``aggregation_unsafe_on_path``; an unsafe (safety-rejected) binding
  is likewise never let through. A sound path (a ``take_latest`` measure whose ordering anchor is
  proven at row grain) returns ``None``.
* ``check_paths_temporal_consistency`` — each path's PIT treatment individually valid AND mutually
  as-of-consistent at the common landing, else ``temporal_paths_incompatible``. Pure over the
  per-path ``TemporalDeclarationV1``s (conn-free), unit-tested directly like Task-6 convergence.
* ``check_time_slot_take_latest`` — A's OWN ordering-anchor validation for a TIME-slot ``take_latest``
  operand (RECENCY/TREND). ``compile_aggregation`` stages MEASURE join_role only, so it never sees a
  TIME operand; an unbindable ordering anchor must reject with ``ordering_anchor_missing`` (the
  multi-source reason), never silently degrade to the single-source ``temporal_anchor_missing``.

Fixtures are seeded through the REAL governance write paths exactly as the reuse spike / Task-5
enumeration suites do (``build_graph`` for the physical graph, ``entity_bridge_edge`` for VERIFIED
bridges, the ``propose_fact``/``_confirm_grain`` four-eyes flow for VERIFIED grain facts).
"""
from __future__ import annotations

from datetime import UTC, datetime

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
    AggregationFunction,
    CatalogScopeV1,
    ParamBindingV1,
    ReasonCode,
    TemporalDeclarationV1,
)
from featuregen.overlay.upload.planner.multisource_assembly import (
    ResolvedOperandPathV1,
    check_operand_path,
    check_paths_temporal_consistency,
    check_time_slot_take_latest,
    enumerate_operand_paths,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedSourceBindingV1,
    MultiSourceReason,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    SemanticRole,
)
from featuregen.overlay.upload.planner.multisource_reuse import build_operand_context
from featuregen.overlay.upload.upload_catalog import (
    ensure_upload_catalog_adapter,
    table_ref,
)

_NOW = datetime(2026, 7, 19, tzinfo=UTC)

# The DETERMINISTIC grain fact_key of the operand's SOURCE table (core_banking.transactions) — the
# source-endpoint revalidation (spec §2/§3.2) compares the binding's claimed grain key against it.
_SRC_GRAIN_FK = fact_key(table_ref("core_banking", "transactions"), "grain")


# ── seed helpers (the sanctioned assembly-suite / reuse-spike pattern) ─────────────────────────
def _seed(db, source, rows_concepts):
    rows = [r for r, _ in rows_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _seed_verified_bridge(db, fact_key, entity_id, lc, lref, rc, rref):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fact_key, entity_id, lc, lref, rc, rref))


def _seed_verified_grain(db, source, table, columns, *, service_actor, human_actor):
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
        scope_id="ms-checks", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-19T00:00:00Z",
        catalog_consideration_truncated=False)


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


def _operand(*, slot_id="op_0", catalog="core_banking", object_ref="public.transactions.amount",
             concept="monetary_flow", source_entity="transaction",
             source_key_ref="public.transactions.transaction_id",
             semantic_role=SemanticRole.measure, aggregation=PathAggregation.sum,
             output_additivity=AdditivityClass.additive, anchor_concept=None):
    return OperandSlotV1(
        slot_id=slot_id, semantic_role=semantic_role, catalog_source=catalog,
        object_ref=object_ref, authoritative_concept=concept,
        path_strategy=PathStrategyV1(
            aggregation=aggregation, output_type="numeric",
            output_additivity=output_additivity, external_type_required=False,
            ordering_anchor_concept=anchor_concept),
        source_binding=GovernedSourceBindingV1(
            source_grain_entity=source_entity, source_grain_key_refs=(source_key_ref,),
            grain_fact_key=_SRC_GRAIN_FK))


def _ctx(conn, catalogs, agg_declarations, now=_NOW):
    return build_operand_context(conn, catalogs=catalogs, roles=("feature_engineer",), now=now,
                                 agg_declarations=agg_declarations)


def _enumerate(conn, ctx, operand, scope, *, target_entity="customer", now=_NOW):
    return enumerate_operand_paths(
        conn, _adapter(), ctx, operand=operand, target_entity=target_entity, scope=scope,
        roles=("feature_engineer",), now=now)


def _first_path(conn, ctx, operand, scope, **kw):
    """Enumerate one operand and pair its first governed candidate with the operand for the checks."""
    result = _enumerate(conn, ctx, operand, scope, **kw)
    assert result.candidates, f"expected a governed candidate, got status={result.status}"
    return ResolvedOperandPathV1(operand=operand, candidate=result.candidates[0])


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def bridged_non_additive(db, service_actor, human_actor):
    """The Task-5 bridged topology (transactions -> VERIFIED bridge at account -> intra-wealth
    realization account->customer, landing wealth.customers) BUT the ``amount`` measure column is
    declared NON-ADDITIVE — so a ``sum`` over the many_to_one bridge fan-in is unsound."""
    ensure_upload_catalog_adapter()
    _seed(db, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core_banking", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("core_banking", "transactions", "amount", "numeric",
                      additivity="non_additive"), "monetary_flow"),
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
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def take_latest_topology(db, service_actor, human_actor):
    """The reuse-spike take_latest topology: core_banking.transactions carries a semi_additive
    monetary_stock (``balance``) + an ``as_of`` anchor. hop0 realizes transaction->account INTRA
    core_banking (the fan-in, with the anchor on the many-side rows before it), a same-entity
    REPOSITION bridge crosses ``account`` to wealth, hop1 realizes account->customer intra wealth.
    Landing = wealth.customers (VERIFIED grain)."""
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
    _seed_verified_bridge(db, "bfk_rep_acct", "account",
                          "core_banking", "public.accounts.account_id",
                          "wealth", "public.accounts.account_id")
    # every hop endpoint governed: source transactions, intermediate core_banking.accounts (the intra
    # fan-in target) + wealth.accounts (the reposition-bridge far side), landing wealth.customers
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "core_banking", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    return db, _scope("core_banking", "wealth")


# ── check_operand_path: aggregation soundness via reuse ─────────────────────────────────────────
def test_non_additive_sum_over_fan_in_is_aggregation_unsafe(bridged_non_additive):
    conn, scope = bridged_non_additive
    operand = _operand(aggregation=PathAggregation.sum, output_additivity=AdditivityClass.additive)
    ctx = _ctx(conn, ["core_banking", "wealth"],
               {("ms:op_0", "operand"): AggregationFunction.sum})
    path = _first_path(conn, ctx, operand, scope)

    temporal, hops, reason = check_operand_path(ctx, path)

    # the non-additive measure SUMmed over the many_to_one bridge fan-in is unsound
    assert reason is MultiSourceReason.aggregation_unsafe_on_path
    assert hops, "the fan-in hop produced at least one aggregation stage to inspect"


def test_take_latest_measure_with_bound_anchor_is_sound(take_latest_topology):
    conn, scope = take_latest_topology
    operand = _operand(
        object_ref="public.transactions.balance", concept="monetary_stock",
        semantic_role=SemanticRole.measure, aggregation=PathAggregation.take_latest,
        output_additivity=AdditivityClass.semi_additive, anchor_concept="as_of_date")
    ctx = _ctx(conn, ["core_banking", "wealth"],
               {("ms:op_0", "operand"): AggregationFunction.take_latest})
    path = _first_path(conn, ctx, operand, scope)

    temporal, hops, reason = check_operand_path(ctx, path)

    # the injected second need makes compile_temporal FIND the anchor; take_latest validates sound
    assert temporal.pit_anchor == "as_of_time"
    assert temporal.anchor_binding == "public.transactions.as_of"
    assert reason is None, "a take_latest measure with a proven ordering anchor is a sound path"


# ── check_paths_temporal_consistency: cross-path as-of coherence (pure) ─────────────────────────
_PB = ParamBindingV1(values=(), is_representative=True)


def _temporal(anchor, *, binding=None, codes=()):
    return TemporalDeclarationV1(
        pit_anchor=anchor, anchor_binding=binding, window=None, param_binding=_PB,
        time_axis_aggregating=False, reason_codes=codes)


def test_paths_with_incompatible_as_of_semantics_reject():
    as_of = _temporal("as_of_time", binding="core_banking.public.a.as_of")
    event = _temporal("event_time", binding="wealth.public.b.event_dt")

    assert (check_paths_temporal_consistency([as_of, event])
            is MultiSourceReason.temporal_paths_incompatible)


def test_paths_sharing_one_as_of_treatment_are_consistent():
    a = _temporal("as_of_time", binding="core_banking.public.a.as_of")
    b = _temporal("as_of_time", binding="wealth.public.b.as_of")

    assert check_paths_temporal_consistency([a, b]) is None


def test_individually_invalid_pit_treatment_rejects():
    valid = _temporal("as_of_time", binding="core_banking.public.a.as_of")
    ambiguous = _temporal(None, codes=(ReasonCode.temporal_anchor_ambiguous,))

    assert (check_paths_temporal_consistency([valid, ambiguous])
            is MultiSourceReason.temporal_paths_incompatible)


# ── check_time_slot_take_latest: A-owned TIME-slot ordering validation ──────────────────────────
def test_time_slot_take_latest_with_bound_anchor_validates(take_latest_topology):
    conn, scope = take_latest_topology
    # a RECENCY TIME slot: take the latest as_of date, ordered by the same as_of anchor concept
    operand = _operand(
        slot_id="time_0", object_ref="public.transactions.as_of", concept="as_of_date",
        semantic_role=SemanticRole.time, aggregation=PathAggregation.take_latest,
        output_additivity=AdditivityClass.not_applicable, anchor_concept="as_of_date")
    ctx = _ctx(conn, ["core_banking", "wealth"], {})
    path = _first_path(conn, ctx, operand, scope)

    # compile_aggregation stages MEASURE join_role only, so A's OWN check validates the TIME operand
    assert check_time_slot_take_latest(path) is None


def test_time_slot_take_latest_unbindable_anchor_is_ordering_anchor_missing(take_latest_topology):
    conn, scope = take_latest_topology
    # the ordering anchor concept has NO column on the source table -> unbindable
    operand = _operand(
        slot_id="time_0", object_ref="public.transactions.as_of", concept="as_of_date",
        semantic_role=SemanticRole.time, aggregation=PathAggregation.take_latest,
        output_additivity=AdditivityClass.not_applicable, anchor_concept="unbindable_anchor")
    ctx = _ctx(conn, ["core_banking", "wealth"], {})
    path = _first_path(conn, ctx, operand, scope)

    # must NOT silently pass (the single-source temporal_anchor_missing) — A rejects the operand
    assert (check_time_slot_take_latest(path)
            is MultiSourceReason.ordering_anchor_missing)

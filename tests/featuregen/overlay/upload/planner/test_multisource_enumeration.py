"""Phase 3C.2b-i-A · Task 5 — per-operand governed path enumeration (spec §5 steps 1-3, §8).

``enumerate_operand_paths`` drives the REUSED cross-catalog frontier (Task 1's
``run_operand_rollup`` engine: ``semantic_rollup_paths`` -> ``assemble_paths`` from a hand-built
``_Position``) for ONE operand, re-derives each resolved plan's landing ``(catalog, table_ref)``
from ``path_segments`` (mirroring ``check_connectivity``'s execution-table logic), and revalidates
that landing with the Task-4 ``governed_endpoint`` grain-fact check. An empty result is NEVER a
bare empty tuple — it carries ``no_governed_path`` (no VERIFIED-bridge path) or
``realization_endpoint_ungoverned`` (a path exists but its landing has no grain fact). Fail-closed.

Fixtures are seeded through the REAL governance write paths exactly as the reuse spike does
(``build_graph`` for the physical graph, ``entity_bridge_edge`` for VERIFIED bridges, the
``propose_fact``/``_confirm_grain`` four-eyes flow for VERIFIED grain facts).
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
    MAX_PATHS_PER_OPERAND,
    AdditivityClass,
    CatalogScopeV1,
)
from featuregen.overlay.upload.planner.multisource_assembly import (
    OperandEnumerationResultV1,
    enumerate_operand_paths,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
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
# key the source-endpoint revalidation (spec §2/§3.2) compares the binding's claimed grain against.
# Derived from ref+type exactly as ``governed_endpoint`` / the governance write path derive it.
_SRC_GRAIN_FK = fact_key(table_ref("core_banking", "transactions"), "grain")


# ── seed helpers (the sanctioned assembly-suite / reuse-spike pattern) ─────────────────────────
def _seed(db, source, rows_concepts):
    """Seed a catalog's physical graph through the REAL ingest graph builder."""
    rows = [r for r, _ in rows_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _seed_verified_bridge(db, fact_key, entity_id, lc, lref, rc, rref):
    """A VERIFIED cross-catalog bridge in the projection ``active_bridges`` reads."""
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fact_key, entity_id, lc, lref, rc, rref))


def _seed_verified_grain(db, source, table, columns, *, service_actor, human_actor):
    """A VERIFIED ``grain`` fact via the REAL governance write path (propose -> confirm -> drain)."""
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
        scope_id="ms-enum", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-19T00:00:00Z",
        catalog_consideration_truncated=False)


def _operand(*, slot_id="op_0", catalog="core_banking", object_ref="public.transactions.amount",
             concept="monetary_flow", source_entity="transaction",
             source_key_ref="public.transactions.transaction_id", grain_fact_key=_SRC_GRAIN_FK):
    """A minimal SUM/identity operand pinned to one measure column with a governed source binding.
    Enumeration re-derives + revalidates EVERY hop endpoint (source + intermediates + landing); the
    ``grain_fact_key`` defaults to the source table's REAL VERIFIED grain key so the source-endpoint
    check passes — a negative case overrides it to prove ``source_binding_ungoverned``."""
    return OperandSlotV1(
        slot_id=slot_id, semantic_role=SemanticRole.measure, catalog_source=catalog,
        object_ref=object_ref, authoritative_concept=concept,
        path_strategy=PathStrategyV1(
            aggregation=PathAggregation.sum, output_type="numeric",
            output_additivity=AdditivityClass.additive, external_type_required=False,
            ordering_anchor_concept=None),
        source_binding=GovernedSourceBindingV1(
            source_grain_entity=source_entity, source_grain_key_refs=(source_key_ref,),
            grain_fact_key=grain_fact_key))


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


def _enumerate(conn, *, operand, catalogs, scope, now=_NOW, target_entity="customer"):
    ctx = build_operand_context(conn, catalogs=catalogs, roles=("feature_engineer",), now=now,
                                agg_declarations={})
    return enumerate_operand_paths(
        conn, _adapter(), ctx, operand=operand, target_entity=target_entity, scope=scope,
        roles=("feature_engineer",), now=now)


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────
def _seed_bridged_graph(db):
    """The shared bridged topology GRAPH (no grain facts): core_banking.transactions -> VERIFIED
    bridge at ``account`` -> intra-wealth realization -> wealth.customers. Hop endpoints in path order
    are core_banking.transactions (source), wealth.accounts (intermediate), wealth.customers (landing).
    Callers layer the VERIFIED grain facts they want governed on top."""
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


@pytest.fixture
def bridged_governed(db, service_actor, human_actor):
    """core_banking.transactions (transaction grain, monetary_flow measure) reaches ``customer`` by
    CROSSING to wealth via a VERIFIED ``entity_bridge`` at ``account`` then an intra-wealth
    realization account -> customer. EVERY hop endpoint (source transactions, intermediate
    wealth.accounts, landing wealth.customers) carries a VERIFIED grain fact, so the path resolves."""
    _seed_bridged_graph(db)
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def bridged_ungoverned_landing(db, service_actor, human_actor):
    """The bridged topology with the source + intermediate governed BUT the landing wealth.customers
    has NO VERIFIED grain fact — a governed path resolves, its source binding is governed, but its
    LANDING endpoint is ungoverned -> ``realization_endpoint_ungoverned``."""
    _seed_bridged_graph(db)
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    # deliberately NO grain fact on wealth.customers (the landing)
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def bridged_ungoverned_intermediate(db, service_actor, human_actor):
    """The bridged topology with the source + landing governed BUT the INTERMEDIATE wealth.accounts
    (the bridge's far endpoint table) has NO VERIFIED grain fact — the SAME revalidation loop that
    checks the landing rejects a mid-path ungoverned endpoint -> ``realization_endpoint_ungoverned``."""
    _seed_bridged_graph(db)
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    # deliberately NO grain fact on wealth.accounts (the intermediate hop endpoint)
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def bridged_source_ungoverned(db, service_actor, human_actor):
    """The bridged topology with the intermediate + landing governed BUT the SOURCE
    core_banking.transactions has NO VERIFIED grain fact — the operand's ``source_binding`` claims a
    grain that isn't a real VERIFIED one -> ``source_binding_ungoverned``."""
    _seed_bridged_graph(db)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    # deliberately NO grain fact on core_banking.transactions (the source)
    return db, _scope("core_banking", "wealth")


@pytest.fixture
def no_bridge(db):
    """core_banking.transactions has an ``account``-keyed FK but NO VERIFIED bridge and NO
    intra-catalog realization off it — ``customer`` is unreachable. No governed path exists."""
    ensure_upload_catalog_adapter()
    _seed(db, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core_banking", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("core_banking", "transactions", "amount", "numeric"), "monetary_flow"),
    ])
    return db, _scope("core_banking")


@pytest.fixture
def bridge_fan(db, service_actor, human_actor):
    """A truncation stress fixture: NINE VERIFIED bridges (distinct fact_keys) at ``account`` all
    anchoring core_banking.transactions.account_id to the SAME wealth account table, then one
    intra-wealth realization to wealth.customers. The frontier's ``used_bridge_fact_keys`` cycle
    key makes each bridge a DISTINCT complete path — 9 governed paths, all landing on the one
    VERIFIED-grain wealth.customers. 9 > MAX_PATHS_PER_OPERAND=8 -> truncation."""
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
    for i in range(9):
        _seed_verified_bridge(db, f"bfk_acct_{i}", "account",
                              "core_banking", "public.transactions.account_id",
                              "wealth", "public.accounts.account_id")
    # every hop endpoint governed (source + intermediate + landing) so all 9 paths survive to truncate
    _seed_verified_grain(db, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(db, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    return db, _scope("core_banking", "wealth")


# ── tests ─────────────────────────────────────────────────────────────────────────────────────
def test_bridged_operand_yields_governed_landing_candidate(bridged_governed):
    conn, scope = bridged_governed
    result = _enumerate(conn, operand=_operand(), catalogs=["core_banking", "wealth"], scope=scope)

    assert isinstance(result, OperandEnumerationResultV1)
    assert result.status is MultiSourceReason.resolved
    assert result.candidates, "the bridged operand must yield >=1 governed candidate"
    cand = result.candidates[0]
    # the resolved cross-catalog plan crosses catalogs via the VERIFIED bridge
    assert cand.binding_plan.participating_catalogs == ("core_banking", "wealth")
    # the landing is re-derived from path_segments (frontier never emits it)
    assert (cand.landing_catalog, cand.landing_table_ref) == ("wealth", "public.customers")
    # and revalidated against the VERIFIED grain fact
    assert isinstance(cand.landing_endpoint, GovernedEndpointV1)
    assert cand.landing_endpoint.grain_key_refs == ("public.customers.customer_id",)
    # EVERY hop endpoint is carried + governed, in path order: source, intermediate, landing
    assert tuple((e.catalog, e.table_ref) for e in cand.governed_endpoints) == (
        ("core_banking", "public.transactions"),
        ("wealth", "public.accounts"),
        ("wealth", "public.customers"))
    assert cand.governed_endpoints[0].grain_fact_key == _SRC_GRAIN_FK   # the source endpoint
    assert cand.governed_endpoints[-1] is cand.landing_endpoint         # landing is the last hop
    assert not result.bounds.paths_per_operand_truncated


def test_no_verified_bridge_path_is_no_governed_path(no_bridge):
    conn, scope = no_bridge
    result = _enumerate(conn, operand=_operand(), catalogs=["core_banking"], scope=scope)

    assert result.candidates == ()          # never a bare empty tuple...
    assert result.status is MultiSourceReason.no_governed_path
    assert MultiSourceReason.no_governed_path in result.reason_codes


def test_path_with_ungoverned_landing_is_realization_endpoint_ungoverned(
        bridged_ungoverned_landing):
    conn, scope = bridged_ungoverned_landing
    result = _enumerate(conn, operand=_operand(), catalogs=["core_banking", "wealth"], scope=scope)

    # a governed path resolves, source + intermediate are governed, but its landing has no grain
    # fact -> classified, not empty-silent
    assert result.candidates == ()
    assert result.status is MultiSourceReason.realization_endpoint_ungoverned
    assert MultiSourceReason.realization_endpoint_ungoverned in result.reason_codes


def test_ungoverned_intermediate_endpoint_is_realization_endpoint_ungoverned(
        bridged_ungoverned_intermediate):
    conn, scope = bridged_ungoverned_intermediate
    result = _enumerate(conn, operand=_operand(), catalogs=["core_banking", "wealth"], scope=scope)

    # source + landing governed, but the INTERMEDIATE wealth.accounts (the bridge's far endpoint)
    # has no grain fact -> the every-hop revalidation loop rejects the same way a bad landing does
    assert result.candidates == ()
    assert result.status is MultiSourceReason.realization_endpoint_ungoverned
    assert MultiSourceReason.realization_endpoint_ungoverned in result.reason_codes


def test_ungoverned_source_endpoint_is_source_binding_ungoverned(bridged_source_ungoverned):
    conn, scope = bridged_source_ungoverned
    result = _enumerate(conn, operand=_operand(), catalogs=["core_banking", "wealth"], scope=scope)

    # a governed path resolves and the intermediate + landing are governed, but the SOURCE table has
    # no VERIFIED grain fact -> the operand's source binding is ungoverned
    assert result.candidates == ()
    assert result.status is MultiSourceReason.source_binding_ungoverned
    assert MultiSourceReason.source_binding_ungoverned in result.reason_codes


def test_source_grain_fact_key_mismatch_is_source_binding_ungoverned(bridged_governed):
    conn, scope = bridged_governed
    # EVERY hop endpoint is governed, but the binding CLAIMS a grain fact_key that is not the source's
    # actual VERIFIED one -> A validates the claim and rejects (structure trusted, claim validated).
    operand = _operand(grain_fact_key="not-the-real-source-grain-fk")
    result = _enumerate(conn, operand=operand, catalogs=["core_banking", "wealth"], scope=scope)

    assert result.candidates == ()
    assert result.status is MultiSourceReason.source_binding_ungoverned
    assert MultiSourceReason.source_binding_ungoverned in result.reason_codes


def test_truncation_at_max_paths_per_operand_sets_the_bound(bridge_fan):
    conn, scope = bridge_fan
    result = _enumerate(conn, operand=_operand(), catalogs=["core_banking", "wealth"], scope=scope)

    assert result.bounds.paths_per_operand_truncated is True
    assert len(result.candidates) == MAX_PATHS_PER_OPERAND
    assert MultiSourceReason.budget_truncated in result.reason_codes
    # every kept candidate still lands on the one VERIFIED-grain landing
    assert all((c.landing_catalog, c.landing_table_ref) == ("wealth", "public.customers")
               for c in result.candidates)

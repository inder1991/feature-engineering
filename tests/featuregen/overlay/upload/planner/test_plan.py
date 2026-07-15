from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import (
    PLAN_CONTRACT_VERSION,
    PathResolutionStatus,
    PlanResolutionStatus,
    ReplayStrength,
)
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import Need, Template

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _catalog(db, source):
    catalog = [
        (CanonicalRow(source, "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(source, "accounts", "balance", "numeric", additivity="semi_additive", currency="USD"),
         "monetary_stock")]
    build_graph(db, source, [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
               "VALUES (%s, %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
               (source, _NOW, _NOW))


def _tmpl(stock_grains: tuple[str, ...] = ()):
    return Template(id="t_bal", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock",
                                allowed_source_grains=stock_grains),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M", use_cases=(),
                    pit="trailing")


def test_plan_bindings_resolves_a_single_catalog_plan(db):
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    assert result.selected_plan_id is not None
    sel = next(p for p in result.candidate_plans if p.plan_id == result.selected_plan_id)
    assert sel.catalog_source == "core"
    assert {b.bound_object_ref for b in sel.ingredient_bindings} == {"public.accounts.balance",
                                                                     "public.accounts.customer_id"}
    # 3B.3b derived fields on a tier-1 plan (from the canonical make_binding_plan constructor)
    assert sel.participating_catalogs == ("core",) and sel.bridge_count == 0
    assert sel.path_resolution_status is PathResolutionStatus.ingredient_binding_only
    assert result.replay_envelope.replay_strength is ReplayStrength.conditional   # watermark stamps, not a snapshot
    assert result.replay_envelope.planner_input_hash
    assert result.replay_envelope.plan_contract_version == PLAN_CONTRACT_VERSION
    assert result.replay_envelope.active_bridge_fact_keys == ()   # no VERIFIED bridge exists -> empty pin
    # source==target (customer) is EXACT: the tier-1 binding is already AT target grain, no assembler ran
    assert result.bounding.frontier_states_truncated is False
    assert result.bounding.total_states_expanded == 0


def test_no_authorized_catalog_is_not_applicable(db):
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)   # nothing seeded
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.not_applicable


def test_rejected_alternative_does_not_downgrade_a_resolved_result(db):
    # two catalogs, and the stock need constrained to the customer grain: 'core' binds cleanly (its
    # accounts table IS customer-grain); 'bad' has NO grain column, so its stock candidate is
    # grain_incompatible and its only plan is genuinely non-resolved. Candidate-local-first: the clean
    # 'core' plan wins AND the rejected 'bad' alternative is preserved, never dropped.
    _catalog(db, "core")
    bad = [(CanonicalRow("bad", "accounts", "customer_id", "integer"), "customer_id"),  # NOT a grain column
           (CanonicalRow("bad", "accounts", "amt", "numeric"), "monetary_stock"),
           (CanonicalRow("bad", "accounts", "amt2", "numeric"), "outcome_label")]  # noise, not bound
    build_graph(db, "bad", [r for r, _ in bad], concepts={content_hash(r): c for r, c in bad})
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
               "VALUES ('bad', %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
               (_NOW, _NOW))
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_bindings(db, template=_tmpl(stock_grains=("customer",)), target_entity="customer",
                           scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved   # the clean 'core' plan wins
    sel = next(p for p in result.candidate_plans if p.plan_id == result.selected_plan_id)
    assert sel.catalog_source == "core"
    # …and the rejected alternative from 'bad' is PRESENT and non-resolved (preserved, not dropped).
    bad_plans = [p for p in result.candidate_plans if p.catalog_source == "bad"]
    assert bad_plans, "the rejected 'bad' alternative must be preserved in candidate_plans"
    assert all(p.resolution_status is not PlanResolutionStatus.resolved for p in bad_plans)


# ---------------------------------------------------------------------------------------------
# Task B5 acceptance — the 3B.3b assembler wired into plan_bindings as a LOG-ONLY enrichment.
# Fixtures use REAL registry data (transaction_id -> entity transaction, account_id -> account;
# transaction->account is DERIVABLE in ENTITY_GRAPH); bridge endpoints are COLUMN refs; the scope
# is constructed directly so the in/out-of-scope catalog split is exact.
# ---------------------------------------------------------------------------------------------
from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.planner.contracts import CatalogScopeV1, ReasonCode


def _scope(*catalogs: str) -> CatalogScopeV1:
    return CatalogScopeV1(
        scope_id="s3b5", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-15T00:00:00Z",
        catalog_consideration_truncated=False)


def _seed(db, source, catalog):
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})


def _seed_bridge(db, fact_key, entity_id, left_cat, left_ref, right_cat, right_ref):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fact_key, entity_id, left_cat, left_ref, right_cat, right_ref))


def _txn_template(extra_needs: tuple = ()):
    """A transaction-grain-source recipe. join_role is EXPLICIT because a test template is not in the
    corpus registry, so the tier-1 binding's join_role falls back to the Need's own field — the wire
    site matches bindings on join_role == source_entity_key."""
    return Template(id="t_roll", family="f", intent="i",
                    needs=(Need(role="txn", concept="transaction_id",
                                join_role=JoinRole.SOURCE_ENTITY_KEY),) + tuple(extra_needs),
                    params={}, aggregation="sum", additivity="additive", explain="M", use_cases=(),
                    pit="trailing", source_entity_need_role="txn")


def _split(db):
    """ops holds the transaction-grain table (an account FK column, NO intra-catalog accounts join);
    rev holds the account-grain landing table — the roll-up completes ONLY over a verified bridge."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])


def test_acceptance_rollup_bridge_end_to_end(db):
    _split(db)
    _seed_bridge(db, "bfk_e2e", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    # candidate-local-first: the tier-1 outcome is untouched by the enrichment
    assert result.result_status is PlanResolutionStatus.resolved
    sel = next(p for p in result.candidate_plans if p.plan_id == result.selected_plan_id)
    assert sel.path_resolution_status is PathResolutionStatus.ingredient_binding_only
    # ...and the governed source->target roll-up IS in the candidate set (logged for 3B.4)
    cross = [p for p in result.candidate_plans
             if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]
    assert len(cross) == 1
    p = cross[0]
    assert p.bridge_count == 1 and p.participating_catalogs == ("ops", "rev")
    assert any(s.bridge_fact_key == "bfk_e2e" for s in p.path_segments)
    # the replay envelope pins the exact governed crossing set the run could see
    assert result.replay_envelope.active_bridge_fact_keys == ("bfk_e2e",)
    assert result.bounding.total_states_expanded > 0


def test_acceptance_zero_bridge_rollup_intra_catalog(db):
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("core"), roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    cross = [p for p in result.candidate_plans
             if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]
    assert len(cross) == 1
    assert cross[0].bridge_count == 0 and cross[0].participating_catalogs == ("core",)
    assert result.replay_envelope.active_bridge_fact_keys == ()


def test_acceptance_multi_grain_recipe_records_reject_and_skips_assembler(db):
    _split(db)
    # a REQUIRED second-entity need (customer grain, distinct from the transaction source grain)
    tmpl = _txn_template(extra_needs=(Need(role="cust", concept="customer_id"),))
    result = plan_bindings(db, template=tmpl, target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    assert ReasonCode.unsupported_multi_grain_ingredients in result.reason_codes
    # the assembler never ran: no source->target plans minted, zero states expanded
    assert all(p.path_resolution_status is PathResolutionStatus.ingredient_binding_only
               for p in result.candidate_plans)
    assert result.bounding.total_states_expanded == 0


def test_acceptance_plan_bindings_is_deterministic(db):
    _split(db)
    _seed_bridge(db, "bfk_det", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    scope = _scope("ops", "rev")
    r1 = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                       roles=(), now=_NOW)
    r2 = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                       roles=(), now=_NOW)
    assert [p.plan_id for p in r1.candidate_plans] == [p.plan_id for p in r2.candidate_plans]
    assert r1.replay_envelope == r2.replay_envelope


def test_acceptance_out_of_scope_bridge_is_never_pinned_or_crossed(db):
    _split(db)
    _seed(db, "hidden", [
        (CanonicalRow("hidden", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    # a VERIFIED bridge whose far endpoint catalog is NOT in the frozen scope
    _seed_bridge(db, "bfk_hidden", "account",
                 "ops", "public.transactions.account_id", "hidden", "public.accounts.account_id")
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    # fail-closed: the crossing is neither pinned on the envelope nor used by any candidate plan
    assert result.replay_envelope.active_bridge_fact_keys == ()
    assert all(s.bridge_fact_key != "bfk_hidden"
               for p in result.candidate_plans for s in p.path_segments)
    assert all("hidden" not in p.participating_catalogs for p in result.candidate_plans)
    # the roll-up fail-closes as a REJECTED candidate without revealing the inaccessible catalog
    rejects = [p for p in result.candidate_plans
               if p.path_resolution_status is PathResolutionStatus.source_to_target_rejected]
    assert rejects and all(p.primary_reason_code is ReasonCode.missing_realization for p in rejects)
    assert result.result_status is PlanResolutionStatus.resolved   # tier-1 untouched

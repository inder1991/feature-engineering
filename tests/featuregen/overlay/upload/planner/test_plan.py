from datetime import UTC, datetime

from tests.featuregen.overlay.upload._bridge_fixtures import (
    seed_verified_bridge as _seed_verified_bridge_fact,
)

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import (
    PLAN_CONTRACT_VERSION,
    PathResolutionStatus,
    PlanResolutionStatus,
    ReplayStrength,
    full_physical_plan_hash,
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
    sel = next(p for p in result.candidate_plans if p.physical_plan_id == result.selected_plan_id)
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
    sel = next(p for p in result.candidate_plans if p.physical_plan_id == result.selected_plan_id)
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
    _seed_verified_bridge_fact(
        db, fact_key, entity=entity_id, left_source=left_cat, left_ref=left_ref,
        right_source=right_cat, right_ref=right_ref)


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
    sel = next(p for p in result.candidate_plans if p.physical_plan_id == result.selected_plan_id)
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
    assert [p.physical_plan_id for p in r1.candidate_plans] == [p.physical_plan_id for p in r2.candidate_plans]
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


# ---------------------------------------------------------------------------------------------
# Task C8 — the shadow contract-compile pass wired into plan_bindings: batched CompilerContext,
# the run-owned CompileBudget, and the contract selection roll-up. compile_ctx=None must stay
# byte-identical to pre-C8 behaviour (all plans not_compiled, no roll-up, zero extra reads).
# ---------------------------------------------------------------------------------------------

from featuregen.overlay.upload.planner.contracts import ContractResolutionStatus
from featuregen.overlay.upload.planner.declarations import CompileBudget, build_compiler_context


def _freshness(db, *sources, head_seq=1):
    event_head = db.execute("SELECT COALESCE(max(global_seq), 0) FROM events").fetchone()[0]
    applied_head = max(head_seq, event_head)
    for src in sources:
        db.execute(
            "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id,"
            " head_seq) VALUES (%s,%s,'c8',%s) ON CONFLICT (catalog_source) DO UPDATE SET"
            " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq",
            (src, _NOW, applied_head))
    db.execute(
        "INSERT INTO projection_checkpoints (projection_name, checkpoint_seq) VALUES"
        " ('overlay', %s) ON CONFLICT (projection_name) DO UPDATE SET"
        " checkpoint_seq = EXCLUDED.checkpoint_seq", (applied_head,))


def _c8_fixture(db):
    """The B5 bridge roll-up fixture made compile-ready: fresh watermarks + an applied projection
    checkpoint, so a compiled contract's freshness axis resolves."""
    _split(db)
    _seed_bridge(db, "bfk_c8", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    _freshness(db, "ops", "rev")
    return _scope("ops", "rev")


def _cross(result):
    return [p for p in result.candidate_plans
            if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]


def test_compile_pass_is_additive_and_rolls_up_contract_selection(db):
    scope = _c8_fixture(db)
    tmpl = _txn_template()
    base = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(),
                         now=_NOW)
    ctx = build_compiler_context(db, scope, (), _NOW)
    result = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(),
                           now=_NOW, compile_ctx=ctx)
    # tier-1 decision + plan order + physical ids: identical to the uncompiled run
    assert result.result_status is base.result_status
    assert result.selected_plan_id == base.selected_plan_id
    assert [p.physical_plan_id for p in result.candidate_plans] == \
        [p.physical_plan_id for p in base.candidate_plans]
    (cross,) = _cross(result)
    assert cross.contract_resolution_status is ContractResolutionStatus.resolved
    assert cross.contract_id is not None
    # tier-1 (non source->target) plans are NEVER compiled
    assert all(p.contract_resolution_status is ContractResolutionStatus.not_compiled
               for p in result.candidate_plans
               if p.path_resolution_status is not PathResolutionStatus.source_to_target_resolved)
    # the roll-up selects the best COMPILED plan; the tier-1 selection is a different axis
    assert result.selected_contract_physical_plan_id == cross.physical_plan_id
    assert result.selected_contract_id == cross.contract_id
    assert result.contract_result_status is ContractResolutionStatus.resolved
    assert result.selected_contract_physical_plan_id != result.selected_plan_id
    # the uncompiled run has NO roll-up
    assert base.contract_result_status is ContractResolutionStatus.not_compiled
    assert base.selected_contract_physical_plan_id is None and base.selected_contract_id is None


def test_compile_budget_is_shared_and_exhaustion_is_recorded(db):
    scope = _c8_fixture(db)
    tmpl = _txn_template()
    ctx = build_compiler_context(db, scope, (), _NOW)
    budget = CompileBudget(remaining=1, deadline_monotonic=1e9, clock=lambda: 0.0)  # count governs
    r1 = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(),
                       now=_NOW, compile_ctx=ctx, budget=budget)
    r2 = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(),
                       now=_NOW, compile_ctx=ctx, budget=budget)
    (c1,) = _cross(r1)
    assert c1.contract_resolution_status is ContractResolutionStatus.resolved
    assert budget.remaining == 0
    (c2,) = _cross(r2)
    assert c2.contract_resolution_status is ContractResolutionStatus.not_compiled
    assert c2.contract_id is None
    assert ReasonCode.compile_budget_exhausted in c2.contract_reason_codes
    # a budget-skipped plan is NEVER the contract selection
    assert r2.contract_result_status is ContractResolutionStatus.not_compiled
    assert r2.selected_contract_physical_plan_id is None
    # the elapsed-time deadline skips too (D6: the injected monotonic clock is already past it)
    past = CompileBudget(remaining=5, deadline_monotonic=0.0, clock=lambda: 1.0)
    r3 = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(),
                       now=_NOW, compile_ctx=ctx, budget=past)
    (c3,) = _cross(r3)
    assert c3.contract_resolution_status is ContractResolutionStatus.not_compiled
    assert ReasonCode.compile_budget_exhausted in c3.contract_reason_codes
    assert past.remaining == 5           # nothing was compiled, nothing decremented
    assert past.stopped_by_time is True  # the time bound (not the count) is what fired


def test_no_compile_ctx_leaves_every_plan_not_compiled(db):
    scope = _c8_fixture(db)
    result = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                           roles=(), now=_NOW)
    assert result.contract_result_status is ContractResolutionStatus.not_compiled
    assert result.selected_contract_physical_plan_id is None
    assert result.selected_contract_id is None
    assert all(p.contract_resolution_status is ContractResolutionStatus.not_compiled
               and p.contract_id is None and p.contract_reason_codes == ()
               for p in result.candidate_plans)


# ---------------------------------------------------------------------------------------------
# Task S1A-4a — the PLAN FACTS a governed option builder consumes.
#
# The plan's OUTPUT grain cannot be rediscovered from the ingredient bindings: a
# transaction -> account roll-up carries no account-key ingredient binding at all, and the landing
# catalog is not the plan's `catalog_source`. It is therefore emitted, qualified, at the one place
# that knows it — the assembler's completing mint. And `full_physical_plan_hash` exposes the
# UNTRUNCATED digest of exactly the material `make_binding_plan` truncates into `physical_plan_id`,
# from ONE shared material builder so the two can never drift apart.
# ---------------------------------------------------------------------------------------------

# Identity pins captured on the PRE-change checkout (task S1A-4a), by running the `_c8_fixture`
# compile flow below before `output_grain_ref` / `anchor_catalog_source` existed. Both new fields
# are non-identity-bearing: neither may move a physical plan id or a contract id.
_PINNED_C8_CROSS_PHYSICAL_PLAN_ID = "bp_0adcb0a8c5748e1a"
_PINNED_C8_CROSS_CONTRACT_ID = "cc_095c221534f53e67"


def test_resolved_cross_catalog_plan_carries_a_qualified_output_grain(db):
    _split(db)
    _seed_bridge(db, "bfk_grain", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    (cross,) = _cross(result)
    # the roll-up LANDS in 'rev' on the account-grain table: the output grain is qualified by the
    # LANDING catalog — not the plan's source catalog, and not derivable from the bindings.
    assert cross.output_grain_ref == ("rev", "public.accounts.account_id")
    assert cross.catalog_source == "ops"
    assert all(b.bound_catalog_source == "ops" for b in cross.ingredient_bindings)
    assert all(b.bound_object_ref != "public.accounts.account_id"
               for b in cross.ingredient_bindings)
    # the tier-1 candidates never reached the assembler, so they claim no output grain
    assert all(p.output_grain_ref is None for p in result.candidate_plans
               if p.path_resolution_status is PathResolutionStatus.ingredient_binding_only)


def test_zero_bridge_rollup_output_grain_is_its_own_catalog(db):
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("core"), roles=(), now=_NOW)
    (cross,) = _cross(result)
    assert cross.bridge_count == 0
    assert cross.output_grain_ref == ("core", "public.accounts.account_id")


def test_rejected_plan_claims_no_output_grain(db):
    _split(db)
    _seed(db, "hidden", [
        (CanonicalRow("hidden", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    # the only crossing to an account-grain table lands in a catalog OUTSIDE the frozen scope, so
    # the roll-up fail-closes as a rejected candidate — which reaches no target grain to report.
    _seed_bridge(db, "bfk_hidden_grain", "account",
                 "ops", "public.transactions.account_id", "hidden", "public.accounts.account_id")
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    rejects = [p for p in result.candidate_plans
               if p.path_resolution_status is PathResolutionStatus.source_to_target_rejected]
    assert rejects
    assert all(p.output_grain_ref is None for p in rejects)


def test_full_physical_plan_hash_is_the_untruncated_physical_plan_id(db):
    scope = _c8_fixture(db)
    result = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                           roles=(), now=_NOW)
    (cross,) = _cross(result)
    full = full_physical_plan_hash(cross)
    assert len(full) == 64 and full == full.lower()
    assert full[:16] == cross.physical_plan_id[len("bp_"):]
    # ONE material, two readers: it holds for every plan the run minted, tier-1 and cross alike
    for p in result.candidate_plans:
        assert full_physical_plan_hash(p)[:16] == p.physical_plan_id[len("bp_"):]


def test_new_plan_facts_move_no_identity(db):
    """The pinned literals were captured BEFORE either new field existed."""
    scope = _c8_fixture(db)
    ctx = build_compiler_context(db, scope, (), _NOW)
    result = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                           roles=(), now=_NOW, compile_ctx=ctx)
    (cross,) = _cross(result)
    assert cross.physical_plan_id == _PINNED_C8_CROSS_PHYSICAL_PLAN_ID
    assert cross.contract_id == _PINNED_C8_CROSS_CONTRACT_ID
    # ...and the un-hashed fact IS populated on that very plan (a vacuous pin would prove nothing)
    assert cross.output_grain_ref == ("rev", "public.accounts.account_id")
    assert full_physical_plan_hash(cross)[:16] == _PINNED_C8_CROSS_PHYSICAL_PLAN_ID[len("bp_"):]
    # The temporal half of this pin is deliberately NOT claimed here: `_txn_template` declares no
    # temporal need, so this plan binds no anchor and `anchor_catalog_source` is "". Stating that
    # plainly rather than implying the contract_id pin covers a populated anchor — the populated
    # case is proved directly by
    # `test_declarations.py::test_anchor_catalog_source_is_not_contract_identity_material`.
    assert cross.temporal_declaration is not None
    assert cross.temporal_declaration.anchor_binding is None
    assert cross.temporal_declaration.anchor_catalog_source == ""


# ---------------------------------------------------------------------------------------------
# Task S1B-2 — the TYPED UNMET HOP a rejected plan carries out.
#
# At the frontier's dead end the assembler holds the failing `EntityRelationshipRefV1`, the exact
# `_Position`, and (inside the taxonomy probe) the realizing catalogs with their endpoint key
# columns. Before this task all of it was discarded and only a reason-code string escaped, so the
# demand ledger (S1B-1) had nothing to record. `unmet_hop` carries it — on REJECTED plans only, as
# a defaulted, never-hashed field.
# ---------------------------------------------------------------------------------------------

from featuregen.overlay.upload.planner import assembly as _assembly


def _far_realizer_split(db):
    """`ops` holds the transaction-grain table with an account FK and NO intra-catalog join; `rev`
    holds a transactions table that DOES declare the transaction -> account join (so `rev` VALIDLY
    realizes the hop) plus the account-grain landing table. NO bridge is seeded, so the roll-up
    from `ops` dead-ends while a realizer demonstrably exists one catalog away — the exact shape
    the bridge-demand queue exists to surface."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("rev", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])


def _rejects(result):
    return [p for p in result.candidate_plans
            if p.path_resolution_status is PathResolutionStatus.source_to_target_rejected]


def test_unsanctioned_bridge_reject_carries_the_hop_and_its_far_realizer(db):
    _far_realizer_split(db)
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    ops_rejects = [p for p in _rejects(result) if p.catalog_source == "ops"]
    assert len(ops_rejects) == 1
    reject = ops_rejects[0]
    assert reject.primary_reason_code is ReasonCode.unsanctioned_bridge
    hop = reject.unmet_hop
    assert hop is not None
    # the semantic hop, verbatim from the governed registry entry the frontier was traversing
    assert hop.relationship_id == "transaction_to_account"
    assert hop.relationship_version
    assert (hop.from_entity, hop.to_entity) == ("transaction", "account")
    assert hop.cardinality == "many_to_one"
    # the EXACT physical position the search died on
    assert (hop.position_entity, hop.position_catalog, hop.position_table_ref) == (
        "transaction", "ops", "public.transactions")
    assert hop.hop_index == 0
    assert hop.verdict == ReasonCode.unsanctioned_bridge.value
    # ...and the realizer that makes this a BRIDGE demand rather than a realization gap: the far
    # catalog plus the two key columns a bridge would have to connect.
    assert len(hop.realizers) == 1
    (realizer,) = hop.realizers
    assert realizer.catalog_source == "rev"
    assert realizer.to_object_ref == "public.accounts"
    assert realizer.from_key_ref == "public.transactions.account_id"
    assert realizer.to_key_ref == "public.accounts.account_id"
    # the near-side key column a bridge would anchor on, resolved AT the refusal site
    assert hop.near_side_key_refs == ("public.transactions.account_id",)


def test_missing_realization_reject_carries_the_hop_with_no_realizers(db):
    """The measured verdict for the plain `_split` seeds with no bridge: `rev` holds ONLY the
    account-grain table, so no catalog anywhere realizes transaction -> account and the honest
    verdict is `missing_realization` with an EMPTY realizer tuple — a realization gap, not
    somebody's unbuilt bridge."""
    _split(db)
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    (reject,) = _rejects(result)
    assert reject.primary_reason_code is ReasonCode.missing_realization
    hop = reject.unmet_hop
    assert hop is not None
    assert hop.verdict == ReasonCode.missing_realization.value
    assert hop.realizers == ()
    assert hop.relationship_id == "transaction_to_account"
    assert hop.near_side_key_refs == ("public.transactions.account_id",)


def test_a_resolved_plan_carries_no_unmet_hop(db):
    _split(db)
    _seed_bridge(db, "bfk_unmet_none", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    (cross,) = _cross(result)
    assert cross.unmet_hop is None                      # it crossed: nothing went unmet
    assert all(p.unmet_hop is None for p in result.candidate_plans
               if p.path_resolution_status is PathResolutionStatus.ingredient_binding_only)


def _diamond(db):
    """Four transaction-grain catalogs bridged as a diamond (ops-m1-hub, ops-m2-hub) at the
    TRANSACTION entity, with nothing anywhere realizing transaction -> account. Only `ops` carries
    the measure the recipe needs, so exactly ONE frontier search runs — and it reaches `hub` twice,
    by two different two-bridge routes, giving two dead ends on the SAME table within ONE
    `assemble_paths` call. That is the shape the near-side cache exists for."""
    for src in ("ops", "m1", "m2", "hub"):
        rows = [
            (CanonicalRow(src, "transactions", "transaction_id", "integer", is_grain=True),
             "transaction_id"),
            (CanonicalRow(src, "transactions", "account_id", "integer"), "account_id"),
        ]
        if src == "ops":
            rows.append((CanonicalRow(src, "transactions", "amount", "numeric",
                                      additivity="additive", currency="USD"), "monetary_flow"))
        _seed(db, src, rows)
    for key, left, right in (("bfk_dx", "ops", "m1"), ("bfk_dy", "ops", "m2"),
                             ("bfk_dz1", "m1", "hub"), ("bfk_dz2", "m2", "hub")):
        _seed_bridge(db, key, "transaction", left, "public.transactions.transaction_id",
                     right, "public.transactions.transaction_id")
    return _txn_template(extra_needs=(Need(role="amt", concept="monetary_flow"),))


def test_repeated_dead_ends_on_one_table_walk_its_columns_once(db, monkeypatch):
    tmpl = _diamond(db)
    calls: list[tuple[str, str]] = []
    real = _assembly._table_columns

    def counted(conn, catalog, table_ref):
        calls.append((catalog, table_ref))
        return real(conn, catalog, table_ref)

    monkeypatch.setattr(_assembly, "_table_columns", counted)
    result = plan_bindings(db, template=tmpl, target_entity="account",
                           scope=_scope("ops", "m1", "m2", "hub"), roles=(), now=_NOW)
    hub_rejects = [p for p in _rejects(result)
                   if p.unmet_hop is not None and p.unmet_hop.position_catalog == "hub"]
    assert len(hub_rejects) == 2, "two distinct two-bridge routes both dead-end on hub"
    assert len({p.physical_plan_id for p in hub_rejects}) == 2   # two REAL plans, not one twice
    for p in hub_rejects:
        assert p.primary_reason_code is ReasonCode.bounded_out_max_bridges
        assert p.unmet_hop.verdict == ReasonCode.bounded_out_max_bridges.value
        assert p.unmet_hop.near_side_key_refs == ("public.transactions.account_id",)
    # THE PIN. `hub.public.transactions` is read once per expanded hub state by
    # `reposition_bridges` (2 states) plus ONCE by the near-side walk — 3, not 4. Without the
    # cache the second dead end would walk the same table again.
    assert calls.count(("hub", "public.transactions")) == 3


def test_an_expired_compile_budget_skips_the_near_side_walk(db):
    """The near-side walk is a governed `key_entity` read per column on a hot path, so it consults
    the run's compile budget first. Past the deadline the hop is still carried — relationship,
    position, verdict and realizers are all free — and only the walked evidence is honestly
    absent."""
    _far_realizer_split(db)
    past = CompileBudget(remaining=5, deadline_monotonic=0.0, clock=lambda: 1.0)
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW, budget=past)
    (reject,) = [p for p in _rejects(result) if p.catalog_source == "ops"]
    hop = reject.unmet_hop
    assert hop is not None
    assert hop.relationship_id == "transaction_to_account"
    assert hop.realizers and hop.realizers[0].catalog_source == "rev"
    assert hop.near_side_key_refs == ()
    # the walk skip is NOT a compile-budget stop: it must not claim the run's truncation reason
    assert past.stopped_by_time is None and past.remaining == 5


def test_the_unmet_hop_moves_no_identity(db):
    """The S1A-4a literal pins, re-run with `unmet_hop` in the world: a defaulted plan field that
    never enters `_physical_plan_material` or `make_contract_id`."""
    scope = _c8_fixture(db)
    ctx = build_compiler_context(db, scope, (), _NOW)
    result = plan_bindings(db, template=_txn_template(), target_entity="account", scope=scope,
                           roles=(), now=_NOW, compile_ctx=ctx)
    (cross,) = _cross(result)
    assert cross.physical_plan_id == _PINNED_C8_CROSS_PHYSICAL_PLAN_ID
    assert cross.contract_id == _PINNED_C8_CROSS_CONTRACT_ID
    assert full_physical_plan_hash(cross)[:16] == _PINNED_C8_CROSS_PHYSICAL_PLAN_ID[len("bp_"):]


def test_the_same_reject_keeps_its_id_whether_or_not_the_hop_is_carried(db):
    """Non-vacuous half of the pin above: a plan that DOES carry a populated `unmet_hop` mints the
    identical physical id as the same plan with the field cleared."""
    from dataclasses import replace as _replace

    from featuregen.overlay.upload.planner.contracts import make_binding_plan
    _far_realizer_split(db)
    result = plan_bindings(db, template=_txn_template(), target_entity="account",
                           scope=_scope("ops", "rev"), roles=(), now=_NOW)
    (reject,) = [p for p in _rejects(result) if p.catalog_source == "ops"]
    assert reject.unmet_hop is not None
    bare = make_binding_plan(
        recipe_id=reject.recipe_id, target_entity=reject.target_entity,
        catalog_source=reject.catalog_source, ingredient_bindings=reject.ingredient_bindings,
        path_segments=reject.path_segments, resolution_status=reject.resolution_status,
        path_resolution_status=reject.path_resolution_status,
        primary_reason_code=reject.primary_reason_code, reason_codes=reject.reason_codes,
        safety=reject.safety, preference_rank=reject.preference_rank,
        preference_reasons=reject.preference_reasons, candidate_role=reject.candidate_role)
    assert bare.unmet_hop is None
    assert bare.physical_plan_id == reject.physical_plan_id
    assert _replace(reject, unmet_hop=None) == bare

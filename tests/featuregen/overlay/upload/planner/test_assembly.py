# NOTE: fixtures use REAL concepts/entities verified against the registry + graph:
#   transaction_id -> entity_link "transaction"; customer_id -> "customer"; monetary_flow is entity-neutral.
#   "transaction"->"account" is DERIVABLE and "account"->"account" is EXACT in ENTITY_GRAPH (38 entities).
#   EntityCompatibility members are UPPERCASE (EXACT/DERIVABLE/AMBIGUOUS/UNKNOWN).
from featuregen.overlay.upload.planner.assembly import ingredient_eligibility, semantic_rollup_paths
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.templates import Need, Template


def _tmpl(needs, *, source_entity_need_role=None):
    return Template(id="t3b3b", family="f", intent="i", needs=needs, params={}, aggregation="sum",
                    additivity="additive", explain="M", use_cases=(), pit="trailing",
                    source_entity_need_role=source_entity_need_role)


def test_single_source_entity_eligible():
    # a lone transaction-grain key -> source resolves to 'transaction'; nothing gates
    t = _tmpl((Need(role="txn", concept="transaction_id"),))
    e = ingredient_eligibility(t)
    assert e.eligible is True and e.source_entity == "transaction"


def test_multi_grain_ingredient_rejected():
    # source anchored on the transaction key; a REQUIRED customer-grain need is a second grain -> rejected
    t = _tmpl((Need(role="txn", concept="transaction_id"), Need(role="cust", concept="customer_id")),
              source_entity_need_role="txn")
    e = ingredient_eligibility(t)
    assert e.eligible is False and e.reason is ReasonCode.unsupported_multi_grain_ingredients


def test_no_single_source_grain_is_skipped_not_rejected():
    # an entity-neutral measure-only recipe has NO SOURCE_ENTITY_KEY -> skipped, NOT a rejection
    t = _tmpl((Need(role="amt", concept="monetary_flow"),))
    e = ingredient_eligibility(t)
    assert e.eligible is False and e.reason is None


def test_semantic_rollup_paths_derivable():
    paths, status = semantic_rollup_paths("transaction", "account")
    assert status is EntityCompatibility.DERIVABLE
    assert paths and all(p.hops[0].from_entity == "transaction" for p in paths)


def test_exact_source_equals_target_is_empty_path():
    paths, status = semantic_rollup_paths("account", "account")
    assert status is EntityCompatibility.EXACT and paths == ()


# ---------------------------------------------------------------------------------------------
# Task B3 — physical-transition physics (R / roll-up bridge B / reposition). DB-backed.
# Fixtures use REAL registry data: transaction_id -> entity transaction, account_id -> account;
# the global hop transaction->account is transaction_to_account (MANY_TO_ONE). Column object_refs
# are `public.<table>.<column>` (graph.py _SCHEMA='public'); bridge endpoints are COLUMN refs.
# ---------------------------------------------------------------------------------------------
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.assembly import (
    _Position,
    realize_in_place,
    reposition_bridges,
    rollup_bridges,
)
from featuregen.overlay.upload.planner.contracts import CatalogScopeV1, SegmentKind


def _scope(*catalogs: str) -> CatalogScopeV1:
    return CatalogScopeV1(
        scope_id="s3b3", authorized_catalog_sources=tuple(catalogs), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-15T00:00:00Z",
        catalog_consideration_truncated=False)


def _txn_to_account_hop():
    paths, _ = semantic_rollup_paths("transaction", "account")
    return paths[0].hops[0]


def _seed(db, source, catalog):
    rows = [r for r, _ in catalog]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in catalog})


def _seed_bridge(db, fact_key, entity_id, left_cat, left_ref, right_cat, right_ref):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, status) VALUES (%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fact_key, entity_id, left_cat, left_ref, right_cat, right_ref))


def _core_catalog(db):
    """One catalog, transactions -> accounts realized by a declared intra-catalog N:1 join."""
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])


def _split_catalogs(db):
    """ops holds transactions (with an account FK column but NO intra-catalog accounts join);
    rev holds the account-grain table — the ONLY way transaction->account completes is a bridge."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
        # a second transaction-grain table holding the SAME concept column, to prove continuity:
        (CanonicalRow("ops", "other", "transaction_id", "integer", is_grain=True), "transaction_id"),
        (CanonicalRow("ops", "other", "account_id", "integer"), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        # an account-KEYED but transaction-GRAIN table (grain check must reject its endpoint):
        (CanonicalRow("rev", "acct_events", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("rev", "acct_events", "account_id", "integer"), "account_id"),
    ])


# --- (R) intra-catalog realization -----------------------------------------------------------

def test_realize_in_place_finds_the_realization_at_the_exact_position(db):
    _core_catalog(db)
    pos = _Position("transaction", "core", "public.transactions")
    hop = _txn_to_account_hop()
    moves = realize_in_place(db, pos, hop, _scope("core"))
    assert len(moves) == 1
    m = moves[0]
    assert m.next_position == _Position("account", "core", "public.accounts")
    assert m.bridge_fact_key is None
    assert [s.segment_kind for s in m.segments] == [
        SegmentKind.semantic_rollup, SegmentKind.intra_catalog_realization]
    roll, real = m.segments
    assert (roll.from_entity, roll.to_entity) == ("transaction", "account")
    assert roll.cardinality == "many_to_one"
    # 3B.3c C7 (C1 carry-forward, F16): the semantic hop's identity rides on the announcement
    # segment — self-contained audit evidence, NEVER physical-plan-id material
    assert (roll.relationship_id, roll.relationship_version) == (
        hop.relationship_id, hop.relationship_version)
    assert (roll.relationship_id, roll.relationship_version) == (
        "transaction_to_account", "1.0.0")
    assert real.relationship_id is None and real.relationship_version is None
    assert real.catalog_source == "core"
    # the distinguishing ref (plan_id material) — MUST be present on every realizer segment
    assert real.realization_ref == "core:public.transactions.account_id->public.accounts.account_id"


def test_realize_in_place_requires_exact_table_continuity(db):
    _core_catalog(db)
    # the realization exists IN the catalog, but its source table is transactions, not accounts
    pos = _Position("transaction", "core", "public.accounts")
    assert realize_in_place(db, pos, _txn_to_account_hop(), _scope("core")) == ()


def test_realize_in_place_orders_parallel_realizations_deterministically(db):
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "transactions", "acct_ref", "integer",
                      joins_to="accounts2.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts2", "account_id", "integer", is_grain=True), "account_id"),
    ])
    pos = _Position("transaction", "core", "public.transactions")
    moves = realize_in_place(db, pos, _txn_to_account_hop(), _scope("core"))
    assert len(moves) == 2
    # both DECLARED_JOIN -> ordered by realization_id ("...account_id->..." < "...acct_ref->...")
    refs = [m.segments[1].realization_ref for m in moves]
    assert refs == ["core:public.transactions.account_id->public.accounts.account_id",
                    "core:public.transactions.acct_ref->public.accounts2.account_id"]
    assert moves[0].next_position.table_ref == "public.accounts"
    assert moves[1].next_position.table_ref == "public.accounts2"


# --- (B) cross-catalog roll-up bridge --------------------------------------------------------

def test_rollup_bridge_crosses_catalog_via_fk_and_verified_bridge(db):
    _split_catalogs(db)
    _seed_bridge(db, "bfk1", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    pos = _Position("transaction", "ops", "public.transactions")
    hop = _txn_to_account_hop()
    moves = rollup_bridges(db, pos, hop, _scope("ops", "rev"))
    assert len(moves) == 1
    m = moves[0]
    assert m.next_position == _Position("account", "rev", "public.accounts")
    assert m.bridge_fact_key == "bfk1"
    assert [s.segment_kind for s in m.segments] == [
        SegmentKind.semantic_rollup, SegmentKind.governed_bridge]
    roll, bridge = m.segments
    assert (roll.from_entity, roll.to_entity) == ("transaction", "account")
    # 3B.3c C7 (C1 carry-forward, F16): the announcement carries the semantic hop's identity
    assert (roll.relationship_id, roll.relationship_version) == (
        hop.relationship_id, hop.relationship_version)
    assert bridge.relationship_id is None and bridge.relationship_version is None
    assert bridge.catalog_source == "rev"
    assert (bridge.from_entity, bridge.to_entity) == ("transaction", "account")
    assert bridge.bridge_fact_key == "bfk1"   # the distinguishing ref (plan_id material)


def test_relationship_refs_are_never_physical_plan_id_material():
    # behaviour-neutrality proof for the C7 assembly change: two otherwise-identical plans, one
    # whose semantic_rollup announcement carries relationship refs and one whose doesn't, MUST
    # mint the SAME physical_plan_id — the segment material hashes segment_kind:catalog:ref
    # (realization_ref/bridge_fact_key) only, so audit evidence can never move a stored id.
    def _mint(with_refs: bool):
        return make_binding_plan(
            recipe_id="t3b3b", target_entity="account", catalog_source="core",
            ingredient_bindings=_bindings("core"),
            path_segments=(
                BindingPathSegmentV1(segment_kind=SegmentKind.direct_catalog, catalog_source="core"),
                BindingPathSegmentV1(
                    segment_kind=SegmentKind.semantic_rollup, catalog_source="core",
                    from_entity="transaction", to_entity="account", cardinality="many_to_one",
                    relationship_id="transaction_to_account" if with_refs else None,
                    relationship_version="1.0.0" if with_refs else None),
                BindingPathSegmentV1(
                    segment_kind=SegmentKind.intra_catalog_realization, catalog_source="core",
                    realization_ref="core:public.transactions.account_id->public.accounts.account_id")),
            resolution_status=PlanResolutionStatus.resolved,
            path_resolution_status=PathResolutionStatus.source_to_target_resolved,
            primary_reason_code=None, reason_codes=(), safety=BindingSafety.safe,
            preference_rank=-1, preference_reasons=(), candidate_role=CandidateRole.rejected)

    assert _mint(True).physical_plan_id == _mint(False).physical_plan_id


def test_rollup_bridge_endpoint_on_a_different_table_is_not_continuous(db):
    _split_catalogs(db)
    # a perfectly VALID bridge — but anchored on ops.other, not the CURRENT table ops.transactions
    _seed_bridge(db, "bfk_other", "account",
                 "ops", "public.other.account_id", "rev", "public.accounts.account_id")
    hop, scope = _txn_to_account_hop(), _scope("ops", "rev")
    assert rollup_bridges(db, _Position("transaction", "ops", "public.transactions"), hop, scope) == ()
    # ...and it IS usable from the table it is actually anchored on (continuity, not absence)
    moves = rollup_bridges(db, _Position("transaction", "ops", "public.other"), hop, scope)
    assert [m.bridge_fact_key for m in moves] == ["bfk_other"]


def test_rollup_bridge_out_of_scope_catalog_is_fail_closed(db):
    _split_catalogs(db)
    _seed_bridge(db, "bfk1", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    pos = _Position("transaction", "ops", "public.transactions")
    hop = _txn_to_account_hop()
    assert rollup_bridges(db, pos, hop, _scope("ops")) == ()    # far endpoint unauthorized
    assert rollup_bridges(db, pos, hop, _scope("rev")) == ()    # current catalog unauthorized


def test_rollup_bridge_is_symmetric_over_left_right_storage(db):
    _split_catalogs(db)
    # the SAME crossing stored with the current-catalog endpoint on the RIGHT
    _seed_bridge(db, "bfk_swap", "account",
                 "rev", "public.accounts.account_id", "ops", "public.transactions.account_id")
    pos = _Position("transaction", "ops", "public.transactions")
    moves = rollup_bridges(db, pos, _txn_to_account_hop(), _scope("ops", "rev"))
    assert len(moves) == 1
    assert moves[0].next_position == _Position("account", "rev", "public.accounts")
    assert moves[0].bridge_fact_key == "bfk_swap"


def test_rollup_bridge_requires_a_genuinely_target_grain_far_table(db):
    _split_catalogs(db)
    # acct_events carries the account key but is TRANSACTION-grain — not a roll-up landing site
    _seed_bridge(db, "bfk_ev", "account",
                 "ops", "public.transactions.account_id", "rev", "public.acct_events.account_id")
    pos = _Position("transaction", "ops", "public.transactions")
    assert rollup_bridges(db, pos, _txn_to_account_hop(), _scope("ops", "rev")) == ()


# --- reposition (same-entity crossing; does not advance a hop) -------------------------------

def _mirror_catalogs(db):
    """account-grain tables in both catalogs; core.accounts also carries a NON-grain account key."""
    _seed(db, "core", [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "parent_account_id", "integer"), "account_id"),
        (CanonicalRow("core", "other", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("rev", "acct_events", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("rev", "acct_events", "account_id", "integer"), "account_id"),
    ])


def test_reposition_crosses_to_the_same_grain_table_symmetrically(db):
    _mirror_catalogs(db)
    # stored with the current-catalog endpoint on the RIGHT — symmetry covered here too
    _seed_bridge(db, "bfk_rep", "account",
                 "rev", "public.accounts.account_id", "core", "public.accounts.account_id")
    moves = reposition_bridges(db, _Position("account", "core", "public.accounts"),
                               _scope("core", "rev"))
    assert len(moves) == 1
    m = moves[0]
    assert m.next_position == _Position("account", "rev", "public.accounts")
    assert m.bridge_fact_key == "bfk_rep"
    assert len(m.segments) == 1
    seg = m.segments[0]
    assert seg.segment_kind is SegmentKind.governed_bridge
    assert (seg.from_entity, seg.to_entity) == ("account", "account")   # entity unchanged
    assert seg.catalog_source == "rev" and seg.bridge_fact_key == "bfk_rep"


def test_reposition_requires_the_grain_key_and_a_same_grain_far_table(db):
    _mirror_catalogs(db)
    pos = _Position("account", "core", "public.accounts")
    scope = _scope("core", "rev")
    # endpoint on a NON-grain account key of the current table -> not a reposition anchor
    _seed_bridge(db, "bfk_par", "account",
                 "core", "public.accounts.parent_account_id", "rev", "public.accounts.account_id")
    assert reposition_bridges(db, pos, scope) == ()
    # endpoint on the grain key, but the far table is transaction-grain -> rejected
    _seed_bridge(db, "bfk_ev", "account",
                 "core", "public.accounts.account_id", "rev", "public.acct_events.account_id")
    assert reposition_bridges(db, pos, scope) == ()
    # endpoint anchored on a DIFFERENT table of the same catalog -> not continuous from pos
    _seed_bridge(db, "bfk_oth", "account",
                 "core", "public.other.account_id", "rev", "public.accounts.account_id")
    assert reposition_bridges(db, pos, scope) == ()


def test_reposition_out_of_scope_is_fail_closed(db):
    _mirror_catalogs(db)
    _seed_bridge(db, "bfk_rep", "account",
                 "core", "public.accounts.account_id", "rev", "public.accounts.account_id")
    pos = _Position("account", "core", "public.accounts")
    assert reposition_bridges(db, pos, _scope("core")) == ()


def test_transitions_return_empty_when_nothing_matches(db):
    _seed(db, "bare", [
        (CanonicalRow("bare", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    pos = _Position("account", "bare", "public.accounts")
    hop = _txn_to_account_hop()
    scope = _scope("bare")
    assert realize_in_place(db, pos, hop, scope) == ()
    assert rollup_bridges(db, pos, hop, scope) == ()
    assert reposition_bridges(db, pos, scope) == ()


def test_rollup_bridges_self_guard_on_mismatched_position_entity(db):
    # B3 carry-forward: the physics must be SELF-defending — a caller pairing the wrong position
    # with a hop (pos.entity != hop.from_entity) gets (), fail-closed, even though the FK column,
    # the VERIFIED bridge, and the account-grain far table all exist.
    _split_catalogs(db)
    _seed_bridge(db, "bfk1", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    pos = _Position("account", "ops", "public.transactions")   # entity does NOT match the hop's from
    assert rollup_bridges(db, pos, _txn_to_account_hop(), _scope("ops", "rev")) == ()


# ---------------------------------------------------------------------------------------------
# Task B4 — the bounded frontier search + layered tier search + ranking + ambiguity.
# The search is NON-GREEDY: within a tier it expands ALL permitted transitions; a locally-valid
# realization that dead-ends must never prevent a bridge-first path from completing. Fail-closed:
# a state with no permitted transition becomes a REJECTED candidate (missing_realization /
# unsanctioned_bridge / bounded_out_*) — never a fabricated segment.
# ---------------------------------------------------------------------------------------------
from featuregen.overlay.upload.planner.assembly import assemble_paths, rank_and_classify
from featuregen.overlay.upload.planner.contracts import (
    BindingPathSegmentV1,
    BindingQuality,
    BindingSafety,
    CandidateRole,
    IngredientBindingV1,
    PathResolutionStatus,
    PlanResolutionStatus,
    PlanTier,
    make_binding_plan,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntitySemanticPathV1


def _bindings(catalog, ref="public.transactions.transaction_id"):
    return (IngredientBindingV1(
        recipe_id="t3b3b", need_role="txn", concept="transaction_id", required_grains=("transaction",),
        join_role="source_entity_key", temporal_role="", bound_catalog_source=catalog,
        bound_object_ref=ref, actual_source_grain="transaction",
        binding_quality=BindingQuality.grain_and_role_fit, safety=BindingSafety.safe, reason_codes=()),)


def _template():
    return _tmpl((Need(role="txn", concept="transaction_id"),))


def _first_path(source, target):
    paths, _ = semantic_rollup_paths(source, target)
    return paths[0]


def _assemble(db, *, source, catalog, table="public.transactions", target, path=None, scope,
              bindings=None):
    return assemble_paths(
        db, source_position=_Position(source, catalog, table),
        semantic_path=path if path is not None else _first_path(source, target),
        scope=scope, ingredient_bindings=bindings if bindings is not None else _bindings(catalog),
        template=_template(), target_entity=target)


# --- completion: roll-up bridge (tier 2) and zero-bridge (tier 1) -----------------------------

def test_assemble_completes_via_rollup_bridge(db):
    _split_catalogs(db)
    _seed_bridge(db, "bfk1", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    asm = _assemble(db, source="transaction", catalog="ops", target="account",
                    scope=_scope("ops", "rev"))
    assert len(asm.complete) == 1
    p = asm.complete[0]
    assert p.path_resolution_status is PathResolutionStatus.source_to_target_resolved
    assert p.resolution_status is PlanResolutionStatus.resolved
    assert p.bridge_count == 1 and p.tier is PlanTier.tier_2_one_bridge
    assert p.candidate_role is CandidateRole.selected
    assert p.participating_catalogs == ("ops", "rev")
    assert [s.segment_kind for s in p.path_segments] == [
        SegmentKind.direct_catalog, SegmentKind.semantic_rollup, SegmentKind.governed_bridge]
    assert p.path_segments[2].bridge_fact_key == "bfk1"


def test_assemble_zero_bridge_rollup_is_tier_1(db):
    _core_catalog(db)
    asm = _assemble(db, source="transaction", catalog="core", target="account", scope=_scope("core"))
    assert len(asm.complete) == 1 and asm.rejected == ()
    p = asm.complete[0]
    assert p.path_resolution_status is PathResolutionStatus.source_to_target_resolved
    assert p.bridge_count == 0 and p.tier is PlanTier.tier_1_single_catalog
    assert [s.segment_kind for s in p.path_segments] == [
        SegmentKind.direct_catalog, SegmentKind.semantic_rollup, SegmentKind.intra_catalog_realization]


def test_assemble_exact_zero_hop_completes_in_place(db):
    # EXACT semantic path (source == target, no hops) -> a valid zero-bridge complete plan
    _seed(db, "core", [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    asm = _assemble(db, source="account", catalog="core", table="public.accounts", target="account",
                    path=EntitySemanticPathV1(hops=()), scope=_scope("core"),
                    bindings=_bindings("core", ref="public.accounts.account_id"))
    assert len(asm.complete) == 1 and asm.rejected == ()
    p = asm.complete[0]
    assert p.path_resolution_status is PathResolutionStatus.source_to_target_resolved
    assert p.bridge_count == 0 and p.tier is PlanTier.tier_1_single_catalog
    assert [s.segment_kind for s in p.path_segments] == [SegmentKind.direct_catalog]


# --- THE GATE: non-greedy — a locally-valid realization that dead-ends must not block the
# --- bridge-first path from completing ---------------------------------------------------------

def _dead_end_vs_bridge(db):
    """ops realizes hop 0 (txn->account) INTRA-catalog, but ops.accounts has no path to customer:
    a greedy searcher that commits to the first (R) transition dies at hop 1. The ONLY completing
    physical path is bridge-FIRST: ops.transactions -bridge-> rev.accounts -R-> rev.customers."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("ops", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("rev", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("rev", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _seed_bridge(db, "bfkx", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")


def test_non_greedy_dead_end_does_not_block_bridge_first_completion(db):
    _dead_end_vs_bridge(db)
    asm = _assemble(db, source="transaction", catalog="ops", target="customer",
                    scope=_scope("ops", "rev"))
    # the bridge-first path completes even though the (deterministically FIRST) R transition dead-ends
    assert len(asm.complete) == 1
    p = asm.complete[0]
    assert p.path_resolution_status is PathResolutionStatus.source_to_target_resolved
    assert p.bridge_count == 1
    assert any(s.bridge_fact_key == "bfkx" for s in p.path_segments)
    # the dead end is a first-class REJECTED candidate (a realizer for hop 1 exists in rev, but no
    # verified bridge reaches it from the dead-end position) — never silently dropped
    assert len(asm.rejected) == 1
    r = asm.rejected[0]
    assert r.path_resolution_status is PathResolutionStatus.source_to_target_rejected
    assert r.primary_reason_code is ReasonCode.unsanctioned_bridge


# --- cycle prevention: reposition pair must terminate, no bridge fact reused -------------------

def test_reposition_cycle_terminates_without_bridge_reuse(db):
    _seed(db, "core", [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    # two parallel same-entity crossings between the SAME tables: an unguarded search ping-pongs forever
    _seed_bridge(db, "cyc1", "account",
                 "core", "public.accounts.account_id", "rev", "public.accounts.account_id")
    _seed_bridge(db, "cyc2", "account",
                 "core", "public.accounts.account_id", "rev", "public.accounts.account_id")
    asm = _assemble(db, source="account", catalog="core", table="public.accounts",
                    target="customer", scope=_scope("core", "rev"),
                    bindings=_bindings("core", ref="public.accounts.account_id"))
    # terminates, completes nothing (no account->customer realizer exists anywhere), rejects finitely
    assert asm.complete == ()
    assert asm.rejected
    for p in asm.rejected:
        keys = [s.bridge_fact_key for s in p.path_segments if s.bridge_fact_key is not None]
        assert len(keys) == len(set(keys))          # the same bridge fact never appears twice
        assert p.primary_reason_code is ReasonCode.missing_realization
    assert asm.bounding.total_states_expanded < 10  # finite, small — the cycle was cut, not bounded out
    assert asm.bounding.frontier_states_truncated is False


# --- whole-tier completion: fewest bridges wins; deeper tiers are not expanded -----------------

def test_whole_tier_zero_bridge_preferred_over_bridge_solution(db):
    _core_catalog(db)                                # 0-bridge R path core.transactions -> core.accounts
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _seed_bridge(db, "bfk_alt", "account",           # a 1-bridge alternative that must NOT be explored
                 "core", "public.transactions.account_id", "rev", "public.accounts.account_id")
    asm = _assemble(db, source="transaction", catalog="core", target="account",
                    scope=_scope("core", "rev"))
    assert len(asm.complete) == 1
    p = asm.complete[0]
    assert p.bridge_count == 0 and p.tier is PlanTier.tier_1_single_catalog
    assert p.candidate_role is CandidateRole.selected
    assert p.resolution_status is PlanResolutionStatus.resolved   # a clear single winner, no ambiguity
    assert all(q.bridge_count == 0 for q in asm.complete)         # the 1-bridge tier never completed
    assert asm.bounding.deeper_tiers_not_explored is True


# --- ambiguity: full-key ties resolve WITH ambiguity, deterministically ------------------------

def test_equal_rank_complete_paths_resolve_with_ambiguity(db):
    _seed(db, "core", [
        (CanonicalRow("core", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "transactions", "acct_ref", "integer",
                      joins_to="accounts2.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts2", "account_id", "integer", is_grain=True), "account_id"),
    ])
    asm = _assemble(db, source="transaction", catalog="core", target="account", scope=_scope("core"))
    # two physical paths, equal on the FULL ranking key (safety/bridges/quality/path-len/authority)
    assert len(asm.complete) == 2
    assert [p.candidate_role for p in asm.complete] == [
        CandidateRole.selected, CandidateRole.equal_rank_alternative]
    assert all(p.resolution_status is PlanResolutionStatus.resolved_with_ambiguity
               for p in asm.complete)
    assert asm.complete[0].physical_plan_id < asm.complete[1].physical_plan_id   # canonical id tie-break


def test_unknown_realizer_authority_ranks_worst_not_best(db):
    # B4-review M3, fail-closed: a realizer segment the governed authority lookup cannot resolve
    # must rank LAST (INFERRED_JOIN-level), never default to APPROVED-best.
    _core_catalog(db)   # core's txn->account realization is a DECLARED_JOIN (authority rank 1)

    def _plan(ref):
        return make_binding_plan(
            recipe_id="t3b3b", target_entity="account", catalog_source="core",
            ingredient_bindings=_bindings("core"),
            path_segments=(
                BindingPathSegmentV1(segment_kind=SegmentKind.direct_catalog, catalog_source="core"),
                BindingPathSegmentV1(segment_kind=SegmentKind.semantic_rollup, catalog_source="core",
                                     from_entity="transaction", to_entity="account",
                                     cardinality="many_to_one"),
                BindingPathSegmentV1(segment_kind=SegmentKind.intra_catalog_realization,
                                     catalog_source="core", realization_ref=ref)),
            resolution_status=PlanResolutionStatus.resolved,
            path_resolution_status=PathResolutionStatus.source_to_target_resolved,
            primary_reason_code=None, reason_codes=(), safety=BindingSafety.safe,
            preference_rank=-1, preference_reasons=(), candidate_role=CandidateRole.rejected)

    known = _plan("core:public.transactions.account_id->public.accounts.account_id")
    unknown = _plan("core:THIS_REALIZATION_DOES_NOT_EXIST")
    ranked = rank_and_classify(db, (unknown, known))
    # the resolvable DECLARED realizer (rank 1) beats the unresolvable one (worst, rank 2)
    assert [p.path_segments[2].realization_ref for p in ranked] == [
        "core:public.transactions.account_id->public.accounts.account_id",
        "core:THIS_REALIZATION_DOES_NOT_EXIST"]
    assert ranked[0].candidate_role is CandidateRole.selected
    assert ranked[1].candidate_role is CandidateRole.lower_rank_alternative   # strictly worse: no tie
    assert all(p.resolution_status is PlanResolutionStatus.resolved for p in ranked)


# --- fail-closed rejects: missing_realization / unsanctioned_bridge ----------------------------

def test_missing_realization_rejects_without_fabricating_segments(db):
    _seed(db, "bare", [
        (CanonicalRow("bare", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("bare", "transactions", "account_id", "integer"), "account_id"),
    ])
    asm = _assemble(db, source="transaction", catalog="bare", target="account", scope=_scope("bare"))
    assert asm.complete == ()
    assert len(asm.rejected) == 1
    r = asm.rejected[0]
    assert r.path_resolution_status is PathResolutionStatus.source_to_target_rejected
    assert r.resolution_status is PlanResolutionStatus.unresolved
    assert r.primary_reason_code is ReasonCode.missing_realization
    assert r.candidate_role is CandidateRole.rejected
    # NEVER a fabricated realizer/bridge segment to force completion
    assert [s.segment_kind for s in r.path_segments] == [SegmentKind.direct_catalog]


def test_unsanctioned_bridge_rejects_when_realizer_is_only_across_an_unbridged_crossing(db):
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
    ])
    _seed(db, "rev", [    # rev CAN realize txn->account — but no VERIFIED bridge reaches rev
        (CanonicalRow("rev", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("rev", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    asm = _assemble(db, source="transaction", catalog="ops", target="account",
                    scope=_scope("ops", "rev"))
    assert asm.complete == ()
    assert len(asm.rejected) == 1
    r = asm.rejected[0]
    assert r.primary_reason_code is ReasonCode.unsanctioned_bridge
    assert r.path_resolution_status is PathResolutionStatus.source_to_target_rejected
    assert [s.segment_kind for s in r.path_segments] == [SegmentKind.direct_catalog]


# --- determinism under seeding shuffle ----------------------------------------------------------

def test_ranked_output_is_byte_identical_under_bridge_seed_shuffle(db):
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
    ])
    for cat in ("rev", "rev2"):
        _seed(db, cat, [
            (CanonicalRow(cat, "accounts", "account_id", "integer", is_grain=True), "account_id"),
        ])
    bridges = [("b_rev", "rev"), ("b_rev2", "rev2")]

    def run(order):
        db.execute("DELETE FROM entity_bridge_edge")
        for fk, cat in order:
            _seed_bridge(db, fk, "account",
                         "ops", "public.transactions.account_id", cat, "public.accounts.account_id")
        return _assemble(db, source="transaction", catalog="ops", target="account",
                         scope=_scope("ops", "rev", "rev2"))

    a = run(bridges)
    b = run(list(reversed(bridges)))
    assert repr(a) == repr(b)                                     # byte-identical ranked output
    assert [p.physical_plan_id for p in a.complete] == [p.physical_plan_id for p in b.complete]
    assert len(a.complete) == 2 and all(p.bridge_count == 1 for p in a.complete)

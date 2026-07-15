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
    moves = realize_in_place(db, pos, _txn_to_account_hop(), _scope("core"))
    assert len(moves) == 1
    m = moves[0]
    assert m.next_position == _Position("account", "core", "public.accounts")
    assert m.bridge_fact_key is None
    assert [s.segment_kind for s in m.segments] == [
        SegmentKind.semantic_rollup, SegmentKind.intra_catalog_realization]
    roll, real = m.segments
    assert (roll.from_entity, roll.to_entity) == ("transaction", "account")
    assert roll.cardinality == "many_to_one"
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
    moves = rollup_bridges(db, pos, _txn_to_account_hop(), _scope("ops", "rev"))
    assert len(moves) == 1
    m = moves[0]
    assert m.next_position == _Position("account", "rev", "public.accounts")
    assert m.bridge_fact_key == "bfk1"
    assert [s.segment_kind for s in m.segments] == [
        SegmentKind.semantic_rollup, SegmentKind.governed_bridge]
    roll, bridge = m.segments
    assert (roll.from_entity, roll.to_entity) == ("transaction", "account")
    assert bridge.catalog_source == "rev"
    assert (bridge.from_entity, bridge.to_entity) == ("transaction", "account")
    assert bridge.bridge_fact_key == "bfk1"   # the distinguishing ref (plan_id material)


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

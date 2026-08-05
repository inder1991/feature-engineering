"""Spec A Task 3 (half b) — the `JoinPlan` adapter over the governed join planner.

This is the task where a mistake produces SILENTLY WRONG NUMBERS instead of an error, so the tests
are weighted toward the refusals. Three of them guard fail-OPENS, i.e. cases where doing nothing
looks like success:

* an UNKNOWN cardinality (`graph_edge.cardinality` is nullable) may BE `1:N`; refusing only the
  edges that SAY `1:N` would let a row-multiplying hop through and inflate every SUM downstream;
* a fan-out is refused and never repaired — de-duplication and pre-aggregation are not equivalent
  to each other, differ per operation (SUM vs COUNT DISTINCT vs RATIO), and encode a business
  allocation decision (whose transaction is a joint account's?) as a technical one;
* the planner's BFS indexes nodes by BARE table name, so two physical tables that share a name
  collapse into ONE node and a path can be stitched through the wrong table. Logical refs cannot
  express that (they are schema-flattened to `public`), so the check runs on RESOLVED physical
  identities across EVERY step, not just the endpoints.
"""
import inspect

import pytest

from featuregen.materialize import joins
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.joins import JoinPlan, PhysicalIdentity, plan_join
from featuregen.overlay.upload.join_path import JoinOutcome

_SRC = "hdfc"
_ROLES = ("feature_engineer",)

TXN = PhysicalIdentity(catalog_source=_SRC, schema="banking", table="transactions")
ACCOUNTS = PhysicalIdentity(catalog_source=_SRC, schema="banking", table="accounts")
CUSTOMERS = PhysicalIdentity(catalog_source=_SRC, schema="banking", table="customers")


# ── catalog seeding: the PUBLIC-FLATTENED graph an upload really produces ────────────────────────
# Every object_ref is `public.<table>.<column>` (graph.py:20) and the REAL declared schema lives
# only in `graph_node.schema_name` — the whole reason the adapter takes resolved identities.


def _col(db, table, column, *, schema="banking", sensitivity=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name, sensitivity) VALUES (%s, %s, 'column', %s, %s, %s, %s)",
        (_SRC, f"public.{table}.{column}", table, column, schema, sensitivity))


def _edge(db, from_ref, to_ref, *, cardinality="N:1", fact_key=None, status=None):
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, %s, 'operational', %s, %s)",
        (_SRC, from_ref, to_ref, cardinality, fact_key, status))


@pytest.fixture
def verified_join_catalog(db):
    """transactions -N:1-> accounts, backed by a VERIFIED `approved_join` fact."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    return db


@pytest.fixture
def two_hop_catalog(db):
    """transactions -N:1-> accounts -N:1-> customers: the shape `total_debit_amount_30d` needs."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id",
          fact_key="ajf-acct-cust", status="VERIFIED")
    return db


# ── the happy path, and the trap it walks past ───────────────────────────────────────────────────


def test_bare_table_names_are_passed_to_the_planner(verified_join_catalog):
    """VERIFIED: `table_of_ref` returns the SECOND dotted segment, so the BFS destination is a bare
    name — passing `banking.accounts` (or even `public.accounts`) never matches and would come back
    NO_PATH. The adapter therefore keeps the physical schema on the identity and sends the table."""
    result = plan_join(verified_join_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, JoinPlan)
    assert result.steps
    assert result.outcome_kind == JoinOutcome.OPERATIONAL
    assert result.fans_out is False


def test_authority_survives_into_the_plan(verified_join_catalog):
    result = plan_join(verified_join_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, JoinPlan)
    (step,) = result.steps
    assert step.approved_join_fact_key == "ajf-txn-acct"
    assert step.approved_join_status == "VERIFIED"
    assert step.authority == "operational"


def test_the_roles_that_decided_the_plan_are_recorded(verified_join_catalog):
    """Roles are not incidental: they decide whether a hop is DENIED, so the same catalog yields
    different plans for different readers. A plan that did not say which roles produced it could
    not be re-checked."""
    result = plan_join(verified_join_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=["feature_engineer"])
    assert isinstance(result, JoinPlan)
    assert result.roles_used == ("feature_engineer",)


def test_a_table_joined_to_itself_needs_no_hops(verified_join_catalog):
    result = plan_join(verified_join_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=TXN, roles=_ROLES)
    assert isinstance(result, JoinPlan)
    assert result.steps == ()
    assert result.fans_out is False


def test_a_multi_hop_plan_keeps_every_hop_in_traversal_order(two_hop_catalog):
    result = plan_join(two_hop_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, JoinPlan)
    assert [s.approved_join_fact_key for s in result.steps] == ["ajf-txn-acct", "ajf-acct-cust"]


# ── refusal 1/4: the outcome kinds ───────────────────────────────────────────────────────────────


def test_unverified_path_is_refused_and_NAMES_the_fact_keys(db):
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-awaiting-review", status="DRAFT")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_PATH_NOT_VERIFIED
    assert "ajf-awaiting-review" in result.detail   # the reviewer needs to know WHICH fact


def test_read_scope_denied_path_is_refused_and_NAMES_the_endpoints(db):
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id", sensitivity="pii")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_PATH_DENIED_BY_READ_SCOPE
    assert "public.accounts.account_id" in result.detail


def test_denied_is_NOT_the_same_refusal_as_unverified_or_no_path(db):
    """Three governed conditions, three codes. Collapsing them (as `find_join_path`'s `None` does)
    would tell an operator to fix the wrong thing: grant a role, get a join approved, or model a
    path that does not exist."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id", sensitivity="pii")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")
    denied = plan_join(db, catalog_source=_SRC, from_identity=TXN, to_identity=ACCOUNTS, roles=())
    granted = plan_join(db, catalog_source=_SRC, from_identity=TXN, to_identity=ACCOUNTS,
                        roles=("pii_reader",))
    assert isinstance(denied, MaterializationRefused)
    assert denied.code is CompilationRefusalCode.JOIN_PATH_DENIED_BY_READ_SCOPE
    assert isinstance(granted, JoinPlan)      # same catalog, sufficient read scope


def test_no_governed_path_is_GRAIN_PATH_NOT_GOVERNED(db):
    _col(db, "transactions", "acct_id")
    _col(db, "customers", "cif_id")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.GRAIN_PATH_NOT_GOVERNED


# ── refusal 2/4: unknown cardinality is a FAIL-OPEN unless it is refused ─────────────────────────


def test_unknown_cardinality_is_REFUSED(db):
    """VERIFIED: `JoinStep.cardinality` is `str | None` over a NULLABLE `graph_edge.cardinality`.
    An unknown edge may BE `1:N`, which multiplies rows and inflates a SUM — so "we do not know"
    must refuse, never proceed."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id", cardinality=None,
          fact_key="ajf-no-cardinality", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_unknown_cardinality_on_an_INTERMEDIATE_hop_is_refused(two_hop_catalog):
    """The first hop is a governed N:1; only the second is unknown. Checking the endpoints' hops
    and trusting the middle would be the same fail-open one hop deeper in."""
    two_hop_catalog.execute(
        "UPDATE graph_edge SET cardinality = NULL WHERE from_ref = 'public.accounts.cif_id'")
    result = plan_join(two_hop_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN


def test_unknown_cardinality_on_the_FIRST_hop_is_refused(two_hop_catalog):
    """The mirror of the intermediate-hop test above, and the one every pre-existing fixture
    missed: the defect sits on hop 1 and the LAST hop is a clean governed N:1. A checker that read
    only the final hop (`steps[-1:]`) passes every other test in this file and fails here."""
    two_hop_catalog.execute(
        "UPDATE graph_edge SET cardinality = NULL WHERE from_ref = 'public.transactions.acct_id'")
    result = plan_join(two_hop_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN
    assert "public.transactions.acct_id" in result.detail   # the FIRST hop is the one named


def test_a_cardinality_token_outside_the_governed_vocabulary_is_also_unknown():
    """Fail closed on a token nobody has classified. The DB CHECK admits only 1:1 / 1:N / N:1
    today, so this is unreachable through the catalog — which is exactly why it is asserted on the
    classifier directly rather than left as an untested `else`."""
    assert joins._cardinality_verdict("N:N") is joins._CardinalityVerdict.UNKNOWN
    assert joins._cardinality_verdict(None) is joins._CardinalityVerdict.UNKNOWN
    assert joins._cardinality_verdict("N:1") is joins._CardinalityVerdict.FANS_IN
    assert joins._cardinality_verdict("1:1") is joins._CardinalityVerdict.FANS_IN
    assert joins._cardinality_verdict("1:N") is joins._CardinalityVerdict.FANS_OUT


def test_every_governed_cardinality_token_has_a_direction_verdict():
    """A tripwire: if the governed vocabulary ever gains a token, this module must decide whether
    it fans toward the destination — not silently reclassify it as UNKNOWN."""
    from featuregen.overlay.upload.canonical import _VALID_CARDINALITY

    assert joins._FANS_IN | joins._FANS_OUT == set(_VALID_CARDINALITY)


# ── refusal 3/4: fan-out toward the destination ──────────────────────────────────────────────────


def test_fan_out_toward_the_destination_is_REFUSED(db):
    """The joint-account shape: one account belongs to two customers, so traversing accounts ->
    customers duplicates every transaction row. Refusing is the honest outcome — §3.2."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id", cardinality="1:N",
          fact_key="ajf-joint-holders", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_fan_out_is_detected_on_a_REVERSE_hop(db):
    """The stored edge reads customers -N:1-> accounts; travelled the other way it is 1:N. The
    adapter must read the TRAVERSAL-oriented cardinality, or a fan-out hides behind a stored
    direction that looks safe."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    _edge(db, "public.customers.cif_id", "public.accounts.cif_id", cardinality="N:1",
          fact_key="ajf-reverse", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_fan_out_on_the_FIRST_hop_is_refused(db):
    """The split-settlement shape: one transaction row lands on MANY account rows, and the second
    hop is a perfectly governed N:1. Every pre-existing fan-out fixture put the 1:N last, so a
    checker that only read the final hop looked correct."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id", cardinality="1:N",
          fact_key="ajf-split-settlement", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id",
          fact_key="ajf-acct-cust", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED
    assert "public.transactions.acct_id" in result.detail


def test_fan_out_is_detected_on_a_REVERSE_FIRST_hop(db):
    """The reverse-orientation trap, moved to hop 1: the stored edge reads accounts -N:1->
    transactions, which travelled transactions -> accounts is 1:N. The second hop is clean, so
    only a checker that reads EVERY hop in traversal orientation refuses this path."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id")
    _edge(db, "public.accounts.account_id", "public.transactions.acct_id", cardinality="N:1",
          fact_key="ajf-reverse-first", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id",
          fact_key="ajf-acct-cust", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_FANOUT_UNSUPPORTED


def test_an_N_N_token_reaching_the_adapter_END_TO_END_is_refused_as_unknown(db):
    """`_cardinality_verdict("N:N")` is asserted directly above because migration 0993's CHECK
    keeps the token out of `graph_edge` today. This test is the day the vocabulary widens: with
    the CHECK gone (rolled back with the test transaction), an `N:N` edge really reaches
    `plan_join` through the planner — and the adapter, not the schema, must be the thing that
    refuses it."""
    db.execute("ALTER TABLE graph_edge DROP CONSTRAINT graph_edge_cardinality_check")
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id", cardinality="N:N",
          fact_key="ajf-many-many", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN
    assert "'N:N'" in result.detail


def test_the_same_edge_is_planned_when_it_fans_IN(db):
    """The mirror image of the test above: N:1 toward the destination is the safe direction and
    must still plan, so the fan-out rule discriminates rather than blocking every reverse hop."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _edge(db, "public.accounts.account_id", "public.transactions.acct_id", cardinality="1:N",
          fact_key="ajf-stored-backwards", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, JoinPlan)
    (step,) = result.steps
    assert step.cardinality == "N:1"
    assert step.approved_join_fact_key == "ajf-stored-backwards"


def test_no_deduplication_helper_exists_in_the_module():
    src = inspect.getsource(joins)
    for banned in ("dropDuplicates", "drop_duplicates", "distinct("):
        assert banned not in src, "fan-out is refused, never repaired"


def test_the_adapter_does_not_reimplement_the_planner():
    """It is an ADAPTER: the governed planner owns traversal. A private BFS here would be a second
    answer to "what joins exist", free to disagree with the one governance approved."""
    # Cross-catalog execution legitimately consumes a typed bridge realization in this module;
    # the single-catalog adapter must still delegate traversal rather than search for bridges.
    src = inspect.getsource(joins.plan_join)
    for banned in ("deque", "_bfs", "bridge"):
        assert banned not in src, "traversal belongs to classify_join_path"


# ── refusal 4/4: two physical schemas sharing a table name ───────────────────────────────────────


@pytest.fixture
def ambiguous_intermediate_catalog(db):
    """`accounts` is TWO physical tables — `banking.accounts` and `risk.accounts`. The flatten
    collapses both onto one `public.accounts` BFS node, so the planner will happily stitch a path
    through a table that is not one table. Both ENDPOINTS are unambiguous."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "accounts", "risk_flag", schema="risk")        # the collision
    _col(db, "customers", "cif_id")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id",
          fact_key="ajf-acct-cust", status="VERIFIED")
    return db


def test_two_resolved_schemas_sharing_a_table_name_are_refused(ambiguous_intermediate_catalog):
    result = plan_join(ambiguous_intermediate_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AMBIGUOUS_TABLE_NAME
    assert "accounts" in result.detail
    assert "banking" in result.detail and "risk" in result.detail


def test_ambiguity_is_checked_on_EVERY_step_not_just_the_endpoints(ambiguous_intermediate_catalog):
    """`accounts` appears nowhere in the call — it is discovered mid-path. A check written against
    the two supplied identities would pass this catalog and read the wrong table."""
    result = plan_join(ambiguous_intermediate_catalog, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert "transactions" not in result.detail and "customers" not in result.detail


def test_a_path_crossing_schemas_with_UNIQUE_table_names_is_allowed(db):
    """Discriminating, not blanket: `crm.customers` joined from `banking.transactions` is a normal
    cross-database join. Only a NAME shared by two schemas is ambiguous."""
    _col(db, "transactions", "acct_id")
    _col(db, "accounts", "account_id")
    _col(db, "accounts", "cif_id")
    _col(db, "customers", "cif_id", schema="crm")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id",
          fact_key="ajf-acct-cust", status="VERIFIED")
    crm_customers = PhysicalIdentity(catalog_source=_SRC, schema="crm", table="customers")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=crm_customers, roles=_ROLES)
    assert isinstance(result, JoinPlan)
    assert len(result.steps) == 2


def test_a_supplied_identity_contradicting_the_catalog_is_refused(verified_join_catalog):
    """The caller resolved `transactions` to `risk`; the governed catalog says `banking`. Two
    candidate physical tables for one name — reading either one silently would be the wrong-table
    read this spec exists to prevent."""
    mis_resolved = PhysicalIdentity(catalog_source=_SRC, schema="risk", table="transactions")
    result = plan_join(verified_join_catalog, catalog_source=_SRC,
                       from_identity=mis_resolved, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.AMBIGUOUS_TABLE_NAME


def test_an_unattested_schema_is_not_an_ambiguity(db):
    """`schema_name` is NULLABLE (a technical upload attests none). NULL is UNKNOWN, not a rival
    candidate — refusing here would refuse nearly every real catalog, and resolving it is Task 5's
    job (`PHYSICAL_SCHEMA_NOT_RESOLVED`), not this adapter's."""
    _col(db, "transactions", "acct_id", schema=None)
    _col(db, "accounts", "account_id", schema=None)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, JoinPlan)


def test_schema_case_alone_is_not_an_ambiguity(db):
    """`schema_name` is stored RAW-CASE (migration 1000), and unquoted SQL identifiers fold. So
    `Banking` and `banking` are one schema, not two."""
    _col(db, "transactions", "acct_id", schema="banking")
    _col(db, "transactions", "amount", schema="BANKING")
    _col(db, "accounts", "account_id", schema="Banking")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-txn-acct", status="VERIFIED")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=ACCOUNTS, roles=_ROLES)
    assert isinstance(result, JoinPlan)


# ── the boundary itself ──────────────────────────────────────────────────────────────────────────


def test_every_refusal_is_a_typed_compilation_refusal(db):
    """Never a bare exception, and never a code from another vocabulary (§14)."""
    _col(db, "transactions", "acct_id")
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)
    assert isinstance(result.code, CompilationRefusalCode)


def test_refusals_are_RETURNED_not_raised(db):
    """The signature is `JoinPlan | MaterializationRefused`: a refused join is one governed verdict
    among many a compilation collects, not an exceptional event."""
    result = plan_join(db, catalog_source=_SRC,
                       from_identity=TXN, to_identity=CUSTOMERS, roles=_ROLES)
    assert isinstance(result, MaterializationRefused)


def test_an_identity_from_another_catalog_is_a_CALLER_error(verified_join_catalog):
    """Not a governed refusal — no governed metadata is involved. The planner is single-catalog, so
    silently planning inside `catalog_source` while the caller believes it asked about another one
    is a defect at the call site, and it must be loud."""
    foreign = PhysicalIdentity(catalog_source="other", schema="banking", table="accounts")
    with pytest.raises(ValueError, match="catalog_source"):
        plan_join(verified_join_catalog, catalog_source=_SRC,
                  from_identity=TXN, to_identity=foreign, roles=_ROLES)


def test_the_plan_and_its_identity_types_are_frozen():
    with pytest.raises(Exception):
        TXN.table = "accounts"      # type: ignore[misc]


# ── Release C: the two-leg crosswalk adapter (`plan_crosswalk_join`) ─────────────────────────────
#
# This module owns `plan_crosswalk_join` and had no test of it here at all: the coverage lived in
# `test_crosswalk_ir.py`, which drives it THROUGH the IR. These four go at the adapter directly,
# because every one of them is a case where the wrong answer is a silently wrong NUMBER rather than
# an error — the same reason the rest of this file is weighted toward refusals.

def _crosswalk_identities():
    from tests.featuregen.materialize import crosswalk_fixtures as cf

    def _of(bind):
        return PhysicalIdentity(catalog_source=bind.identity.catalog_source,
                                schema=bind.identity.schema, table=bind.identity.table)
    return _of(cf.CIB_BINDING), _of(cf.FTR_BINDING), _of(cf.MAP_BINDING)


def _plan_crosswalk(*, direction=None, admitted=None, tier=None):
    from tests.featuregen.materialize import crosswalk_fixtures as cf

    from featuregen.overlay.upload.bridge_realization import ExecutionTier
    from featuregen.overlay.upload.crosswalk_observation import SOURCE_TO_TARGET

    source, target, mapping = _crosswalk_identities()
    forward = (direction or SOURCE_TO_TARGET) == SOURCE_TO_TARGET
    return joins.plan_crosswalk_join(
        admitted if admitted is not None else cf.admitted(),
        direction=direction or SOURCE_TO_TARGET,
        from_identity=source if forward else target,
        to_identity=target if forward else source,
        mapping_identity=mapping,
        execution_tier=tier or ExecutionTier.PRODUCTION,
        roles=_ROLES)


def test_a_crosswalk_plans_TWO_steps_and_never_collapses_to_endpoint_equality():
    """The mapping table is a RELATION on the path, not an implementation detail of a bridge.

    Collapsing the two legs into one endpoint-to-endpoint equality would join `cib.acct_no` to
    `ftr.counter_party_acct_no` directly — two different identifier schemes that were never equal —
    and produce a plausible, empty-or-wrong result with no error anywhere.
    """
    from tests.featuregen.materialize import crosswalk_fixtures as cf

    plan = _plan_crosswalk()
    assert isinstance(plan, JoinPlan), getattr(plan, "detail", plan)
    assert len(plan.steps) == 2
    endpoints = {frozenset(pair) for step in plan.steps for pair in step.column_pairs}
    assert frozenset({cf.CIB_ACCT, cf.FTR_ACCT}) not in endpoints, (
        "the plan joins the two endpoints to each other: a crosswalk rendered as a direct bridge")
    # Every pair has exactly one foot in the mapping table — which is what "two hops" means.
    for pair in endpoints:
        assert len([ref for ref in pair if ref.startswith(cf.MAP + ".")]) == 1


def test_both_legs_column_pairs_reach_the_plan_so_neither_escapes_authorization():
    """Gate 2 reads the plan's steps; a leg missing from it is a table read without authorization.

    The failure this pins is asymmetric: dropping a leg from the PLAN removes its columns from the
    physical read set, so the run reads a table nobody authorized — and every visible symptom
    (fewer requirements, a shorter read set) looks like a smaller, tidier plan.
    """
    from tests.featuregen.materialize import crosswalk_fixtures as cf

    plan = _plan_crosswalk()
    assert isinstance(plan, JoinPlan), getattr(plan, "detail", plan)
    refs = {ref for step in plan.steps for pair in step.column_pairs for ref in pair}
    assert refs == {cf.CIB_ACCT, cf.MAP_ACCT, cf.MAP_EXT, cf.FTR_ACCT}
    # Both catalogs are named by the steps themselves: the mapping dataset and the target endpoint
    # need not live where the source relation does, so a single-catalog read of this plan is wrong.
    assert {step.to_catalog_source for step in plan.steps} == {"cib", "ftr"}


def test_the_reverse_direction_is_gated_on_its_OWN_measured_cardinality():
    """1:1 forward with 2:1 reverse is the ordinary mapping-table shape, and it admits ONE way.

    Reading the reverse direction's gate off the forward verdict — or inverting which verdict gates
    which traversal — refuses every legal reverse plan and admits an illegal one. The fixture is
    measured asymmetrically precisely so that swapping the two is visible here.
    """
    from featuregen.materialize.codes import CompilationRefusalCode as _Code
    from featuregen.overlay.upload.crosswalk_observation import SOURCE_TO_TARGET, TARGET_TO_SOURCE

    forward = _plan_crosswalk(direction=SOURCE_TO_TARGET)
    reverse = _plan_crosswalk(direction=TARGET_TO_SOURCE)

    assert isinstance(forward, JoinPlan), getattr(forward, "detail", forward)
    assert forward.fans_out is False
    assert isinstance(reverse, MaterializationRefused), (
        "the reverse direction was planned on the forward direction's evidence")
    assert reverse.code in {_Code.JOIN_FANOUT_UNSUPPORTED, _Code.JOIN_CARDINALITY_UNKNOWN}


def test_a_fanning_crosswalk_direction_is_REFUSED_and_never_deduplicated():
    """No DISTINCT, no pre-aggregation, no allocation. Same rule as every other join here.

    Allocating one row across many is a governed business decision (whose transaction is a joint
    account's?) and no allocation policy exists — so the traversal stops rather than inventing one.
    """
    from featuregen.materialize.codes import CompilationRefusalCode as _Code
    from featuregen.overlay.upload.crosswalk_observation import TARGET_TO_SOURCE

    refused = _plan_crosswalk(direction=TARGET_TO_SOURCE)

    assert isinstance(refused, MaterializationRefused)
    # The MEASURED fan-out is what stops it, and the refusal names that reason rather than a
    # generic "unknown": admissibility is checked before the composed-cardinality branch, so this
    # direction never reaches the point where a repair could be attempted.
    assert refused.code is _Code.JOIN_CARDINALITY_UNKNOWN
    assert "directional_crosswalk_fanout" in refused.detail
    # And the module still offers nobody a way to repair one — the sibling assertion above, applied
    # to the crosswalk path.
    source = inspect.getsource(joins)
    for banned in ("dropDuplicates", "drop_duplicates", "DISTINCT ON"):
        assert banned not in source

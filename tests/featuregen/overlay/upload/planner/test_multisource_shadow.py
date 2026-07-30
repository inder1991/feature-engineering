"""Phase 3C.2b-i-A · Task 11 — the TWO-CONNECTION multi-source assembly shadow HARNESS + CLI entry.

``run_multisource_assembly_shadow`` mirrors the single-source ``run_shadow_planner`` but is driven
over TWO connections (finding #13), the whole point of the task: a single connection CANNOT both
(a) see the gold fixture transaction that is rolled back after planning AND (b) durably retain the
telemetry. So the harness:

  (1) writes the dispatch MANIFEST on ``telemetry_conn`` FIRST (the expected intent-id set);
  (2) plans each intent on ``planning_conn`` (which sees the gold fixture txn) inside a per-intent
      SAVEPOINT, owning ONE mutable ``CompileBudget`` across intents;
  (3) retains results in memory;
  (4) rolls back the fixture transaction on ``planning_conn``;
  (5) persists one intent_result (+ candidates + operands) per intent on ``telemetry_conn``;
  (6) reconciles the manifest against the results (the durable capture-integrity signal).

Covered here: a clean 2-intent run (manifest-first, plan, retain, rollback, persist, reconcile
clean); an injected DB error in ONE intent -> ``technical_failure`` isolated by the savepoint (the
manifest + the OTHER intent unpoisoned); a budget-exhausting run -> ``budget_truncated``; and the
two-connection boundary itself — the gold is GONE on ``planning_conn`` after rollback while the
telemetry PERSISTS on ``telemetry_conn`` (a single connection could not do both). The gold is seeded
through the REAL governance write paths (``propose_fact``/``_confirm_grain``, VERIFIED bridge, drift
watermarks) on ``planning_conn``'s transaction — nothing stubbed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from tests.featuregen.overlay.upload.conftest import _confirm_grain

from featuregen.contracts.envelopes import Command
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner import multisource_shadow
from featuregen.overlay.upload.planner.contracts import AdditivityClass
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    FinalOperation,
    GovernedSourceBindingV1,
    MultiSourcePlannerIntentV1,
    MultiSourceReason,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    SemanticRole,
)
from featuregen.overlay.upload.planner.multisource_shadow import (
    run_multisource_assembly_shadow,
    run_shadow_cli,
)
from featuregen.overlay.upload.planner.multisource_shadow_store import (
    read_candidates,
    read_intent_results,
    read_operands,
    reconcile,
)
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 19, tzinfo=UTC)
_SRC_GRAIN_FK = fact_key(table_ref("core_banking", "transactions"), "grain")


# ── a SECOND connection: planning_conn (sees the gold fixture txn; rolled back on teardown) ──
@pytest.fixture
def planning_conn(_dsn):
    """A durable, INDEPENDENT connection to the same test DB as ``db`` (the telemetry conn). The
    gold fixture is seeded (uncommitted) on THIS transaction; the harness rolls it back after
    planning. A separate session from ``db`` is exactly what the two-connection contract needs —
    ``db``'s telemetry writes are unaffected by this connection's rollback."""
    connection = psycopg.connect(_dsn)
    try:
        yield connection
        connection.rollback()
    finally:
        connection.close()


# ── seed helpers (the sanctioned Task-5/Task-8/Task-9 pattern) ──
def _seed(conn, source, rows_concepts):
    rows = [r for r, _ in rows_concepts]
    build_graph(conn, source, rows, concepts={content_hash(r): c for r, c in rows_concepts})


def _seed_verified_bridge(conn, fk, entity_id, lc, lref, rc, rref):
    conn.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, confirmed_event_id, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'VERIFIED')",
        (fk, entity_id, lc, lref, rc, rref, f"evt-{fk}"))


def _seed_verified_grain(conn, source, table, columns, *, service_actor, human_actor):
    ref = table_ref(source, table)
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain",
         "proposed_value": {"columns": columns, "is_unique": True}},
        service_actor, f"propose-grain-{source}-{table}"))
    assert res.accepted, res.denied_reason
    _confirm_grain(conn, source, table, columns, actor=human_actor)


def _watermark(conn, source, at, head_seq=0):
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id,"
        " head_seq) VALUES (%s,%s,'drift_t11',%s) ON CONFLICT (catalog_source) DO UPDATE SET"
        " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq",
        (source, at, head_seq))


def _seed_resolved_topology(conn, service_actor, human_actor):
    """core_banking.transactions -> VERIFIED bridge at account -> intra-wealth realization ->
    wealth.customers, with a VERIFIED grain fact on EVERY hop endpoint + FRESH drift watermarks —
    so a governed multi-source intent resolves BOTH axes. Seeded on ``conn``'s transaction."""
    ensure_upload_catalog_adapter()
    _seed(conn, "core_banking", [
        (CanonicalRow("core_banking", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("core_banking", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("core_banking", "transactions", "amount", "numeric"), "monetary_flow"),
    ])
    _seed(conn, "wealth", [
        (CanonicalRow("wealth", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("wealth", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("wealth", "customers", "customer_id", "integer", is_grain=True),
         "customer_id"),
    ])
    _seed_verified_bridge(conn, "bfk_acct", "account",
                          "core_banking", "public.transactions.account_id",
                          "wealth", "public.accounts.account_id")
    _seed_verified_grain(conn, "core_banking", "transactions", ["transaction_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(conn, "wealth", "accounts", ["account_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _seed_verified_grain(conn, "wealth", "customers", ["customer_id"],
                         service_actor=service_actor, human_actor=human_actor)
    _watermark(conn, "core_banking", _NOW - timedelta(minutes=5))
    _watermark(conn, "wealth", _NOW - timedelta(minutes=5))


def _seed_stale_union_topology(conn, service_actor, human_actor):
    """The resolving topology, but with wealth's drift watermark STALE (2h > 60min SLA): every path
    resolves on the ASSEMBLY axis, yet the compile-end UNION freshness observation fails — so the plan
    lands resolved-assembly + INCOMPLETE-contract (the two axes are NEVER collapsed)."""
    _seed_resolved_topology(conn, service_actor, human_actor)
    _watermark(conn, "wealth", _NOW - timedelta(hours=2))   # stale -> union freshness unresolved


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


# ── intent builders ──
def _strategy(aggregation=PathAggregation.sum, output_additivity=AdditivityClass.additive):
    return PathStrategyV1(
        aggregation=aggregation, output_type="numeric", output_additivity=output_additivity,
        external_type_required=False, ordering_anchor_concept=None)


def _operand(*, slot_id, semantic_role):
    return OperandSlotV1(
        slot_id=slot_id, semantic_role=semantic_role, catalog_source="core_banking",
        object_ref="public.transactions.amount", authoritative_concept="monetary_flow",
        path_strategy=_strategy(),
        source_binding=GovernedSourceBindingV1(
            source_grain_entity="transaction",
            source_grain_key_refs=("public.transactions.transaction_id",),
            grain_fact_key=_SRC_GRAIN_FK))


def _ratio_intent():
    return MultiSourcePlannerIntentV1(
        target_entity="customer",
        operands=(
            _operand(slot_id="op_num", semantic_role=SemanticRole.numerator),
            _operand(slot_id="op_den", semantic_role=SemanticRole.denominator)),
        final_expression=FinalExpressionV1(
            operation=FinalOperation.ratio, ordered_slot_ids=("op_num", "op_den"),
            time_slot_id=None, window=None, output_additivity=AdditivityClass.non_additive),
        operation_policy_version=multisource_shadow.OPERATION_POLICY_VERSION)


# ── tests ──
def test_two_intent_run_manifest_first_persist_reconcile_clean(
        db, planning_conn, service_actor, human_actor):
    """The whole two-connection sequence over 2 gold intents: manifest on telemetry FIRST, plan on
    planning, retain, roll back the fixtures, persist on telemetry, reconcile clean — both resolve."""
    _seed_resolved_topology(planning_conn, service_actor, human_actor)
    intents = {"i_a": _ratio_intent(), "i_b": _ratio_intent()}

    results = run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents=intents, run_id="mrun_t11", roles=("feature_engineer",), now=_NOW)

    # both intents planned + retained in memory (run_id stamped)
    assert len(results) == 2
    assert {r.result_status for r in results} == {MultiSourceReason.resolved}
    assert all(r.run_id == "mrun_t11" for r in results)

    # persisted on telemetry_conn despite the planning_conn rollback; reconcile is complete
    rows = read_intent_results(db, "mrun_t11")
    assert len(rows) == 2
    assert {row["intent_id"] for row in rows} == {"i_a", "i_b"}
    for row in rows:
        assert row["semantic_outcome"] == "resolved"
        assert row["compile_completeness"] == "complete"    # contract axis resolved too
        assert row["technical_status"] == "ok"
        assert row["capture_status"] == "persisted"
        assert row["selected_plan_id"]                      # an assembly selection was recorded
    assert read_candidates(db, "mrun_t11", "i_a")           # candidate rows landed

    rec = reconcile(db, "mrun_t11")
    assert rec.expected == 2
    assert rec.present == 2
    assert rec.complete


def test_manifest_written_before_planning(db, planning_conn, service_actor, human_actor, monkeypatch):
    """The manifest is written on telemetry_conn BEFORE the first plan_multi_source call — a pre-loop
    failure (or a mid-run loss) is then visible via reconciliation. We prove ordering by asserting the
    dispatch row already exists at the moment the FIRST plan call runs."""
    _seed_resolved_topology(planning_conn, service_actor, human_actor)
    real_plan = multisource_shadow.plan_multi_source
    seen: dict[str, int] = {}

    def _spy(conn, adapter, *, intent, **kw):
        seen["dispatch_at_first_plan"] = db.execute(
            "SELECT count(*) FROM multisource_assembly_shadow_dispatch WHERE run_id = 'mrun_order'"
        ).fetchone()[0]
        multisource_shadow.plan_multi_source = real_plan   # only spy the first call
        return real_plan(conn, adapter, intent=intent, **kw)

    monkeypatch.setattr(multisource_shadow, "plan_multi_source", _spy)
    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents={"i_a": _ratio_intent()}, run_id="mrun_order",
        roles=("feature_engineer",), now=_NOW)
    assert seen["dispatch_at_first_plan"] == 1   # manifest present before the plan ran


def test_injected_db_error_in_one_intent_is_isolated_technical_failure(
        db, planning_conn, service_actor, human_actor, monkeypatch):
    """A per-intent DB error is caught by the per-intent SAVEPOINT: it records ``technical_failure``
    for that intent WITHOUT poisoning the manifest or the OTHER intent (which still resolves)."""
    _seed_resolved_topology(planning_conn, service_actor, human_actor)
    intents = {"i_bad": _ratio_intent(), "i_good": _ratio_intent()}
    bad = intents["i_bad"]
    real_plan = multisource_shadow.plan_multi_source

    def _flaky(conn, adapter, *, intent, **kw):
        if intent is bad:
            # a REAL DB error inside the per-intent savepoint (aborts the subtransaction); the
            # savepoint rollback must restore the outer txn so i_good still plans.
            conn.execute("SELECT * FROM __multisource_shadow_no_such_table__")
        return real_plan(conn, adapter, intent=intent, **kw)

    monkeypatch.setattr(multisource_shadow, "plan_multi_source", _flaky)
    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents=intents, run_id="mrun_err", roles=("feature_engineer",), now=_NOW)

    rows = {row["intent_id"]: row for row in read_intent_results(db, "mrun_err")}
    assert rows["i_bad"]["technical_status"] == "technical_failure"
    assert rows["i_bad"]["semantic_outcome"] == "not_evaluated"
    assert rows["i_bad"]["compile_completeness"] == "not_applicable"
    # the OTHER intent is unpoisoned — the savepoint isolated the failure
    assert rows["i_good"]["semantic_outcome"] == "resolved"
    assert rows["i_good"]["technical_status"] == "ok"
    # the manifest is intact: reconcile still complete (both expected ids present)
    rec = reconcile(db, "mrun_err")
    assert rec.expected == 2
    assert rec.present == 2
    assert rec.complete


def test_budget_exhausting_run_records_truncation(
        db, planning_conn, service_actor, human_actor, monkeypatch):
    """The harness owns ONE mutable ``CompileBudget`` across intents. With the per-run compile
    allowance set to 1, the first intent consumes it (one compile) and the second is recorded
    ``budget_truncated`` (technical axis) — never silently dropped."""
    _seed_resolved_topology(planning_conn, service_actor, human_actor)
    monkeypatch.setattr(multisource_shadow, "MAX_MULTISOURCE_COMPILES_PER_RUN", 1)
    intents = {"i_a": _ratio_intent(), "i_b": _ratio_intent()}

    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents=intents, run_id="mrun_budget", roles=("feature_engineer",), now=_NOW,
        monotonic=lambda: 0.0)   # pin the clock so ONLY the compile-count bound can fire

    rows = {row["intent_id"]: row for row in read_intent_results(db, "mrun_budget")}
    # i_a (sorted first) planned + compiled; i_b truncated once the run budget was spent
    assert rows["i_a"]["semantic_outcome"] == "resolved"
    assert rows["i_b"]["technical_status"] == "budget_truncated"
    assert rows["i_b"]["semantic_outcome"] == "not_evaluated"
    assert rows["i_b"]["compile_completeness"] == "not_applicable"
    # both are still captured (reconcile complete) — truncation is recorded, not lost
    rec = reconcile(db, "mrun_budget")
    assert rec.expected == 2
    assert rec.present == 2
    assert rec.complete


def test_telemetry_persists_despite_fixture_rollback_two_connection_boundary(
        db, planning_conn, service_actor, human_actor):
    """THE two-connection proof: after the harness runs, the GOLD is gone on ``planning_conn`` (the
    fixture transaction was rolled back) yet the TELEMETRY persists on ``telemetry_conn``. A single
    connection could not do both — the rollback that discards the gold would also discard the
    telemetry."""
    _seed_resolved_topology(planning_conn, service_actor, human_actor)
    # the gold IS visible on planning_conn before the run
    assert planning_conn.execute("SELECT count(*) FROM graph_node").fetchone()[0] > 0

    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents={"i_a": _ratio_intent()}, run_id="mrun_boundary",
        roles=("feature_engineer",), now=_NOW)

    # after the run the harness rolled planning_conn back -> its gold is GONE (a fresh txn sees 0)
    assert planning_conn.execute("SELECT count(*) FROM graph_node").fetchone()[0] == 0
    # but the telemetry durably survives on the OTHER connection
    assert len(read_intent_results(db, "mrun_boundary")) == 1
    assert reconcile(db, "mrun_boundary").complete


def test_resolved_assembly_stale_union_lands_compile_incomplete(
        db, planning_conn, service_actor, human_actor):
    """M22: a governed plan whose ASSEMBLY axis resolves but whose compile-end UNION freshness is stale
    lands ``semantic_outcome=resolved`` + ``compile_completeness=incomplete`` in the store — the two
    axes recorded separately, never collapsed into a clean resolve."""
    _seed_stale_union_topology(planning_conn, service_actor, human_actor)

    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents={"i_a": _ratio_intent()}, run_id="mrun_stale",
        roles=("feature_engineer",), now=_NOW)

    row = read_intent_results(db, "mrun_stale")[0]
    assert row["semantic_outcome"] == "resolved"        # assembly axis: a governed plan WAS assembled
    assert row["compile_completeness"] == "incomplete"  # contract axis: stale union -> NOT clean resolve
    assert row["technical_status"] == "ok"              # not a technical/truncation failure


def test_governed_crossings_persisted_for_resolved_operand(
        db, planning_conn, service_actor, human_actor):
    """I-1 end-to-end: a resolved cross-catalog operand persists its governed crossings on the operand
    row — the VERIFIED bridge (authority=verified, carrying the audit ``confirmed_event_id`` re-queried
    BEFORE the fixture rollback) plus the declared intra-catalog realization — so crossing-governedness
    is FALSIFIABLE from persisted telemetry, not only from the endpoint grain-facts."""
    _seed_resolved_topology(planning_conn, service_actor, human_actor)

    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents={"i_a": _ratio_intent()}, run_id="mrun_cross",
        roles=("feature_engineer",), now=_NOW)

    plan_id = read_intent_results(db, "mrun_cross")[0]["selected_plan_id"]
    operands = read_operands(db, "mrun_cross", "i_a", plan_id)
    assert operands
    for o in operands:
        crossings = list(o["crossings"])
        assert crossings, f"expected governed crossings on slot {o['slot_id']}"
        # every crossing is a governed authority (VERIFIED bridge / approved-or-declared realization)
        assert all(c["authority"] in {"verified", "declared_join", "approved_join"}
                   for c in crossings)
    # the VERIFIED bridge crossing carries its audit confirmed_event_id (re-queried pre-rollback)
    bridge_crossings = [c for o in operands for c in o["crossings"]
                        if c["kind"] == "governed_bridge"]
    assert bridge_crossings
    assert all(c["authority"] == "verified" for c in bridge_crossings)
    assert any(c["confirmed_event_id"] == "evt-bfk_acct" for c in bridge_crossings)


# ── CLI/admin entrypoint: the flag is read HERE (never in the harness) ──
class _FakeConn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_cli_entrypoint_flag_off_is_a_noop():
    """Flag off -> the entrypoint is a NO-OP: it opens NO connection and returns None."""
    def _connect():
        raise AssertionError("connect() must not be called when the flag is off")

    out = run_shadow_cli(
        intents_provider=lambda _c: {}, run_id="mrun_off", roles=("feature_engineer",),
        now=_NOW, connect=_connect, env={})
    assert out is None


def test_cli_entrypoint_flag_on_opens_two_connections_and_runs(monkeypatch):
    """Flag on -> the entrypoint reads the flag, opens TWO distinct connections, invokes the harness,
    and COMMITS the telemetry connection (durable — the point of the second connection)."""
    conns: list[_FakeConn] = []

    def _connect():
        c = _FakeConn()
        conns.append(c)
        return c

    captured: dict[str, object] = {}

    def _fake_harness(*, planning_conn, telemetry_conn, adapter, intents, run_id, roles, now,
                      monotonic):
        captured["planning_conn"] = planning_conn
        captured["telemetry_conn"] = telemetry_conn
        return ()

    monkeypatch.setattr(multisource_shadow, "run_multisource_assembly_shadow", _fake_harness)
    out = run_shadow_cli(
        intents_provider=lambda _c: {}, run_id="mrun_on", roles=("feature_engineer",),
        now=_NOW, connect=_connect, env={multisource_shadow.MULTISOURCE_ASSEMBLY_SHADOW_FLAG: "1"})

    assert out == ()
    assert len(conns) == 2                                   # two distinct connections opened
    assert captured["planning_conn"] is not captured["telemetry_conn"]
    telemetry = captured["telemetry_conn"]
    assert telemetry.committed                              # telemetry made durable
    assert all(c.closed for c in conns)                    # both connections closed

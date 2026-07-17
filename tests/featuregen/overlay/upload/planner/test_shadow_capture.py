from __future__ import annotations

from tests.featuregen.overlay.upload.planner.test_plan import (
    _NOW,
    _catalog,
    _freshness,
    _seed_bridge,
    _split,
    _tmpl,
    _txn_template,
)

from featuregen.overlay.upload.planner import shadow as shadow_mod
from featuregen.overlay.upload.planner import shadow_capture as sc
from featuregen.overlay.upload.planner import shadow_store as ss
from featuregen.overlay.upload.planner.contracts import (
    PlanResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.shadow import run_shadow_planner
from featuregen.overlay.upload.planner.shadow_store import PlannerOutcome


def _cross_seed(db):
    _split(db)
    _seed_bridge(db, "bfk_cap", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    _freshness(db, "ops", "rev")


# ── unit: the total planner-outcome map + compile-axes ──
def test_planner_outcome_is_total_over_plan_resolution_status():
    import types
    for st in PlanResolutionStatus:
        fake = types.SimpleNamespace(result_status=st, primary_reason_code=None)
        assert isinstance(sc._planner_outcome(fake), PlannerOutcome)   # never raises / unmapped


def test_planner_outcome_no_authorized_catalog():
    import types
    fake = types.SimpleNamespace(result_status=PlanResolutionStatus.not_applicable,
                                 primary_reason_code=ReasonCode.no_authorized_catalog)
    assert sc._planner_outcome(fake) is PlannerOutcome.no_authorized_catalog


# ── integration: persist off/on, pre-loop failure, template-not-found, reconcile ──
def test_persist_off_writes_nothing(db):
    _catalog(db, "core")
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}), target_entity="customer",
                       roles=(), run_id="run_off", now=_NOW, templates=(_tmpl(),), persist=False)
    assert db.execute("SELECT count(*) FROM planner_shadow_dispatch").fetchone()[0] == 0


def test_persist_on_writes_manifest_and_reconciles(db):
    _catalog(db, "core")
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}), target_entity="customer",
                       roles=(), run_id="run_p", now=_NOW, templates=(_tmpl(),), persist=True)
    assert ss.reconcile(db, "run_p").complete
    rows = ss.read_run_results(db, "run_p")
    assert len(rows) == 1 and rows[0]["recipe_id"] == "t_bal"


def test_compile_disabled_when_path_resolved_but_compile_off(db):
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="run_cd", now=_NOW, templates=(_txn_template(),),
                       compile_contracts=False, persist=True)
    row = ss.read_run_results(db, "run_cd")[0]
    assert row["compile_status"] == "compile_disabled"        # path-resolved candidates, compile off (F2)
    assert row["path_resolved_eligible"] >= 1
    obs = ss.read_observations(db, "run_cd")
    assert obs and all(o["is_compiled"] is False for o in obs)  # nothing compiled


def test_compile_on_completes_and_compiles_observations(db):
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="run_co", now=_NOW, templates=(_txn_template(),),
                       compile_contracts=True, persist=True)
    row = ss.read_run_results(db, "run_co")[0]
    assert row["compile_status"] == "complete" and row["planner_input_hash"] is not None
    obs = ss.read_observations(db, "run_co")
    assert any(o["is_compiled"] is True for o in obs)


def test_preloop_failure_retains_manifest_and_writes_failure_rows(db, monkeypatch):
    _catalog(db, "core")

    def _boom(*a, **k):
        raise RuntimeError("scope resolution blew up")

    monkeypatch.setattr(shadow_mod, "resolve_catalog_scope", _boom)
    results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}), target_entity="customer",
                                 roles=(), run_id="run_pf", now=_NOW, templates=(_tmpl(),), persist=True)
    assert results == ()                                   # pre-loop failed, returned normally
    assert db.execute("SELECT count(*) FROM planner_shadow_dispatch "
                      "WHERE generation_run_id='run_pf'").fetchone()[0] == 1   # manifest RETAINED
    row = ss.read_run_results(db, "run_pf")[0]
    assert row["planner_outcome"] == "preloop_failure"


def test_persistence_failure_retains_manifest_and_reconcile_detects_loss(db, monkeypatch):
    # F6: a write_run_and_plans failure is caught internally (never re-propagated) so the manifest is
    # retained; the loss surfaces via manifest<->results reconciliation (not a circular self-report).
    _catalog(db, "core")

    def _boom(*a, **k):
        raise RuntimeError("store write failed")

    monkeypatch.setattr(shadow_mod, "write_run_and_plans", _boom)
    results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}), target_entity="customer",
                                 roles=(), run_id="run_pl", now=_NOW, templates=(_tmpl(),), persist=True)
    assert len(results) == 1                                    # planning succeeded; only persistence failed
    rec = ss.reconcile(db, "run_pl")                            # manifest RETAINED
    assert rec.expected == 1 and rec.present == 0 and rec.missing_recipe_ids == ("t_bal",)
    assert not rec.complete                                     # the loss is detected


# ── D6: compile-status refinement (budget_time vs budget_count) + identity-comparability ──
def test_is_identity_comparable_excludes_only_incomplete():
    assert sc.is_identity_comparable(ss.CompileStatus.complete) is True
    assert sc.is_identity_comparable(ss.CompileStatus.not_applicable) is True
    assert sc.is_identity_comparable(ss.CompileStatus.compile_disabled) is True
    assert sc.is_identity_comparable(ss.CompileStatus.incomplete) is False   # budget-truncated → excluded


def test_compile_axes_labels_budget_time_vs_count():
    import types

    from featuregen.overlay.upload.planner.contracts import (
        ContractResolutionStatus as CRS,
    )
    from featuregen.overlay.upload.planner.contracts import (
        PathResolutionStatus as PRS,
    )

    def _p(compiled):
        return types.SimpleNamespace(
            path_resolution_status=PRS.source_to_target_resolved,
            contract_resolution_status=CRS.resolved if compiled else CRS.not_compiled)

    result = types.SimpleNamespace(candidate_plans=(_p(True), _p(False)))   # 2 eligible, 1 compiled
    st, reason, elig, comp, skip = sc._compile_axes(result, True, True)
    assert st is ss.CompileStatus.incomplete and reason is ss.IncompleteReason.budget_time
    assert (elig, comp, skip) == (2, 1, 1)
    assert sc._compile_axes(result, True, False)[1] is ss.IncompleteReason.budget_count
    assert sc._compile_axes(result, True, None)[1] is ss.IncompleteReason.budget_count   # None → count


def test_real_elapsed_time_deadline_marks_incomplete_budget_time(db):
    # D6/F17: a compile pass that overruns the REAL elapsed-time deadline (injected monotonic clock
    # already past it) is truncated → compile_status=incomplete / incomplete_reason=budget_time.
    _cross_seed(db)
    calls = {"n": 0}

    def clock():   # construction reads 0 (deadline=+30s); the first compile check is already past it
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1e6

    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="run_bt", now=_NOW, templates=(_txn_template(),),
                       compile_contracts=True, persist=True, monotonic=clock)
    row = ss.read_run_results(db, "run_bt")[0]
    assert row["compile_status"] == "incomplete"
    assert row["incomplete_reason"] == "budget_time"
    assert row["compiled_count"] == 0 and row["skipped_count"] >= 1


def test_template_not_found_writes_a_row(db):
    _catalog(db, "core")
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal", "ghost"}), target_entity="customer",
                       roles=(), run_id="run_tnf", now=_NOW, templates=(_tmpl(),), persist=True)
    rows = {r["recipe_id"]: r["planner_outcome"] for r in ss.read_run_results(db, "run_tnf")}
    assert rows.get("ghost") == "template_not_found"
    assert ss.reconcile(db, "run_tnf").complete   # BOTH eligible ids accounted for

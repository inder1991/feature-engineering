import dataclasses
import logging

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
from featuregen.overlay.upload.planner.contracts import (
    ContractResolutionStatus,
    PathResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.shadow import run_shadow_planner


def test_run_shadow_planner_logs_per_recipe(db, caplog):
    _catalog(db, "core")
    with caplog.at_level(logging.INFO):
        results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}),
                                     target_entity="customer", roles=(), run_id="run1", now=_NOW,
                                     templates=(_tmpl(),))
    assert len(results) == 1 and results[0].recipe_id == "t_bal"


# ---------------------------------------------------------------------------------------------
# Task C8 — compile_contracts: ONE CompilerContext per run, a run-owned CompileBudget that
# persists ACROSS recipes (F10), the contract roll-up on the summary line, and a run metric
# derived from the injected `now` (never a wall-clock read in the deterministic path).
# ---------------------------------------------------------------------------------------------

def _c8_seed_shadow(db):
    _split(db)
    _seed_bridge(db, "bfk_shadow", "account",
                 "ops", "public.transactions.account_id", "rev", "public.accounts.account_id")
    _freshness(db, "ops", "rev")


def _cross_of(result):
    return next(p for p in result.candidate_plans
                if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved)


def test_shadow_compile_contracts_compiles_and_logs_the_rollup(db, caplog):
    _c8_seed_shadow(db)
    with caplog.at_level(logging.INFO):
        results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}),
                                     target_entity="account", roles=(), run_id="run_c8", now=_NOW,
                                     templates=(_txn_template(),), compile_contracts=True)
    (res,) = results
    assert res.contract_result_status is ContractResolutionStatus.resolved
    assert res.selected_contract_physical_plan_id and res.selected_contract_id
    messages = [r.getMessage() for r in caplog.records]
    # the per-recipe summary line carries the contract roll-up
    assert any("shadow_binding_plan" in m and "contract_status=resolved" in m for m in messages)
    # the run metric: compile count + budget flag, derived WITHOUT wall-clock reads
    assert any("shadow_contract_compile_run" in m and "compiles=1" in m and "budget_hit=False" in m
               for m in messages)


def test_shadow_budget_persists_across_recipes(db, caplog, monkeypatch):
    # F10: the budget is owned by the RUN, not the recipe — with MAX_COMPILES_PER_RUN=1 the first
    # recipe (sorted order) spends the whole allowance and the second recipe's cross-catalog plan
    # honestly records compile_budget_exhausted instead of compiling.
    _c8_seed_shadow(db)
    monkeypatch.setattr(shadow_mod, "MAX_COMPILES_PER_RUN", 1)
    t1 = _txn_template()
    t2 = dataclasses.replace(t1, id="t_roll_b")
    with caplog.at_level(logging.INFO):
        results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll", "t_roll_b"}),
                                     target_entity="account", roles=(), run_id="run_c8b",
                                     now=_NOW, templates=(t1, t2), compile_contracts=True)
    by_id = {r.recipe_id: r for r in results}
    first_cross = _cross_of(by_id["t_roll"])
    assert first_cross.contract_resolution_status is ContractResolutionStatus.resolved
    second_cross = _cross_of(by_id["t_roll_b"])
    assert second_cross.contract_resolution_status is ContractResolutionStatus.not_compiled
    assert ReasonCode.compile_budget_exhausted in second_cross.contract_reason_codes
    assert by_id["t_roll_b"].contract_result_status is ContractResolutionStatus.not_compiled
    assert any("shadow_contract_compile_run" in r.getMessage()
               and "budget_hit=True" in r.getMessage() for r in caplog.records)


def test_shadow_flag_off_never_builds_a_compiler_context(db, monkeypatch):
    _catalog(db, "core")

    def _boom(*_a, **_k):
        raise AssertionError("build_compiler_context must not run when compile_contracts is off")

    monkeypatch.setattr(shadow_mod, "build_compiler_context", _boom)
    results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}),
                                 target_entity="customer", roles=(), run_id="r", now=_NOW,
                                 templates=(_tmpl(),))
    assert all(r.contract_result_status is ContractResolutionStatus.not_compiled for r in results)
    assert all(p.contract_resolution_status is ContractResolutionStatus.not_compiled
               and p.contract_id is None
               for r in results for p in r.candidate_plans)
